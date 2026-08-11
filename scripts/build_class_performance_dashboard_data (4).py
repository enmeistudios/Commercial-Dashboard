"""
build_class_performance_dashboard_data.py

Coach performance + class utilization, backing the Class Performance
section (heatmaps, coach scorecards, studio-vs-studio comparison).

Two data sources, deliberately NOT joined together:

  1. Report 294 ("Class Session Utilization Details",
     table_report_data/294) -- one row per class that actually took
     place. Confirmed by testing: cancelled classes never appear in
     this report at all, so there's no join/reconciliation needed
     between it and cancellations -- they're just two independent
     counts.

  2. class_sessions endpoint -- used ONLY to count cancelled classes
     (report 294 has no cancellation field). cancellation_datetime
     IS NOT NULL = cancelled.

CONFIRMED API QUIRKS (validated interactively before writing this,
same as every other script in this repo -- see conversation history):

  - Report 294 respects min_start_date_day / max_start_date_day.
    It does NOT respect min_date / max_date (silently ignored --
    returns whatever the default/oldest window is regardless of what
    you ask for). Caps at 500 rows/request with no reliable page
    parameter, so we chunk by date window and adaptively halve the
    window if max_results_exceeded fires.

  - class_sessions endpoint ignores min_date/max_date AND ordering
    (tested: ascending and descending "ordering" params returned
    identical results). The ONLY filter that reliably works is
    `location`. This means counting cancellations requires paging
    through the ENTIRE class_sessions table per location (~20k+ rows
    total across all studios) and filtering by date client-side.
    Expensive, but there's no cheaper confirmed alternative. If this
    becomes a real runtime problem, worth periodically re-testing
    whether MT ever fixes date filtering on this endpoint.

  - Report 294 has NO session ID / composite key that reliably maps
    to class_sessions records. Since cancelled sessions never appear
    in 294 anyway, this doesn't matter for this build -- but if a
    future feature needs to join the two, that join does not
    currently exist and would need to be built via
    Location + Class Date + Class Time + Classroom (fragile, could
    collide if two classes ever share a slot).

EXCLUSIONS (by Class Type name -- validated against real data, not
assumed):
  - "Recovery — Compression Boots": a perk, not a real class. NOT
    reliably identifiable by null instructor (confirmed ~27% of these
    rows DO have an instructor logged) -- name-match is the only
    option here, same category of fragility as the old
    is_intro_offer issue elsewhere in this repo. Revisit if MT ever
    adds a real "session type" category field (Class Category is
    useless for this -- everything is "Strength training"; Class
    Tags only has Standard/Recovery Chair).
  - "FULL BODY": a training session, not a coach-led class. Confirmed
    null-instructor in every sample checked.

NOT excluded, kept as a real class type:
  - "INTRO50": confirmed always has a real instructor logged.

DATA QUALITY, NOT EXCLUSION: any OTHER class type with a null
instructor (e.g. a couple of stray "HUMPDAY50" rows found during
validation) is a genuine data-entry gap -- missing instructor
assignment -- not a hidden category. These are flagged in the
Unassigned_Instructor_Flag column rather than silently dropped, so
they surface as a data-quality issue on the dashboard instead of
disappearing.

Capacity: uses the real "Actual Capacity" field from report 294
directly (NOT a hardcoded per-studio map like the original heatmap
notebook used) -- this is more accurate and automatically covers
Shoreditch. "Layout Capacity" (the room's max) is also kept for
reference on the Definitions page, since the two can differ (e.g. a
reduced equipment setup on a given day).

Admin Holds count as a paid visit -- validated against a real example
(Marylebone, 2026-06-02 18:30: 2 checked-in + 12 admin holds = a
genuine private class, confirmed with Remy). This is real but RARE --
an initial pull of nonzero Admin Holds rows was mostly a red herring:
~150 rows all turned out to be a recurring pattern on "Recovery —
Compression Boots" (already excluded by class type, unrelated to
private classes), leaving only 3 genuine non-excluded classes with
Admin Holds > 0 in a ~2-month window. Unavailable Holds (broken
equipment etc.) are deliberately NOT included -- that's lost capacity,
not a paid visit. FUTURE ENHANCEMENT, not built yet: eventually assign
an average RPV (revenue per visit) specifically to Admin Hold spots,
since private-class economics likely differ from a standard drop-in/
membership visit.

Multi-instructor: no comma-separated names were found in any sample
pulled during validation, but the raw class_sessions API field
(instructor_names) IS a list, so report 294 could theoretically
flatten multiple names together. Instructors is split on "," as a
defensive safeguard, at negligible cost, even though it wasn't
observed in testing.

Writes four tabs:
  - ClassPerformance_Sessions: one row per real class session
    (ID-only, no PII -- just studio/date/time/instructor/type/
    capacity/utilization numbers). Full grain, so the dashboard can
    do all studio/timeframe/coach/compare-to filtering client-side,
    same pattern as IntroOffers_Starters.
  - ClassPerformance_History: weekly rollup (total classes, avg
    utilization, total cancelled) per studio, for trend charts.
  - ClassPerformance_CoachScorecard: one row per coach x studio,
    computed over rolling 14/28/56-day windows (ported from Remy's
    validated Colab notebook logic -- benchmark by studio+time slot,
    utilization lift, paid-pct lift, consistency, momentum,
    demand_creation_score).
  - ClassPerformance_Cancellations: weekly cancelled-class counts per
    studio, from class_sessions.

No client PII anywhere -- instructor names are staff/work info, not
client data, same treatment as coach names in the existing Coach
Talent Development dashboard.

Required environment variables:
    MARIANA_API_KEY
    MARIANA_BASE_URL
    GOOGLE_SERVICE_ACCOUNT_JSON
"""

import os
import sys
import json
import time
import requests
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import date, timedelta

import gspread
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    DASHBOARD_SHEET_ID,
    TAB_CLASS_SESSIONS, TAB_CLASS_HISTORY, TAB_COACH_SCORECARD, TAB_CLASS_CANCELLATIONS,
)

BASE_URL = os.environ.get("MARIANA_BASE_URL", "https://enmei.marianatek.com/api")
HEADERS = {"Authorization": f"Bearer {os.environ['MARIANA_API_KEY']}"}
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

REPORT_294_ID = 294

# Excluded by name -- see module docstring for why these can't be
# identified any other reliable way.
EXCLUDED_CLASS_TYPES = {"recovery — compression boots", "full body"}

# How far back to pull full history on first run / backfill. Subsequent
# runs still pull this whole window each time (no accumulated state --
# same "reconstruct from source every run" principle as Membership
# Health and Intro Offers), since report 294 has no reliable way to
# fetch "just what changed since last time."
HISTORY_START = "2025-01-01"


def get_table_report_294(min_date_str, max_date_str, max_retries=4):
    """Pull report 294 for a date window using the confirmed-working
    min_start_date_day/max_start_date_day params. Adaptively halves the
    window if max_results_exceeded fires, since row volume isn't even
    week to week and a fixed chunk size isn't safe."""
    window_start = pd.Timestamp(min_date_str)
    window_end = pd.Timestamp(max_date_str)
    all_rows = []
    headers = None

    def pull_chunk(start, end):
        params = {
            "page_size": 2000,
            "min_start_date_day": start.date().isoformat(),
            "max_start_date_day": end.date().isoformat(),
        }
        for attempt in range(max_retries):
            try:
                resp = requests.get(f"{BASE_URL}/table_report_data/{REPORT_294_ID}",
                                     headers=HEADERS, params=params, timeout=30)
                resp.raise_for_status()
                return resp.json()["data"]["attributes"]
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, OSError) as e:
                wait = 2 ** attempt
                print(f"Report 294 pull {start.date()}-{end.date()}: network error ({e}), retrying in {wait}s...")
                time.sleep(wait)
        raise RuntimeError(f"Failed to fetch report 294 for {start.date()}-{end.date()} after {max_retries} attempts")

    # Start with weekly windows -- confirmed safe in validation (a full
    # week across all studios came in under 500 rows), then adaptively
    # halve any window that still exceeds the cap.
    pending = list(pd.date_range(window_start, window_end, freq="7D"))
    if pending[-1] < window_end:
        pending.append(window_end)

    for start, end in zip(pending[:-1], pending[1:]):
        stack = [(start, end)]
        while stack:
            s, e = stack.pop()
            data = pull_chunk(s, e)
            headers = data["headers"]
            if data["max_results_exceeded"] and (e - s) > pd.Timedelta(days=1):
                mid = s + (e - s) / 2
                print(f"  {s.date()}-{e.date()} exceeded 500 rows, halving window")
                stack.append((mid, e))
                stack.append((s, mid))
            else:
                if data["max_results_exceeded"]:
                    print(f"  WARNING: {s.date()}-{e.date()} still exceeded 500 rows even at minimum window size")
                all_rows.extend(data["rows"])

    df = pd.DataFrame(all_rows, columns=headers)
    return df.drop_duplicates()


def clean_sessions_df(raw_df):
    """Clean report 294 output into a typed, de-duplicated,
    exclusion-flagged DataFrame."""
    df = raw_df.copy()
    df.columns = [c.strip() for c in df.columns]

    # BUG FIX: report 294 returns Class Date in ISO format (YYYY-MM-DD,
    # unambiguous) via the API -- confirmed during validation (dates like
    # '2026-07-06' seen directly in raw API responses). An earlier version
    # of this function used dayfirst=True (copied from the old CSV-export
    # workflow, which used DD/MM/YYYY), but dayfirst=True can silently
    # swap month/day even on unambiguous ISO strings (verified: pandas
    # turned '2025-01-06' into 2025-06-01). This corrupted every date in
    # the pipeline. Do NOT add dayfirst=True back without re-confirming
    # the actual raw format first.
    df["Class Date"] = pd.to_datetime(df["Class Date"], errors="coerce")
    df = df.dropna(subset=["Class Date"])
    df["Class Day of Week"] = df["Class Day of Week"].astype(str).str.strip()

    numeric_cols = ["Checked In Reservations", "Late Cancelled Reservations", "No Showed Reservations",
                     "Admin Holds", "Unavailable Holds", "Layout Capacity", "Actual Capacity"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    df["% Utilization"] = (
        df["% Utilization"].astype(str).str.replace("%", "", regex=False).str.strip()
    )
    df["% Utilization"] = pd.to_numeric(df["% Utilization"], errors="coerce")
    if df["% Utilization"].dropna().max() is not None and df["% Utilization"].dropna().max() <= 1:
        df["% Utilization"] = df["% Utilization"] * 100

    # Admin Holds count as a paid visit -- confirmed with Remy against a
    # real example (Marylebone, 2026-06-02 18:30: 2 checked-in + 12 admin
    # holds in a 16-cap room = a genuine private class, staff manually
    # holding spots rather than a dedicated private-booking product).
    # IMPORTANT: this is a real but RARE signal -- in a ~2-month window
    # only 3 non-excluded classes had Admin Holds > 0 (all Marylebone),
    # after filtering out ~150 rows that turned out to be recurring
    # Recovery Boots holds (unrelated, already excluded by class type).
    # Won't move headline numbers much, but is accurate on the classes
    # where it applies. Unavailable Holds (broken equipment etc.)
    # deliberately NOT included -- that's lost capacity, not a paid visit.
    # FUTURE ENHANCEMENT (not built yet, flagged per Remy): eventually
    # assign an average RPV (revenue per visit) specifically to Admin
    # Hold spots, since private-class economics likely differ from a
    # standard drop-in/membership visit.
    df["Paid Visits"] = (
        df["Checked In Reservations"] + df["Late Cancelled Reservations"]
        + df["No Showed Reservations"] + df["Admin Holds"]
    )
    df["Total % Paid Visits"] = np.where(
        df["Actual Capacity"] > 0,
        100 * df["Paid Visits"] / df["Actual Capacity"],
        np.nan,
    )

    class_type_lower = df["Class Type"].astype(str).str.strip().str.lower()
    df["Is_Excluded_Class"] = class_type_lower.isin(EXCLUDED_CLASS_TYPES)

    # Defensive multi-instructor split (not observed in validation, but
    # the underlying API field is a list, so guard against it anyway).
    df["Instructors"] = df["Instructors"].fillna("").astype(str)
    df["Instructor_List"] = df["Instructors"].apply(
        lambda s: [n.strip() for n in s.split(",") if n.strip()] if s else []
    )
    df["Instructor_Count"] = df["Instructor_List"].apply(len)

    # Data-quality flag: null/blank instructor on a class type that
    # ISN'T one of our confirmed no-instructor-by-design types.
    df["Unassigned_Instructor_Flag"] = (df["Instructor_Count"] == 0) & (~df["Is_Excluded_Class"])

    return df


def pull_all_class_sessions(locations, max_retries=4):
    """Pull the FULL class_sessions table per location (the only
    reliably-working filter on this endpoint -- date filtering and
    ordering are both silently ignored, confirmed by testing). Filters
    to cancelled sessions only after the full pull, client-side."""
    all_rows = []
    for loc_name, loc_id in locations.items():
        page = 1
        while True:
            params = {"page": page, "page_size": 2000, "location": loc_id}
            payload = None
            for attempt in range(max_retries):
                try:
                    resp = requests.get(f"{BASE_URL}/class_sessions", headers=HEADERS, params=params, timeout=30)
                    resp.raise_for_status()
                    payload = resp.json()
                    break
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, OSError) as e:
                    wait = 2 ** attempt
                    print(f"class_sessions [{loc_name}] page {page}: network error ({e}), retrying in {wait}s...")
                    time.sleep(wait)
            if payload is None:
                raise RuntimeError(f"Failed to fetch class_sessions page {page} for {loc_name} after {max_retries} attempts")

            for record in payload["data"]:
                attrs = record["attributes"]
                all_rows.append({
                    "location": loc_name,
                    "start_date": attrs.get("start_date"),
                    "start_time": attrs.get("start_time"),
                    "class_type": attrs.get("class_type_display"),
                    "cancellation_datetime": attrs.get("cancellation_datetime"),
                    "instructor_names": attrs.get("instructor_names"),
                })

            total_pages = payload["meta"]["pagination"]["pages"]
            print(f"class_sessions [{loc_name}] page {page} of {total_pages}")
            if page >= total_pages:
                break
            page += 1

    df = pd.DataFrame(all_rows)
    df["start_date"] = pd.to_datetime(df["start_date"], errors="coerce")
    return df


def build_cancellations_history(sessions_df, start_date):
    """Weekly cancelled-class counts per studio, from a raw
    class_sessions pull (already location-tagged, all dates)."""
    cancelled = sessions_df[
        sessions_df["cancellation_datetime"].notna()
        & (sessions_df["start_date"] >= pd.Timestamp(start_date))
    ].copy()
    cancelled["week"] = cancelled["start_date"].dt.to_period("W-MON").apply(lambda p: p.start_time)

    out = (
        cancelled.groupby(["week", "location"])
        .size()
        .reset_index(name="cancelled_classes")
        .rename(columns={"location": "studio"})
        .sort_values(["week", "studio"])
    )
    return out


def build_class_history(sessions_df):
    """Weekly rollup per studio -- total real classes held, avg
    utilization, total classes cancelled would join in here from the
    separate cancellations tab (kept separate per the "two independent
    counts" decision -- see module docstring)."""
    real = sessions_df[~sessions_df["Is_Excluded_Class"]].copy()
    real["week"] = real["Class Date"].dt.to_period("W-MON").apply(lambda p: p.start_time)

    out = (
        real.groupby(["week", "Location"])
        .agg(
            total_classes=("Class Date", "count"),
            avg_utilization=("% Utilization", "mean"),
            avg_paid_pct=("Total % Paid Visits", "mean"),
            total_checked_in=("Checked In Reservations", "sum"),
        )
        .reset_index()
        .rename(columns={"Location": "studio"})
        .sort_values(["week", "studio"])
    )
    return out


def build_coach_scorecard(sessions_df):
    """Ported from Remy's validated Colab notebook logic: benchmark
    each class against the studio+time-slot average, compute lift,
    consistency, and rolling-window momentum per coach. Excludes
    Recovery/Full Body and any class with no real instructor
    assigned (can't score a coach who isn't there)."""
    real = sessions_df[
        (~sessions_df["Is_Excluded_Class"]) & (sessions_df["Instructor_Count"] > 0)
    ].copy()

    # One row per (session, instructor) so co-taught classes attribute
    # correctly to each instructor rather than being skipped or
    # double-counted under a combined name.
    real = real.explode("Instructor_List").rename(columns={"Instructor_List": "Instructor"})

    benchmark = (
        real.groupby(["Location", "Class Time"])
        .agg(benchmark_utilization=("% Utilization", "mean"),
             benchmark_paid_pct=("Total % Paid Visits", "mean"))
        .reset_index()
    )
    real = real.merge(benchmark, on=["Location", "Class Time"], how="left")
    real["utilization_lift"] = real["% Utilization"] - real["benchmark_utilization"]
    real["paid_pct_lift"] = real["Total % Paid Visits"] - real["benchmark_paid_pct"]

    latest_date = real["Class Date"].max()
    windows = {"14": 14, "28": 28, "56": 56}
    rows = []
    for label, n_days in windows.items():
        window_df = real[real["Class Date"] > latest_date - pd.Timedelta(days=n_days)]
        grp = (
            window_df.groupby(["Location", "Instructor"])
            .agg(
                classes_taught=("Class Date", "count"),
                avg_utilization=("% Utilization", "mean"),
                avg_paid_pct=("Total % Paid Visits", "mean"),
                avg_utilization_lift=("utilization_lift", "mean"),
                avg_paid_pct_lift=("paid_pct_lift", "mean"),
                utilization_consistency=("% Utilization", "std"),
                pct_classes_above_80=("% Utilization", lambda x: (x >= 80).mean() * 100),
                pct_classes_under_50=("% Utilization", lambda x: (x < 50).mean() * 100),
            )
            .reset_index()
        )
        grp["window_days"] = label
        rows.append(grp)

    trends = pd.concat(rows, ignore_index=True)

    pivot = trends.pivot_table(
        index=["Location", "Instructor"], columns="window_days", values="avg_utilization_lift"
    ).reset_index()
    pivot["momentum_14_vs_56"] = pivot.get("14") - pivot.get("56")

    scorecard = trends[trends["window_days"] == "28"].drop(columns=["window_days"]).copy()
    scorecard = scorecard.merge(pivot[["Location", "Instructor", "momentum_14_vs_56"]],
                                  on=["Location", "Instructor"], how="left")

    scorecard["demand_creation_score"] = (
        0.45 * scorecard["avg_utilization_lift"]
        + 0.45 * scorecard["avg_paid_pct_lift"]
        - 0.10 * scorecard["utilization_consistency"].fillna(0)
    )
    scorecard = scorecard.rename(columns={"Location": "studio"}).sort_values(
        ["studio", "demand_creation_score"], ascending=[True, False]
    )
    return scorecard


def write_to_sheet(df, tab_name, max_retries=4):
    """Same retry-wrapped overwrite pattern used everywhere else."""
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)

    df_out = df.copy()
    for col in df_out.columns:
        if pd.api.types.is_datetime64_any_dtype(df_out[col]):
            df_out[col] = df_out[col].astype(str).replace("NaT", "")
    # Guard against any leftover list-typed columns (e.g. Instructor_List
    # if it isn't dropped before writing) -- gspread can't serialize them.
    for col in df_out.columns:
        if df_out[col].apply(lambda v: isinstance(v, list)).any():
            df_out[col] = df_out[col].apply(lambda v: ", ".join(v) if isinstance(v, list) else v)

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
    end_date = date.today().isoformat()

    print(f"Pulling report 294 from {HISTORY_START} to {end_date}...")
    raw_294 = get_table_report_294(HISTORY_START, end_date)
    sessions_df = clean_sessions_df(raw_294)
    print(f"Report 294: {len(sessions_df)} real class-session rows after cleaning.")

    print("\nPulling class_sessions (all locations, for cancellation counts)...")
    locations = {"Chelsea": 48717, "Marylebone": 48750, "Shoreditch": 48783}
    raw_sessions = pull_all_class_sessions(locations)
    cancellations_df = build_cancellations_history(raw_sessions, HISTORY_START)
    print(f"class_sessions: {cancellations_df['cancelled_classes'].sum()} total cancelled classes found since {HISTORY_START}.")

    history_df = build_class_history(sessions_df)
    scorecard_df = build_coach_scorecard(sessions_df)

    # Session-level export tab: drop the helper list column before
    # writing (kept as Instructors string instead), keep the
    # data-quality flag visible.
    sessions_export = sessions_df.drop(columns=["Instructor_List"]).copy()

    if os.environ.get("DEBUG_ONLY_SKIP_SHEET_WRITE") == "true":
        print("\nDEBUG_ONLY_SKIP_SHEET_WRITE is set -- skipping the actual write.")
        print("\n--- Sessions (head) ---")
        print(sessions_export.head(20).to_string())
        print("\n--- History ---")
        print(history_df.to_string())
        print("\n--- Coach Scorecard ---")
        print(scorecard_df.to_string())
        print("\n--- Cancellations ---")
        print(cancellations_df.to_string())
        return

    write_to_sheet(sessions_export, TAB_CLASS_SESSIONS)
    write_to_sheet(history_df, TAB_CLASS_HISTORY)
    write_to_sheet(scorecard_df, TAB_COACH_SCORECARD)
    write_to_sheet(cancellations_df, TAB_CLASS_CANCELLATIONS)


if __name__ == "__main__":
    main()
