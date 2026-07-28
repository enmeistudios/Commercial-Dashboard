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
import time
import requests
import pandas as pd
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DASHBOARD_SHEET_ID, TAB_LIVE_STATUS, TAB_HISTORY, TAB_CUSTOMER_LOG, TAB_AGE_PYRAMID

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


# Cancellation reasons that mean "customer stayed, just changed something"
# -- NOT real churn. Confirmed by inspecting real tight-gap cancel->new
# pairs together with the business (see script history / conversation).
SWITCH_REASONS = {"upgrade", "downgrade", "new_home_studio"}

# How close a cancellation and a new membership start have to be (in days,
# either direction) to be considered "the same customer continuing" rather
# than a real gap/departure.
SWITCH_GAP_DAYS = 3

# Reasons that represent genuine, controllable churn (the business could
# potentially act on these) vs uncontrollable churn, for the
# Controllable/Uncontrollable split. "other" (once cleaned of renewal
# artifacts) is treated as controllable-unknown.
CONTROLLABLE_REASONS = {"cost", "other"}
UNCONTROLLABLE_REASONS = {"moving", "injury"}


def detect_switches(df):
    """
    Identify cancel->new pairs that represent the SAME customer continuing
    (an upgrade, downgrade, studio switch, or -- most commonly -- a
    renewal artifact from a period when some memberships didn't
    auto-renew and got manually restarted under the same plan).

    Returns two sets of membership `id`s:
      - switch_old_ids: cancelled memberships that should NOT count as
        real churn
      - switch_new_ids: new memberships that should NOT count as a real
        new signup (since the customer didn't actually leave and come
        back, they never left)
    """
    cancelled = df[df["cancellation_datetime"].notna()][
        ["id", "user_id", "membership_name", "cancellation_datetime", "cancellation_reason"]
    ].copy()
    started = df[["id", "user_id", "membership_name", "start_date"]].copy()

    pairs = cancelled.merge(started, on="user_id", suffixes=("_old", "_new"))
    pairs = pairs[pairs["id_old"] != pairs["id_new"]]
    pairs["gap_days"] = (pairs["start_date"] - pairs["cancellation_datetime"]).dt.total_seconds() / 86400
    close = pairs[pairs["gap_days"].abs() <= SWITCH_GAP_DAYS].copy()

    is_switch_reason = close["cancellation_reason"].isin(SWITCH_REASONS)
    is_renewal_artifact = (
        (close["cancellation_reason"].isna() | (close["cancellation_reason"] == "other"))
        & (close["membership_name_old"] == close["membership_name_new"])
    )
    close["is_switch"] = is_switch_reason | is_renewal_artifact
    close = close[close["is_switch"]]

    # If a cancellation matches multiple candidate new starts (or vice
    # versa), keep only the closest-in-time match per side
    close["abs_gap"] = close["gap_days"].abs()
    best_old = close.sort_values("abs_gap").drop_duplicates("id_old")
    best_new = close.sort_values("abs_gap").drop_duplicates("id_new")

    switch_old_ids = set(best_old["id_old"])
    switch_new_ids = set(best_new["id_new"])
    print(f"Switch/renewal detection: {len(switch_old_ids)} cancellations and "
          f"{len(switch_new_ids)} new starts reclassified as NOT real churn/signups")

    return switch_old_ids, switch_new_ids


def get_all_with_relationships(resource, page_size=100, filters=None, max_retries=4):
    """Pull every page of a resource, keeping relationship IDs (not just attributes).
    Retries transient network errors (connection drops, SSL hiccups) with
    exponential backoff, since these are common on CI runners mid-pull."""
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

    switch_old_ids, switch_new_ids = detect_switches(df)
    df["is_switch_cancellation"] = df["id"].isin(switch_old_ids)
    df["is_switch_new"] = df["id"].isin(switch_new_ids)

    return df


def build_live_status(df):
    """Current granular status right now. Cancelled-within-range is now
    computed dynamically on the dashboard side from Membership_History,
    matching whatever timeframe the user has selected -- so no fixed
    7/30/90-day windows are precomputed here anymore."""
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

    result = status_counts.merge(active_revenue, on=["studio", "tier", "membership_name"], how="left")
    result["active_revenue"] = result["active_revenue"].fillna(0)

    result.insert(0, "as_of_date", pd.Timestamp.now(tz="utc").date().isoformat())
    return result


def build_history(df):
    """Full weekly reconstruction: active/frozen/new/cancelled counts + revenue, by studio/tier/type.

    Upgrades, downgrades, studio switches, and renewal artifacts (see
    detect_switches) are excluded from new_count and cancelled_count --
    they're the same customer continuing, not a real departure or a real
    new signup. cancelled_reason_* columns only ever reflect genuine
    departures (cost, moving, unresolved other)."""
    valid_start = df["start_date"].notna()
    start = df.loc[valid_start, "start_date"].min().normalize()
    end = pd.Timestamp.now(tz="utc").normalize()
    weeks = pd.date_range(start, end, freq="W-MON")

    all_combos = df[["studio", "tier", "membership_name"]].drop_duplicates().reset_index(drop=True)
    real_churn_df = df[~df["is_switch_cancellation"]]

    results = []
    for week in weeks:
        active_mask = (df["start_date"] <= week) & (
            df["cancellation_datetime"].isna() | (df["cancellation_datetime"] > week)
        )
        frozen_mask = active_mask & df["freeze_datetime"].notna() & (df["freeze_datetime"] <= week) & (
            df["freeze_reactivation_datetime"].isna() | (df["freeze_reactivation_datetime"] > week)
        )
        # New signups: exclude anyone whose "new" membership was actually a switch/renewal
        new_mask = (df["start_date"] > week - pd.Timedelta(days=7)) & (df["start_date"] <= week) & (~df["is_switch_new"])
        # Cancellations: exclude switch/renewal artifacts entirely
        cancelled_mask = (real_churn_df["cancellation_datetime"] > week - pd.Timedelta(days=7)) & (real_churn_df["cancellation_datetime"] <= week)

        active_grp = df[active_mask].groupby(["studio", "tier", "membership_name"]).agg(
            active_count=("id", "size"), revenue=("renewal_rate", "sum")
        ).reset_index()
        frozen_grp = df[frozen_mask].groupby(["studio", "tier", "membership_name"]).size().reset_index(name="frozen_count")
        new_grp = df[new_mask].groupby(["studio", "tier", "membership_name"]).size().reset_index(name="new_count")
        cancelled_grp = real_churn_df[cancelled_mask].groupby(["studio", "tier", "membership_name"]).size().reset_index(name="cancelled_count")

        # Cancellation reason breakdown -- genuine churn only
        week_cancelled = real_churn_df[cancelled_mask]
        reason_cols = {}
        for reason_group, reasons in [("cost", {"cost"}), ("moving", {"moving"}), ("injury", {"injury"})]:
            mask = week_cancelled["cancellation_reason"].isin(reasons)
            reason_cols[f"cancelled_reason_{reason_group}"] = (
                week_cancelled[mask].groupby(["studio", "tier", "membership_name"]).size().reset_index(name=f"cancelled_reason_{reason_group}")
            )
        # "other" or blank reason, not already resolved as a switch
        # "Other" catches EVERYTHING not explicitly cost/moving/injury --
        # including blank/"other" reasons, but also upgrade/downgrade/
        # new_home_studio cancellations that were NOT matched to a real
        # completed switch (is_switch_cancellation=False for them, so
        # they correctly count as real churn above, but we still need a
        # bucket for them here so this breakdown always sums to the same
        # total as cancelled_count -- no cancellation silently disappears).
        other_mask = ~week_cancelled["cancellation_reason"].isin({"cost", "moving", "injury"})
        reason_cols["cancelled_reason_other"] = (
            week_cancelled[other_mask].groupby(["studio", "tier", "membership_name"]).size().reset_index(name="cancelled_reason_other")
        )

        merged = (all_combos
                  .merge(active_grp, on=["studio", "tier", "membership_name"], how="left")
                  .merge(frozen_grp, on=["studio", "tier", "membership_name"], how="left")
                  .merge(new_grp, on=["studio", "tier", "membership_name"], how="left")
                  .merge(cancelled_grp, on=["studio", "tier", "membership_name"], how="left"))
        for key, reason_df in reason_cols.items():
            merged = merged.merge(reason_df, on=["studio", "tier", "membership_name"], how="left")

        count_cols = ["active_count", "frozen_count", "new_count", "cancelled_count",
                      "cancelled_reason_cost", "cancelled_reason_moving", "cancelled_reason_injury", "cancelled_reason_other"]
        for col in count_cols:
            merged[col] = merged[col].fillna(0).astype(int)
        merged["revenue"] = pd.to_numeric(merged["revenue"], errors="coerce").fillna(0)
        merged.insert(0, "week", week.date().isoformat())
        results.append(merged)

    return pd.concat(results, ignore_index=True)


AGE_BUCKET_BINS = [0, 30, 60, 90, 180, 365, 730, float("inf")]
AGE_BUCKET_LABELS = ["0-30 days", "30-60 days", "60-90 days", "3-6 months",
                      "6-12 months", "12-24 months", "24+ months"]


def build_age_pyramid(df):
    """
    How long have currently-ongoing members (not cancelled -- includes
    frozen, same definition used everywhere else) been with us, bucketed
    by tenure. Uses real start_date. Note: memberships are 6-month
    commitments with an option to renew -- the 3-6 / 6-12 month buckets
    straddle that natural renewal decision point, worth keeping in mind
    when interpreting any drop-off there (may be normal non-renewal, not
    necessarily dissatisfaction).
    """
    today = pd.Timestamp.now(tz="utc")
    ongoing = df[df["cancellation_datetime"].isna()].copy()
    ongoing["age_days"] = (today - ongoing["start_date"]).dt.days
    ongoing["age_bucket"] = pd.cut(ongoing["age_days"], bins=AGE_BUCKET_BINS, labels=AGE_BUCKET_LABELS, right=False)

    result = (
        ongoing.groupby(["studio", "tier", "membership_name", "age_bucket"])
        .size()
        .reset_index(name="count")
    )
    result["age_bucket"] = result["age_bucket"].astype(str)
    result.insert(0, "as_of_date", today.date().isoformat())
    return result


def build_customer_log(df):
    """One row per membership instance -- IDs only, no PII.

    Includes an estimated term_end_date (start_date + commitment_length
    months) so the dashboard can flag members nearing the end of their
    6-month commitment term, without exposing any names/emails -- staff
    look up the customer_id directly in Mariana Tek for outreach. A
    separate private (non-published) sheet with real names is a planned
    follow-up, not yet built."""
    log_df = df[[
        "id", "user_id", "membership_id", "membership_name", "tier", "studio",
        "status", "start_date", "cancellation_datetime", "freeze_datetime",
        "freeze_reactivation_datetime", "renewal_rate", "commitment_length",
    ]].copy()
    log_df.columns = [
        "membership_instance_id", "customer_id", "membership_id", "membership_name",
        "tier", "studio", "status", "start_date", "cancellation_datetime",
        "freeze_datetime", "freeze_reactivation_datetime", "renewal_rate", "commitment_length",
    ]

    def compute_term_end(row):
        if pd.isna(row["start_date"]) or pd.isna(row["commitment_length"]):
            return None
        return row["start_date"] + pd.DateOffset(months=int(row["commitment_length"]))

    log_df["term_end_date"] = log_df.apply(compute_term_end, axis=1)
    return log_df


def write_to_sheet(df, tab_name, max_retries=4):
    """Authenticate and OVERWRITE the given tab (full recompute each run, not append).
    Retries the whole sequence on transient Google API errors (e.g. 503
    service unavailable) -- safe to retry since clear+rewrite is idempotent."""
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)

    # Convert any datetime columns to strings before writing (Sheets API needs plain values)
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
            print(f"Wrote {len(df_out)} rows to '{tab_name}' tab (full overwrite).")
            return
        except gspread.exceptions.APIError as e:
            wait = 2 ** attempt
            print(f"'{tab_name}': Google Sheets API error ({e}), retrying in {wait}s...")
            time.sleep(wait)

    raise RuntimeError(f"Failed to write '{tab_name}' after {max_retries} attempts")


def main():
    df = pull_and_clean()

    print("\nBuilding live status view...")
    live_status_df = build_live_status(df)
    print(f"Live status: {live_status_df.shape[0]} rows")

    print("\nBuilding full historical weekly reconstruction...")
    history_df = build_history(df)
    print(f"History: {history_df.shape[0]} rows")

    print("\nBuilding membership age pyramid...")
    age_pyramid_df = build_age_pyramid(df)
    print(f"Age pyramid: {age_pyramid_df.shape[0]} rows")

    print("\nBuilding customer log (IDs only, no PII)...")
    customer_log_df = build_customer_log(df)
    print(f"Customer log: {customer_log_df.shape[0]} rows")

    if os.environ.get("DEBUG_ONLY_SKIP_SHEET_WRITE") == "true":
        print("\nDEBUG_ONLY_SKIP_SHEET_WRITE is set -- skipping the actual write.")
        print("\n--- Live status (head) ---")
        print(live_status_df.head(10).to_string())
        print("\n--- History (tail) ---")
        print(history_df.tail(10).to_string())
        print("\n--- Age pyramid ---")
        print(age_pyramid_df.to_string())
        print("\n--- Customer log (head) ---")
        print(customer_log_df.head(10).to_string())
        return

    write_to_sheet(live_status_df, TAB_LIVE_STATUS)
    write_to_sheet(history_df, TAB_HISTORY)
    write_to_sheet(age_pyramid_df, TAB_AGE_PYRAMID)
    write_to_sheet(customer_log_df, TAB_CUSTOMER_LOG)


if __name__ == "__main__":
    main()
