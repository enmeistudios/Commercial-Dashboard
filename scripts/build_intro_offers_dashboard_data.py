"""
build_intro_offers_dashboard_data.py

Reconstructs each customer's full purchase timeline (memberships +
credit purchases) to answer: who started on an intro offer, what did
they do next, and did they EVER convert (not just on their very next
purchase -- some people take months). Validated interactively before
being written into this script; see conversation history for the real
data behind each threshold/classification choice below.

Two intro offer products exist:
  - "1 Week Unlimited" (a membership) -- has real studio data via
    purchase_location_id, same as any other membership.
  - "Welcome 3" (a credit purchase, origination_type="purchase",
    is_intro_offer=True) -- credits are usable at ANY studio, so this
    is treated as studio-agnostic ("All Studios") by design, not a data
    gap. Casual drop-ins (Single/1 Credit) are also studio-agnostic for
    the same reason.

Outcome classification (lifetime, mutually exclusive, in priority order):
  1. Converted to Membership -- ever bought a real recurring membership
     after their intro offer, however long it took.
  2. Converted to Pack -- ever bought a 5+ credit/class pack (not
     membership).
  3. Tried Another Intro Offer -- ever bought the OTHER intro offer
     product without ever converting (intro-offer-hopping).
  4. Casual Re-engagement Only -- bought something again (drop-ins,
     small packs, the Recovery perk) but never converted or hopped.
  5. No Further Purchase -- nothing bought since the intro offer.

Writes four tabs:
  - IntroOffers_Live: KPI snapshot, one row per intro type + an "All" row.
    Now also includes attendance stats (avg/median classes attended,
    avg/median days between visits).
  - IntroOffers_History: weekly starters/conversions, for trend charts.
  - IntroOffers_Flow: first-purchase-category -> outcome counts, for the
    flow/Sankey-style chart.
  - IntroOffers_Attendance: conversion rate by number of classes attended
    during the intro offer, per intro type -- the real "does showing up
    predict conversion" answer (validated interactively: yes, strongly,
    for 1 Week Unlimited; much more weakly for Welcome 3).

Attendance data comes from the "Reservations" report (table_report_data/341),
filtered to reservation_status="check in" plus the specific
membership/credit ID for each intro offer product. This report caps at
500 rows per request and does NOT support page-based pagination, so we
loop through it one calendar month at a time instead (same trick used
for the membership reports).

KNOWN LIMITATION (accepted tradeoff, see conversation history): Welcome 3
check-ins are matched via credit_id=2356 only, which covers ~95% of real
Welcome 3 purchases. A small number of edge-case purchases under
different internal credit_ids (2323, 2521) and any complimentary/comped
intro credits are not included in attendance figures. This was a
deliberate scope decision, not an oversight -- full precision would
require tracing each reservation's specific deduction transaction back
to its parent purchase transaction, a meaningfully bigger build for a
small accuracy gain.

No client PII anywhere -- counts and aggregates only.

Required environment variables:
    MARIANA_API_KEY
    MARIANA_BASE_URL
    GOOGLE_SERVICE_ACCOUNT_JSON
"""

import os
import sys
import json
import re
import time
import requests
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import date

import gspread
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    DASHBOARD_SHEET_ID,
    TAB_INTRO_LIVE, TAB_INTRO_HISTORY, TAB_INTRO_FLOW, TAB_INTRO_ATTENDANCE,
)

BASE_URL = os.environ.get("MARIANA_BASE_URL", "https://enmei.marianatek.com/api")
HEADERS = {"Authorization": f"Bearer {os.environ['MARIANA_API_KEY']}"}
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

INTRO_OFFER_NAMES = {"welcome 3", "1 week unlimited"}

# Real internal IDs, confirmed via table_reports/341 filter metadata + direct
# lookups (the report's "Credit"/"Membership" columns show internal catalog
# names, not customer-facing labels like "Welcome 3" -- so we filter by ID,
# not by text). See module docstring for the accepted Welcome 3 edge-case gap.
ONE_WEEK_UNLIMITED_MEMBERSHIP_IDS = {2921, 3215}  # Chelsea, Marylebone
WELCOME_3_CREDIT_ID = 2356
RESERVATIONS_REPORT_ID = 341
CASUAL_NAMES = {"single", "1 credit"}
PERK_NAMES = {"recovery — compression boots", "recovery - compression boots"}


def get_all_with_relationships(resource, page_size=100, filters=None, max_retries=4):
    """Same resilient paginated puller used for Membership Health."""
    all_records = []
    page = 1
    while True:
        params = {"page": page, "page_size": page_size}
        if filters:
            params.update(filters)

        payload = None
        for attempt in range(max_retries):
            try:
                resp = requests.get(f"{BASE_URL}/{resource}", headers=HEADERS, params=params, timeout=30)
                resp.raise_for_status()
                payload = resp.json()
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, OSError) as e:
                wait = 2 ** attempt
                print(f"[{resource}] page {page}: network error ({e}), retrying in {wait}s...")
                time.sleep(wait)

        if payload is None:
            raise RuntimeError(f"Failed to fetch page {page} of {resource} after {max_retries} attempts")

        for record in payload["data"]:
            row = {"id": record["id"], **record["attributes"]}
            for rel_name, rel_data in record.get("relationships", {}).items():
                data = rel_data.get("data")
                if isinstance(data, list):
                    row[f"{rel_name}_ids"] = [d["id"] for d in data]
                elif isinstance(data, dict):
                    row[f"{rel_name}_id"] = data["id"]
                else:
                    row[f"{rel_name}_id"] = None
            all_records.append(row)

        total_pages = payload["meta"]["pagination"]["pages"]
        print(f"[{resource}] page {page} of {total_pages}")
        if page >= total_pages:
            break
        page += 1

    return pd.DataFrame(all_records)


def classify_purchase_category(name):
    """Broad category for any single purchase -- used to build the timeline."""
    name_lower = (name or "").lower()
    if name_lower in INTRO_OFFER_NAMES:
        return "intro_offer"
    if name_lower in CASUAL_NAMES:
        return "casual"
    if name_lower in PERK_NAMES:
        return "perk"
    m = re.match(r"^(\d+)\s+(classes|credits)$", name_lower)
    if m:
        return "pack_5plus" if int(m.group(1)) >= 5 else "pack_small"
    return "membership"


def pull_events():
    """Pull membership_instances + credit purchases, build one unified
    chronological event timeline per customer."""
    print("Pulling membership_instances...")
    instances_df = get_all_with_relationships("membership_instances")
    print(f"membership_instances: {instances_df.shape}")

    print("Pulling locations...")
    locations_df = get_all_with_relationships("locations")
    loc_map = dict(zip(locations_df["id"].astype(str), locations_df["name"]))

    # For Intro Offers, "1 Week Unlimited" is exactly what we're measuring
    # -- unlike Membership Health, we do NOT exclude it here. Only the
    # Recovery perk (a client perk, not a real product decision) is dropped
    # from the membership side (it still appears via credit purchases below).
    membership_df = instances_df.copy()
    membership_df["studio"] = membership_df["purchase_location_id"].astype(str).map(loc_map)
    name_lower = membership_df["membership_name"].str.lower().fillna("")
    membership_df = membership_df[~name_lower.str.contains("recovery", na=False)].copy()

    for col in ["start_date", "purchase_date"]:
        membership_df[col] = pd.to_datetime(membership_df[col], utc=True, format="ISO8601", errors="coerce")

    membership_events = membership_df[["user_id", "purchase_date", "membership_name", "studio"]].copy()
    membership_events.columns = ["user_id", "event_date", "product_name", "studio"]
    membership_events["event_type"] = "membership"

    print("Pulling credit_transactions...")
    credit_df = get_all_with_relationships("credit_transactions", page_size=2000)
    print(f"credit_transactions (all): {credit_df.shape}")
    credit_df = credit_df[credit_df["origination_type"] == "purchase"].copy()
    print(f"credit_transactions (purchases only): {credit_df.shape}")

    credit_df["event_date"] = pd.to_datetime(credit_df["transaction_datetime"], utc=True, format="ISO8601", errors="coerce")
    credit_events = credit_df[["user_id", "event_date", "credit_name"]].copy()
    credit_events.columns = ["user_id", "event_date", "product_name"]
    credit_events["studio"] = "All Studios"  # credits are usable anywhere -- studio-agnostic by design
    credit_events["event_type"] = "credit"

    all_events = pd.concat([membership_events, credit_events], ignore_index=True)
    all_events = all_events.dropna(subset=["event_date", "user_id"])
    all_events["purchase_category"] = all_events["product_name"].apply(classify_purchase_category)
    all_events = all_events.sort_values(["user_id", "event_date"])
    all_events["purchase_rank"] = all_events.groupby("user_id").cumcount() + 1

    return all_events


def build_outcomes(all_events):
    """For every intro offer starter, determine their lifetime outcome."""
    first_purchases = all_events[all_events["purchase_rank"] == 1]
    intro_starters = first_purchases[first_purchases["product_name"].str.lower().isin(INTRO_OFFER_NAMES)].copy()
    intro_starters = intro_starters.rename(columns={
        "product_name": "intro_type", "event_date": "intro_date", "studio": "intro_studio",
    })[["user_id", "intro_type", "intro_date", "intro_studio"]]

    outcomes = []
    for _, starter in intro_starters.iterrows():
        uid = starter["user_id"]
        user_events = all_events[(all_events["user_id"] == uid) & (all_events["purchase_rank"] > 1)]

        ever_membership = (user_events["purchase_category"] == "membership").any()
        ever_pack = (user_events["purchase_category"] == "pack_5plus").any()
        ever_hopped = user_events["product_name"].str.lower().isin(INTRO_OFFER_NAMES).any()
        has_any_subsequent = len(user_events) > 0

        days_to_convert = None
        if ever_membership:
            first_membership = user_events[user_events["purchase_category"] == "membership"].iloc[0]
            days_to_convert = (first_membership["event_date"] - starter["intro_date"]).days

        if ever_membership:
            outcome = "Converted to Membership"
        elif ever_pack:
            outcome = "Converted to Pack"
        elif ever_hopped:
            outcome = "Tried Another Intro Offer"
        elif has_any_subsequent:
            outcome = "Casual Re-engagement Only"
        else:
            outcome = "No Further Purchase"

        outcomes.append({
            "user_id": uid, "intro_type": starter["intro_type"], "intro_date": starter["intro_date"],
            "intro_studio": starter["intro_studio"], "outcome": outcome, "days_to_convert": days_to_convert,
        })

    return pd.DataFrame(outcomes)


def build_live_kpis(outcomes_df, one_week_attendance, one_week_gaps, welcome3_attendance, welcome3_gaps):
    """One row per intro type + an 'All' row, with the headline KPIs,
    including real attendance stats from the Reservations report."""
    attendance_lookup = {
        "1 Week Unlimited": (one_week_attendance, one_week_gaps),
        "Welcome 3": (welcome3_attendance, welcome3_gaps),
    }

    rows = []
    for intro_type, group in list(outcomes_df.groupby("intro_type")) + [("All", outcomes_df)]:
        total = len(group)
        if total == 0:
            continue
        converted_membership = (group["outcome"] == "Converted to Membership").sum()
        converted_pack = (group["outcome"] == "Converted to Pack").sum()
        hopped = (group["outcome"] == "Tried Another Intro Offer").sum()
        casual_only = (group["outcome"] == "Casual Re-engagement Only").sum()
        no_further = (group["outcome"] == "No Further Purchase").sum()
        days = group["days_to_convert"].dropna()

        # Attendance stats -- only meaningful per real intro type, not the "All" row
        avg_classes, median_classes, avg_gap, median_gap = None, None, None, None
        if intro_type in attendance_lookup:
            attendance_df, gaps = attendance_lookup[intro_type]
            if len(attendance_df) > 0:
                avg_classes = round(float(attendance_df["classes_attended"].mean()), 2)
                median_classes = round(float(attendance_df["classes_attended"].median()), 1)
            if len(gaps) > 0:
                avg_gap = round(float(np.mean(gaps)), 2)
                median_gap = round(float(np.median(gaps)), 1)

        rows.append({
            "intro_type": intro_type,
            "total_starters": total,
            "converted_membership_count": int(converted_membership),
            "converted_membership_pct": round(converted_membership / total * 100, 1),
            "converted_pack_count": int(converted_pack),
            "converted_pack_pct": round(converted_pack / total * 100, 1),
            "hopped_count": int(hopped),
            "hopped_pct": round(hopped / total * 100, 1),
            "casual_only_count": int(casual_only),
            "casual_only_pct": round(casual_only / total * 100, 1),
            "no_further_purchase_count": int(no_further),
            "no_further_purchase_pct": round(no_further / total * 100, 1),
            "median_days_to_convert": round(float(days.median()), 1) if len(days) > 0 else None,
            "mean_days_to_convert": round(float(days.mean()), 1) if len(days) > 0 else None,
            # Time-to-convert histogram buckets (days between intro offer and
            # actual membership conversion, for those who converted)
            "convert_days_0_7": int(((days >= 0) & (days <= 7)).sum()),
            "convert_days_8_30": int(((days >= 8) & (days <= 30)).sum()),
            "convert_days_31_90": int(((days >= 31) & (days <= 90)).sum()),
            "convert_days_90plus": int((days > 90).sum()),
            # Real attendance stats, from the Reservations report
            "avg_classes_attended": avg_classes,
            "median_classes_attended": median_classes,
            "avg_days_between_visits": avg_gap,
            "median_days_between_visits": median_gap,
        })

    return pd.DataFrame(rows)


def build_history(outcomes_df, all_events):
    """Weekly starters + conversions (event-based: counted in the week the
    event actually happened), by intro type and studio (1 Week Unlimited
    only -- Welcome 3 stays 'All Studios')."""
    outcomes_df = outcomes_df.copy()
    outcomes_df["start_week"] = outcomes_df["intro_date"].dt.to_period("W-SUN").dt.start_time

    # Find the actual conversion event date for those who converted, to bucket by the week THAT happened
    conversion_weeks = []
    for _, row in outcomes_df[outcomes_df["outcome"].isin(["Converted to Membership", "Converted to Pack"])].iterrows():
        uid = row["user_id"]
        target_cat = "membership" if row["outcome"] == "Converted to Membership" else "pack_5plus"
        user_events = all_events[(all_events["user_id"] == uid) & (all_events["purchase_category"] == target_cat)]
        if len(user_events) > 0:
            conv_date = user_events.iloc[0]["event_date"]
            conversion_weeks.append({
                "user_id": uid, "intro_type": row["intro_type"], "intro_studio": row["intro_studio"],
                "conversion_week": conv_date.to_period("W-SUN").start_time, "outcome": row["outcome"],
            })
    conversions_df = pd.DataFrame(conversion_weeks)

    starters_weekly = outcomes_df.groupby(["start_week", "intro_type", "intro_studio"]).size().reset_index(name="starters_count")
    starters_weekly = starters_weekly.rename(columns={"start_week": "week", "intro_studio": "studio"})

    if len(conversions_df) > 0:
        conv_weekly = conversions_df.groupby(["conversion_week", "intro_type", "intro_studio", "outcome"]).size().reset_index(name="count")
        conv_weekly = conv_weekly.rename(columns={"conversion_week": "week", "intro_studio": "studio"})
        conv_pivot = conv_weekly.pivot_table(index=["week", "intro_type", "studio"], columns="outcome", values="count", fill_value=0).reset_index()
        conv_pivot.columns = [c if isinstance(c, str) else c for c in conv_pivot.columns]
    else:
        conv_pivot = pd.DataFrame(columns=["week", "intro_type", "studio"])

    history = starters_weekly.merge(conv_pivot, on=["week", "intro_type", "studio"], how="outer")
    for col in ["starters_count", "Converted to Membership", "Converted to Pack"]:
        if col not in history.columns:
            history[col] = 0
        history[col] = history[col].fillna(0).astype(int)
    history = history.rename(columns={"Converted to Membership": "converted_membership_count", "Converted to Pack": "converted_pack_count"})
    history["week"] = history["week"].apply(lambda d: d.date().isoformat() if pd.notna(d) else "")

    return history[["week", "intro_type", "studio", "starters_count", "converted_membership_count", "converted_pack_count"]]


def pull_checkins(filter_params, start_date="2024-10-01", max_retries=4):
    """Pull all real check-ins matching filter_params (a membership or
    credit ID) from the Reservations report. This report caps at 500
    rows and does NOT support page-based pagination (confirmed by
    testing), so we loop through one calendar month at a time instead."""
    end_date = date.today().isoformat()
    date_chunks = pd.date_range(start_date, end_date, freq="MS")
    all_rows = []
    headers = None

    for start, end in zip(date_chunks[:-1], date_chunks[1:]):
        params = {
            "page_size": 2000, "reservation_status": "check in",
            "min_start_date_day": start.date().isoformat(),
            "max_start_date_day": end.date().isoformat(),
            **filter_params,
        }
        payload = None
        for attempt in range(max_retries):
            try:
                resp = requests.get(f"{BASE_URL}/table_report_data/{RESERVATIONS_REPORT_ID}",
                                     headers=HEADERS, params=params, timeout=30)
                resp.raise_for_status()
                payload = resp.json()["data"]["attributes"]
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, OSError) as e:
                wait = 2 ** attempt
                print(f"Reservations pull {start.date()}: network error ({e}), retrying in {wait}s...")
                time.sleep(wait)
        if payload is None:
            raise RuntimeError(f"Failed to fetch reservations for {start.date()} after {max_retries} attempts")

        headers = payload["headers"]
        all_rows.extend(payload["rows"])
        if payload["max_results_exceeded"]:
            print(f"  WARNING: {start.date()} to {end.date()} still exceeded 500 rows -- data incomplete for this month")

    return pd.DataFrame(all_rows, columns=headers)


def build_attendance_summary(checkins_df):
    """Per-customer classes attended + average gap between visits, from
    real check-in data."""
    if len(checkins_df) == 0:
        return pd.DataFrame(columns=["user_id", "classes_attended"]), []

    df = checkins_df.copy()
    df["Class Start Date"] = pd.to_datetime(df["Class Start Date"], errors="coerce")
    df = df.dropna(subset=["Class Start Date"])

    per_customer = df.groupby("Customer ID").agg(classes_attended=("Class ID", "count")).reset_index()
    per_customer = per_customer.rename(columns={"Customer ID": "user_id"})
    per_customer["user_id"] = per_customer["user_id"].astype(str)

    gaps = []
    for _, group in df.groupby("Customer ID"):
        dates = group["Class Start Date"].sort_values().tolist()
        if len(dates) > 1:
            gaps.extend((dates[i + 1] - dates[i]).days for i in range(len(dates) - 1))

    return per_customer, gaps


def build_attendance_breakdown(outcomes_df, one_week_attendance, welcome3_attendance):
    """Conversion rate by classes attended, per intro type -- the real
    'does showing up predict conversion' answer."""
    outcomes_df = outcomes_df.copy()
    outcomes_df["user_id"] = outcomes_df["user_id"].astype(str)

    def bucket_one_week(n):
        if n == 0: return "0 (never attended)"
        if n <= 2: return "1-2"
        if n <= 4: return "3-4"
        if n <= 6: return "5-6"
        return "7+"

    def bucket_welcome3(n):
        if n == 0: return "0 (never attended)"
        if n == 1: return "1"
        if n == 2: return "2"
        if n == 3: return "3 (used it all)"
        return "4+"

    rows = []
    for intro_type, attendance_df, bucket_fn in [
        ("1 Week Unlimited", one_week_attendance, bucket_one_week),
        ("Welcome 3", welcome3_attendance, bucket_welcome3),
    ]:
        merged = outcomes_df[outcomes_df["intro_type"] == intro_type].merge(
            attendance_df[["user_id", "classes_attended"]], on="user_id", how="left"
        )
        merged["classes_attended"] = merged["classes_attended"].fillna(0)
        merged["bucket"] = merged["classes_attended"].apply(bucket_fn)

        for bucket, group in merged.groupby("bucket"):
            total = len(group)
            converted = (group["outcome"] == "Converted to Membership").sum()
            rows.append({
                "intro_type": intro_type, "attendance_bucket": bucket,
                "count": total, "converted_membership_count": int(converted),
                "conversion_pct": round(converted / total * 100, 1) if total > 0 else 0,
            })

    return pd.DataFrame(rows)


def build_flow(outcomes_df):
    """First-purchase-category -> outcome counts, for the flow/Sankey chart."""
    flow = outcomes_df.groupby(["intro_type", "outcome"]).size().reset_index(name="count")
    return flow


def write_to_sheet(df, tab_name, max_retries=4):
    """Same retry-wrapped overwrite pattern as Membership Health."""
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)

    df_out = df.copy()
    for col in df_out.columns:
        if pd.api.types.is_datetime64_any_dtype(df_out[col]):
            df_out[col] = df_out[col].astype(str).replace("NaT", "")
    values = [df_out.columns.tolist()] + df_out.astype(object).where(df_out.notna(), "").values.tolist()

    for attempt in range(max_retries):
        try:
            sh = client.open_by_key(DASHBOARD_SHEET_ID)
            try:
                worksheet = sh.worksheet(tab_name)
            except gspread.exceptions.WorksheetNotFound:
                worksheet = sh.add_worksheet(title=tab_name, rows=max(len(df) + 10, 100), cols=len(df.columns) + 2)
            worksheet.clear()
            worksheet.update(values, "A1")
            print(f"Wrote {len(df_out)} rows to '{tab_name}' tab.")
            return
        except gspread.exceptions.APIError as e:
            wait = 2 ** attempt
            print(f"'{tab_name}': Google Sheets API error ({e}), retrying in {wait}s...")
            time.sleep(wait)

    raise RuntimeError(f"Failed to write '{tab_name}' after {max_retries} attempts")


def main():
    all_events = pull_events()
    print(f"\nTotal events: {len(all_events)}, unique customers: {all_events['user_id'].nunique()}")

    outcomes_df = build_outcomes(all_events)
    print(f"Intro offer starters: {len(outcomes_df)}")
    print(outcomes_df["outcome"].value_counts())

    print("\nPulling 1 Week Unlimited check-ins (real attendance data)...")
    one_week_checkins = pd.concat([
        pull_checkins({"membership": mid}) for mid in ONE_WEEK_UNLIMITED_MEMBERSHIP_IDS
    ], ignore_index=True)
    one_week_attendance, one_week_gaps = build_attendance_summary(one_week_checkins)
    print(f"1 Week Unlimited: {len(one_week_attendance)} customers with real check-ins")

    print("\nPulling Welcome 3 check-ins (real attendance data)...")
    welcome3_checkins = pull_checkins({"credit": WELCOME_3_CREDIT_ID})
    welcome3_attendance, welcome3_gaps = build_attendance_summary(welcome3_checkins)
    print(f"Welcome 3: {len(welcome3_attendance)} customers with real check-ins")

    live_kpis_df = build_live_kpis(outcomes_df, one_week_attendance, one_week_gaps, welcome3_attendance, welcome3_gaps)
    history_df = build_history(outcomes_df, all_events)
    flow_df = build_flow(outcomes_df)
    attendance_breakdown_df = build_attendance_breakdown(outcomes_df, one_week_attendance, welcome3_attendance)

    if os.environ.get("DEBUG_ONLY_SKIP_SHEET_WRITE") == "true":
        print("\nDEBUG_ONLY_SKIP_SHEET_WRITE is set -- skipping the actual write.")
        print("\n--- Live KPIs ---")
        print(live_kpis_df.to_string())
        print("\n--- History (tail) ---")
        print(history_df.tail(20).to_string())
        print("\n--- Flow ---")
        print(flow_df.to_string())
        print("\n--- Attendance breakdown ---")
        print(attendance_breakdown_df.to_string())
        return

    write_to_sheet(live_kpis_df, TAB_INTRO_LIVE)
    write_to_sheet(history_df, TAB_INTRO_HISTORY)
    write_to_sheet(flow_df, TAB_INTRO_FLOW)
    write_to_sheet(attendance_breakdown_df, TAB_INTRO_ATTENDANCE)


if __name__ == "__main__":
    main()
