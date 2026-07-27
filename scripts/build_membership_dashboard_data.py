"""
build_membership_dashboard_data.py

Rebuilt approach (replaces the old current-status-report-based version):

Pulls `membership_instances` directly -- this resource has real
`start_date`, `cancellation_datetime`, `freeze_datetime`, and
`freeze_reactivation_datetime` fields per membership, going back to
whenever each one actually happened. This means we can RECONSTRUCT full
historical weekly trends (active/frozen/new/cancelled counts + revenue)
from real dates, rather than accumulating one snapshot per week forever.

Every run of this script recomputes everything from scratch and
OVERWRITES the sheet tabs below -- there's no fragile accumulated data
to lose, since real history is always fully reconstructable from source.

Writes three tabs:
  - Membership_Live_Status   -- current granular status right now
                                 (active/frozen/cancelled/terminated/
                                 payment_failure/ding_failure/done/
                                 pending_customer_activation/
                                 pending_start_date/converted), plus
                                 cancelled-within-window counts using
                                 the real cancellation_datetime.
  - Membership_History       -- full weekly reconstruction from the
                                 earliest real start_date to today.
  - Membership_Customer_Log  -- one row per membership instance, with
                                 customer_id + membership_id (IDs only,
                                 NOT PII) for future individual-level
                                 lifecycle analysis. Same principle to
                                 reuse when building Intro Offers.

Exclusions (confirmed with the business, applied everywhere):
  - "1 Week Unlimited" -- technically an intro offer, not a membership.
    NOTE: Mariana Tek's own `is_intro_offer` flag does NOT reliably mark
    this membership type as an intro offer (confirmed by inspecting real
    data) -- so we exclude it by NAME, not by that flag.
  - "Recovery" (Compression Boots) -- a client perk, not a sold membership.

Tier classification: keyword-based, confirmed against the real
membership_name values in this table (see classify_tier below).

Currency: all amounts are GBP.

Required environment variables:
    MARIANA_API_KEY
    MARIANA_BASE_URL
    GOOGLE_SERVICE_ACCOUNT_JSON -- full contents of the service account
                                    JSON key file, as a single string
"""

import os
import sys
import json
import requests
import pandas as pd
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DASHBOARD_SHEET_ID, TAB_LIVE_STATUS, TAB_HISTORY, TAB_CUSTOMER_LOG

BASE_URL = os.environ.get("MARIANA_BASE_URL", "https://enmei.marianatek.com/api")
HEADERS = {"Authorization": f"Bearer {os.environ['MARIANA_API_KEY']}"}
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Membership types that are NOT real memberships -- excluded everywhere.
EXCLUDED_NAME_KEYWORDS = ["1 week unlimited", "recovery"]

# Exact-name matches (lowercase) for the short-term/promo Unlimited tier.
PROMO_UNLIMITED_NAMES = {
    "2 week unlimited", "one month unlimited", "spring sale - unlimited monthly",
}


def classify_tier(name):
    name_lower = (name or "").lower()

    if name_lower in PROMO_UNLIMITED_NAMES:
        return "Promo/Short-term Unlimited"

    if any(kw in name_lower for kw in ["influencer", "fitness instructor", "student/healthcare/teacher", "neighbours"]):
        return "Reduced Price"

    if "12 monthly" in name_lower or "12 classes" in name_lower:
        return "12 Classes"

    if "8 monthly" in name_lower:
        return "8 Classes"

    if "4 monthly" in name_lower:
        return "4 Classes"

    if any(kw in name_lower for kw in ["unlimited", "founding membership", "all access", "chelsea access", "marylebone access"]):
        return "Unlimited"

    return "Other/Unclassified"


def get_all_with_relationships(resource, page_size=100, filters=None):
    """Pull every page of a resource, keeping relationship IDs (not just attributes)."""
    all_records = []
    page = 1
    while True:
        params = {"page": page, "page_size": page_size}
        if filters:
            params.update(filters)

        resp = requests.get(f"{BASE_URL}/{resource}", headers=HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()

        for record in payload["data"]:
            row = {"id": record["id"], **record["attributes"]}
            rels = record.get("relationships", {})
            for rel_name, rel_data in rels.items():
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


def pull_and_clean():
    """Pull membership_instances + locations, join, exclude non-memberships, classify tier."""
    print("Pulling membership_instances...")
    instances_df = get_all_with_relationships("membership_instances")
    print(f"membership_instances: {instances_df.shape}")

    print("Pulling locations (for studio names)...")
    locations_df = get_all_with_relationships("locations")
    loc_map = dict(zip(locations_df["id"].astype(str), locations_df["name"]))

    df = instances_df.copy()
    df["studio"] = df["purchase_location_id"].astype(str).map(loc_map)

    # Exclude non-memberships by name (NOT relying on is_intro_offer -- see
    # module docstring for why)
    name_lower = df["membership_name"].str.lower().fillna("")
    excluded_mask = name_lower.apply(lambda n: any(kw in n for kw in EXCLUDED_NAME_KEYWORDS))
    print(f"Excluding {excluded_mask.sum()} rows matching {EXCLUDED_NAME_KEYWORDS}")
    df = df[~excluded_mask].copy()

    df["tier"] = df["membership_name"].apply(classify_tier)

    unclassified = (df["tier"] == "Other/Unclassified").sum()
    if unclassified > 0:
        print(f"WARNING: {unclassified} rows classified as 'Other/Unclassified' -- "
              f"check membership_name values: "
              f"{df[df['tier'] == 'Other/Unclassified']['membership_name'].unique().tolist()}")

    for col in ["start_date", "purchase_date", "cancellation_datetime",
                "freeze_datetime", "freeze_reactivation_datetime"]:
        df[col] = pd.to_datetime(df[col], utc=True, format="ISO8601", errors="coerce")

    df["renewal_rate"] = pd.to_numeric(df["renewal_rate"], errors="coerce").fillna(0)

    return df


def build_live_status(df):
    """Current granular status right now, plus cancelled-within-window counts."""
    today = pd.Timestamp.now(tz="utc")

    status_counts = (
        df.groupby(["studio", "tier", "membership_name", "status"])
        .size()
        .reset_index(name="count")
        .pivot(index=["studio", "tier", "membership_name"], columns="status", values="count")
        .fillna(0)
        .astype(int)
        .reset_index()
    )

    active_revenue = (
        df[df["status"] == "active"]
        .groupby(["studio", "tier", "membership_name"])["renewal_rate"]
        .sum()
        .reset_index()
        .rename(columns={"renewal_rate": "active_revenue"})
    )

    cancelled_df = df[df["cancellation_datetime"].notna()].copy()
    cancelled_df["days_since_cancel"] = (today - cancelled_df["cancellation_datetime"]).dt.days

    result = status_counts.merge(active_revenue, on=["studio", "tier", "membership_name"], how="left")
    result["active_revenue"] = result["active_revenue"].fillna(0)

    for window in (7, 30, 90):
        windowed = (
            cancelled_df[cancelled_df["days_since_cancel"] <= window]
            .groupby(["studio", "tier", "membership_name"])
            .size()
            .reset_index(name=f"cancelled_last_{window}")
        )
        result = result.merge(windowed, on=["studio", "tier", "membership_name"], how="left")
        result[f"cancelled_last_{window}"] = result[f"cancelled_last_{window}"].fillna(0).astype(int)

    result.insert(0, "as_of_date", pd.Timestamp.now(tz="utc").date().isoformat())
    return result


def build_history(df):
    """Full weekly reconstruction: active/frozen/new/cancelled counts + revenue, by studio/tier/type."""
    valid_start = df["start_date"].notna()
    start = df.loc[valid_start, "start_date"].min().normalize()
    end = pd.Timestamp.now(tz="utc").normalize()
    weeks = pd.date_range(start, end, freq="W-MON")

    all_combos = df[["studio", "tier", "membership_name"]].drop_duplicates().reset_index(drop=True)

    results = []
    for week in weeks:
        active_mask = (df["start_date"] <= week) & (
            df["cancellation_datetime"].isna() | (df["cancellation_datetime"] > week)
        )
        frozen_mask = active_mask & df["freeze_datetime"].notna() & (df["freeze_datetime"] <= week) & (
            df["freeze_reactivation_datetime"].isna() | (df["freeze_reactivation_datetime"] > week)
        )
        new_mask = (df["start_date"] > week - pd.Timedelta(days=7)) & (df["start_date"] <= week)
        cancelled_mask = (df["cancellation_datetime"] > week - pd.Timedelta(days=7)) & (df["cancellation_datetime"] <= week)

        active_grp = df[active_mask].groupby(["studio", "tier", "membership_name"]).agg(
            active_count=("id", "size"), revenue=("renewal_rate", "sum")
        ).reset_index()
        frozen_grp = df[frozen_mask].groupby(["studio", "tier", "membership_name"]).size().reset_index(name="frozen_count")
        new_grp = df[new_mask].groupby(["studio", "tier", "membership_name"]).size().reset_index(name="new_count")
        cancelled_grp = df[cancelled_mask].groupby(["studio", "tier", "membership_name"]).size().reset_index(name="cancelled_count")

        merged = (all_combos
                  .merge(active_grp, on=["studio", "tier", "membership_name"], how="left")
                  .merge(frozen_grp, on=["studio", "tier", "membership_name"], how="left")
                  .merge(new_grp, on=["studio", "tier", "membership_name"], how="left")
                  .merge(cancelled_grp, on=["studio", "tier", "membership_name"], how="left"))

        for col in ["active_count", "frozen_count", "new_count", "cancelled_count"]:
            merged[col] = merged[col].fillna(0).astype(int)
        merged["revenue"] = pd.to_numeric(merged["revenue"], errors="coerce").fillna(0)
        merged.insert(0, "week", week.date().isoformat())
        results.append(merged)

    return pd.concat(results, ignore_index=True)


def build_customer_log(df):
    """One row per membership instance -- IDs only, no PII."""
    log_df = df[[
        "id", "user_id", "membership_id", "membership_name", "tier", "studio",
        "status", "start_date", "cancellation_datetime", "freeze_datetime",
        "freeze_reactivation_datetime", "renewal_rate",
    ]].copy()
    log_df.columns = [
        "membership_instance_id", "customer_id", "membership_id", "membership_name",
        "tier", "studio", "status", "start_date", "cancellation_datetime",
        "freeze_datetime", "freeze_reactivation_datetime", "renewal_rate",
    ]
    return log_df


def write_to_sheet(df, tab_name):
    """Authenticate and OVERWRITE the given tab (full recompute each run, not append)."""
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)

    sh = client.open_by_key(DASHBOARD_SHEET_ID)
    try:
        worksheet = sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=tab_name, rows=max(len(df) + 10, 100), cols=len(df.columns) + 2)

    # Convert any datetime columns to strings before writing (Sheets API needs plain values)
    df_out = df.copy()
    for col in df_out.columns:
        if pd.api.types.is_datetime64_any_dtype(df_out[col]):
            df_out[col] = df_out[col].astype(str).replace("NaT", "")

    worksheet.clear()
    values = [df_out.columns.tolist()] + df_out.astype(object).where(df_out.notna(), "").values.tolist()
    worksheet.update(values, "A1")
    print(f"Wrote {len(df_out)} rows to '{tab_name}' tab (full overwrite).")


def main():
    df = pull_and_clean()

    print("\nBuilding live status view...")
    live_status_df = build_live_status(df)
    print(f"Live status: {live_status_df.shape[0]} rows")

    print("\nBuilding full historical weekly reconstruction...")
    history_df = build_history(df)
    print(f"History: {history_df.shape[0]} rows")

    print("\nBuilding customer log (IDs only, no PII)...")
    customer_log_df = build_customer_log(df)
    print(f"Customer log: {customer_log_df.shape[0]} rows")

    if os.environ.get("DEBUG_ONLY_SKIP_SHEET_WRITE") == "true":
        print("\nDEBUG_ONLY_SKIP_SHEET_WRITE is set -- skipping the actual write.")
        print("\n--- Live status (head) ---")
        print(live_status_df.head(10).to_string())
        print("\n--- History (tail) ---")
        print(history_df.tail(10).to_string())
        print("\n--- Customer log (head) ---")
        print(customer_log_df.head(10).to_string())
        return

    write_to_sheet(live_status_df, TAB_LIVE_STATUS)
    write_to_sheet(history_df, TAB_HISTORY)
    write_to_sheet(customer_log_df, TAB_CUSTOMER_LOG)


if __name__ == "__main__":
    main()
