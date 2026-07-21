"""
build_membership_dashboard_data.py

Pulls membership data from Mariana Tek, aggregates it, and writes the
result into the "Membership_Data" tab of the shared Google Sheet -- the
dashboard (a single static HTML file) reads that sheet live via Google's
gviz endpoint. Run weekly via GitHub Actions.

No client PII is written to the sheet -- only aggregated counts/revenue
per week/studio/membership type. The sheet is "published to web," so
treat anything written here as effectively public.

IMPORTANT -- UNCONFIRMED FIELD:
membership_instances does not have an obvious "studio/location" field in
the documented API response. This script currently derives location via
each instance's related user's `home_location`, which is a reasonable
proxy but NOT necessarily "which studio this membership was purchased
at." Before trusting studio-level breakdowns in the dashboard, inspect
`membership_instances_raw_sample.json` (saved locally when this script
runs) and confirm this assumption holds -- or find a better field.

Required environment variables:
    MARIANA_API_KEY
    MARIANA_BASE_URL
    GOOGLE_SERVICE_ACCOUNT_JSON -- full contents of the service account
                                    JSON key file, as a single string
                                    (set as a GitHub Actions secret)
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
from config import STUDIOS, DASHBOARD_SHEET_ID, DASHBOARD_MEMBERSHIP_TAB

BASE_URL = os.environ.get("MARIANA_BASE_URL", "https://enmei.marianatek.com/api")
HEADERS = {"Authorization": f"Bearer {os.environ['MARIANA_API_KEY']}"}

WEEKS_OF_HISTORY = 26

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_all(resource, page_size=100, filters=None):
    """Same pagination pattern as mariana_client.get_all -- pull every page."""
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
            all_records.append(row)

        total_pages = payload["meta"]["pagination"]["pages"]
        print(f"[{resource}] page {page} of {total_pages}")
        if page >= total_pages:
            break
        page += 1

    return pd.DataFrame(all_records)


def build_snapshots(instances_df, users_df):
    """
    Aggregate raw membership_instances into weekly snapshots per studio +
    membership type. CONFIRM the home_location assumption -- see module
    docstring above -- before trusting studio-level numbers.
    """
    df = instances_df.copy()

    df = df.merge(
        users_df[["id", "home_location"]].rename(columns={"id": "user"}),
        on="user",
        how="left",
    )

    df["purchase_date"] = pd.to_datetime(df["purchase_date"], errors="coerce", utc=True)
    df["cancellation_datetime"] = pd.to_datetime(df["cancellation_datetime"], errors="coerce", utc=True)
    df["week"] = df["purchase_date"].dt.to_period("W-SUN").dt.start_time

    cutoff = pd.Timestamp.now(tz="utc") - pd.Timedelta(weeks=WEEKS_OF_HISTORY)
    df = df[df["purchase_date"] >= cutoff]

    rows = []
    for (week, studio, membership_name), group in df.groupby(["week", "home_location", "membership_name"]):
        active_count = (group["status"] == "active").sum()
        new_count = len(group)
        cancelled_count = group["cancellation_datetime"].notna().sum()
        revenue = group["renewal_rate"].fillna(0).astype(float).sum()

        rows.append({
            "week": week.date().isoformat() if pd.notna(week) else "",
            "studio": studio or "",
            "membership_type": membership_name or "",
            "active_count": int(active_count),
            "new_count": int(new_count),
            "cancelled_count": int(cancelled_count),
            "revenue": round(float(revenue), 2),
        })

    return pd.DataFrame(rows)


def write_to_sheet(df):
    """Authenticate with the service account and overwrite the Membership_Data tab."""
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)

    sh = client.open_by_key(DASHBOARD_SHEET_ID)
    try:
        worksheet = sh.worksheet(DASHBOARD_MEMBERSHIP_TAB)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=DASHBOARD_MEMBERSHIP_TAB, rows=1000, cols=10)

    worksheet.clear()
    header = df.columns.tolist()
    values = [header] + df.values.tolist()
    worksheet.update(values, "A1")
    print(f"Wrote {len(df)} rows to '{DASHBOARD_MEMBERSHIP_TAB}' tab.")


def main():
    print("Pulling membership_instances...")
    instances_df = get_all("membership_instances")
    print(f"membership_instances: {instances_df.shape}")

    print("Pulling users (for home_location)...")
    users_df = get_all("users")
    print(f"users: {users_df.shape}")

    debug_path = Path(__file__).parent / "membership_instances_raw_sample.json"
    instances_df.head(20).to_json(debug_path, orient="records", indent=2)
    print(f"Saved raw sample for inspection: {debug_path}")

    snapshots_df = build_snapshots(instances_df, users_df)
    print(f"Aggregated to {len(snapshots_df)} weekly snapshot rows")

    write_to_sheet(snapshots_df)


if __name__ == "__main__":
    main()
