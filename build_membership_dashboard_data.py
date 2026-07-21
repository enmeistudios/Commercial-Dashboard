"""
build_membership_dashboard_data.py

Pulls current membership status from Mariana Tek's "Membership Details"
report (table_report_data/297), strips PII, aggregates into a snapshot
row per studio + membership type, and appends that snapshot to the
"Membership_Data" tab of the shared Google Sheet.

This is a CURRENT STATUS snapshot, not historical data -- Mariana Tek's
reports only show "right now." Each weekly run adds one new dated
snapshot, so real history builds up over time starting from whenever
this was first run. See README for more on this tradeoff.

Pagination: this report caps at 500 rows per request and does NOT
support page-based pagination (confirmed by testing -- page=2 returns
identical results to page=1). Instead, we loop through every combination
of current_membership_status x location, which keeps each individual
request comfortably under the cap.

TODO (confirmed with the business, not yet resolved):
Some membership types have no studio in their name (e.g. "All Access",
"Fitness Instructors") and currently fall into a "All Locations" bucket
implicitly (since Purchase Location reflects where they were bought,
not necessarily which studio(s) they're valid at). Revisit this once
there's a clear source of truth for which studios these apply to.

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
from datetime import date

import gspread
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DASHBOARD_SHEET_ID, DASHBOARD_MEMBERSHIP_TAB, DASHBOARD_HISTORY_TAB

BASE_URL = os.environ.get("MARIANA_BASE_URL", "https://enmei.marianatek.com/api")
HEADERS = {"Authorization": f"Bearer {os.environ['MARIANA_API_KEY']}"}

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

REPORT_ID = 297  # "Membership Details"

# Confirmed via table_reports/297 filter metadata
STATUSES = [
    "active", "frozen", "cancelled", "terminated", "payment_failure",
    "ding_failure", "done", "pending_customer_activation",
    "pending_start_date", "converted",
]

# Confirmed via get_all("locations") -- add Shoreditch's data once it goes live
LOCATIONS = {
    "Chelsea": 48717,
    "Marylebone": 48750,
    "Shoreditch": 48783,
}


def pull_membership_details():
    """
    Loop through every (status, location) combination to stay under the
    500-row-per-request cap, and combine into one raw DataFrame.
    """
    all_rows = []
    report_headers = None

    for status in STATUSES:
        for loc_name, loc_id in LOCATIONS.items():
            resp = requests.get(
                f"{BASE_URL}/table_report_data/{REPORT_ID}",
                headers=HEADERS,
                params={"page_size": 2000, "current_membership_status": status, "location": loc_id},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()["data"]["attributes"]
            report_headers = data["headers"]

            if data["max_results_exceeded"]:
                print(f"WARNING: {status}/{loc_name} exceeded 500 rows -- data incomplete, "
                      f"consider narrowing further.")

            all_rows.extend(data["rows"])
            if data["rows"]:
                print(f"{status} / {loc_name}: {len(data['rows'])} rows")

    print(f"Total rows collected: {len(all_rows)}")
    return pd.DataFrame(all_rows, columns=report_headers)


def clean_and_aggregate(raw_df):
    """Strip PII, keep only what the dashboard needs, aggregate into one snapshot."""
    clean_df = raw_df[[
        "Membership Status", "Membership Type", "Purchase Location",
        "Purchase Date", "Renewal Rate",
    ]].copy()
    clean_df.columns = ["status", "membership_type", "studio", "purchase_date", "renewal_rate"]

    snapshot_date = date.today().isoformat()

    active_revenue = (
        clean_df[clean_df["status"] == "active"]
        .groupby(["studio", "membership_type"])["renewal_rate"]
        .sum()
        .reset_index()
        .rename(columns={"renewal_rate": "active_revenue"})
    )

    status_counts = (
        clean_df.groupby(["studio", "membership_type", "status"])
        .size()
        .reset_index(name="count")
        .pivot(index=["studio", "membership_type"], columns="status", values="count")
        .fillna(0)
        .astype(int)
        .reset_index()
    )

    snapshot_df = status_counts.merge(active_revenue, on=["studio", "membership_type"], how="left")
    snapshot_df["active_revenue"] = snapshot_df["active_revenue"].fillna(0)
    snapshot_df.insert(0, "snapshot_date", snapshot_date)

    return snapshot_df


def build_membership_history(raw_df):
    """
    Per-membership snapshot for individual-level tracking over time.
    Uses Membership ID + Customer ID -- internal numeric IDs only, NOT
    PII on their own (meaningless without Mariana Tek account access).
    Explicitly excludes Customer Name / Email / Phone Number.
    """
    history_df = raw_df[[
        "Membership ID", "Customer ID", "Membership Status", "Membership Type",
        "Purchase Location", "Purchase Date", "Renewal Rate",
    ]].copy()
    history_df.columns = [
        "membership_id", "customer_id", "status", "membership_type",
        "studio", "purchase_date", "renewal_rate",
    ]
    history_df.insert(0, "snapshot_date", date.today().isoformat())
    return history_df


def append_to_sheet(df, tab_name):
    """Authenticate with the service account and APPEND rows to the given tab."""
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)

    sh = client.open_by_key(DASHBOARD_SHEET_ID)
    try:
        worksheet = sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=tab_name, rows=1000, cols=20)
        worksheet.update([df.columns.tolist()], "A1")

    existing = worksheet.get_all_values()
    if not existing:
        worksheet.update([df.columns.tolist()], "A1")

    worksheet.append_rows(df.values.tolist(), value_input_option="USER_ENTERED")
    print(f"Appended {len(df)} rows to '{tab_name}' tab.")


def main():
    print("Pulling Membership Details report (status x location loop)...")
    raw_df = pull_membership_details()

    print("Cleaning (removing PII) and aggregating into this week's snapshot...")
    snapshot_df = clean_and_aggregate(raw_df)
    print(f"Aggregate snapshot: {snapshot_df.shape[0]} rows (one per studio x membership type)")

    print("Building per-membership history (IDs only, no PII)...")
    history_df = build_membership_history(raw_df)
    print(f"History detail: {history_df.shape[0]} rows (one per membership)")

    if os.environ.get("DEBUG_ONLY_SKIP_SHEET_WRITE") == "true":
        print("\nDEBUG_ONLY_SKIP_SHEET_WRITE is set -- skipping the actual write.")
        print("\n--- Aggregate snapshot ---")
        print(snapshot_df.to_string())
        print("\n--- History detail (first 20 rows) ---")
        print(history_df.head(20).to_string())
        return

    append_to_sheet(snapshot_df, DASHBOARD_MEMBERSHIP_TAB)
    append_to_sheet(history_df, DASHBOARD_HISTORY_TAB)


if __name__ == "__main__":
    main()
