"""
Class Performance -- RAW-DATA version.

Rebuilds the exact same session table that report 294 ("Class Session
Utilization Details") produces, but from Mariana Tek's raw endpoints
(class_sessions, reservations, spot_holds), so the dashboard no longer
depends on the pre-built table reports. Everything downstream (cleaning,
weekly history, coach scorecard, cancellations) is REUSED, unchanged,
from build_class_performance_dashboard_data.py -- only the source of the
session table is new.

Writes to separate ClassPerformanceRaw_* tabs, so the live dashboard is
untouched until the two versions have been compared and trusted. A
comparison against report 294 is written to ClassPerformanceRaw_Compare*
(skipped automatically once report access is gone).

HOW EACH REPORT COLUMN IS REBUILT (validated against report 294 on a full
week of Marylebone: 86 report rows vs 86 raw sessions, all matched):
  Checked In Reservations      = reservations with status "check in"        (100% match)
  Late Cancelled Reservations  = status "penalty cancel"                    (100% match)
  No Showed Reservations       = status "penalty no show"                   (100% match)
      "Graced" cancels / no-shows are fee-waived, so the report does NOT
      count them; "standard cancel" (before the late window), "removed",
      "class cancelled" and "no fee no show" are not paid visits either.
  Admin Holds                  = spot_holds of type "admin" that have NOT
                                 been released (release_date empty)         (100% match)
  Unavailable Holds            = unreleased spot_holds of type "unavailable"
  Layout / Actual Capacity     = class_sessions.capacity                    (100% match)
  % Utilization                = checked in / capacity   (verified in the comparison tab)

ADMIN HOLDS AS PAID VISITS: the raw "Admin Holds" column matches the report
exactly, but only holds covering MORE THAN HALF the room count toward Paid
Visits (shared rule in clean_sessions_df -- the report-based pipeline uses
the same one). Smaller counts are mostly waitlist-alert holds placed when a
late cancel opens a spot in a full class: not paid visits, and they can
double-count the late cancel itself.

EXTRA COLUMNS (not in the report; added for the "revisit later" docket):
  Waived Late Cancels / Waived No Shows = graced cancels / graced no-shows
        (fees waived) -- a possible measure of how often fees are waived.

Required environment variables:
    MARIANA_API_KEY, MARIANA_BASE_URL, GOOGLE_SERVICE_ACCOUNT_JSON
Optional:
    CP_RAW_HISTORY_START   override the start date (default: same as the report version)
    CP_RAW_PAGE_SIZE       page size for raw endpoints (default 500)
    CP_COMPARE_DAYS        days to compare against report 294 (default 56; 0 disables)
    DEBUG_ONLY_SKIP_SHEET_WRITE=true   print instead of writing to the Sheet
"""

import os
import sys
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
import build_class_performance_dashboard_data as base  # noqa: E402
from config import (  # noqa: E402
    LOCATIONS, INSTRUCTOR_NAME_OVERRIDES,
    TAB_CLASS_SESSIONS_RAW, TAB_CLASS_HISTORY_RAW, TAB_COACH_SCORECARD_RAW,
    TAB_CLASS_CANCELLATIONS_RAW, TAB_CLASS_COMPARE_SUMMARY, TAB_CLASS_COMPARE_DETAIL,
)

# Every reservation status we know about. Anything else seen in the data is
# reported loudly, because an unrecognised status would silently count as
# zero toward paid visits (e.g. if Mariana Tek once used a different name
# for a late cancel).
KNOWN_STATUSES = {
    "check in", "standard cancel", "graced cancel", "penalty cancel",
    "penalty no show", "graced no show", "no fee no show",
    "removed", "class cancelled", "pending",
}

PAGE_SIZE = int(os.environ.get("CP_RAW_PAGE_SIZE", "500"))
WORKERS = int(os.environ.get("CP_RAW_WORKERS", "4"))   # studios pulled in parallel

# Reservation status -> what it means for the paid-visit logic.
STATUS_CHECKED_IN = "check in"
STATUS_LATE_CANCEL = "penalty cancel"
STATUS_NO_SHOW = "penalty no show"
STATUS_LATE_CANCEL_WAIVED = "graced cancel"
STATUS_NO_SHOW_WAIVED = "graced no show"


# --------------------------------------------------------------------------
# API helpers
# --------------------------------------------------------------------------
def api_get(resource, params, max_retries=6):
    """GET with retries for network errors, 429 (honouring Retry-After) and 5xx."""
    url = f"{base.BASE_URL}/{resource}"
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=base.HEADERS, params=params, timeout=60)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, OSError) as e:
            wait = 2 ** attempt
            print(f"  {resource}: network error ({e}), retrying in {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2 ** attempt))
            print(f"  {resource}: rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            wait = 2 ** attempt
            print(f"  {resource}: HTTP {resp.status_code}, retrying in {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"Failed to fetch {resource} {params} after {max_retries} attempts")


def pull_pages(resource, params, label=""):
    """Yield every record of a paginated endpoint."""
    page = 1
    while True:
        payload = api_get(resource, {**params, "page": page, "page_size": PAGE_SIZE})
        data = payload.get("data", [])
        yield from data
        pagination = (payload.get("meta") or {}).get("pagination") or {}
        total_pages = pagination.get("pages")
        if total_pages is not None:
            if page >= total_pages:
                return
        elif len(data) < PAGE_SIZE:
            return
        page += 1


def rel_id(record, name):
    """ID of a to-one relationship, or None."""
    data = ((record.get("relationships") or {}).get(name) or {}).get("data")
    if isinstance(data, dict):
        return data.get("id")
    return None


def rel_ids(record, name):
    """IDs of a to-many relationship (empty list if absent)."""
    data = ((record.get("relationships") or {}).get(name) or {}).get("data")
    if isinstance(data, list):
        return [d.get("id") for d in data if isinstance(d, dict) and d.get("id") is not None]
    if isinstance(data, dict) and data.get("id") is not None:
        return [data["id"]]
    return []


def month_windows(start, end):
    """Inclusive (start, end) ISO date windows, one per calendar month."""
    cur, last = pd.Timestamp(start), pd.Timestamp(end)
    while cur <= last:
        win_end = min(cur + pd.offsets.MonthEnd(0), last)
        yield cur.date().isoformat(), win_end.date().isoformat()
        cur = win_end + pd.Timedelta(days=1)


# --------------------------------------------------------------------------
# Lookups and holds
# --------------------------------------------------------------------------
def load_class_categories():
    """class_session_type id -> category name (report's 'Class Category').
    Not used by any metric today; failure here must not stop the run."""
    try:
        cat_names = {c["id"]: c["attributes"].get("name", "")
                     for c in pull_pages("class_session_categories", {})}
        out = {}
        for t in pull_pages("class_session_types", {}):
            cat_id = rel_id(t, "class_session_category") or t["attributes"].get("class_session_category")
            out[t["id"]] = cat_names.get(str(cat_id), cat_names.get(cat_id, ""))
        return out
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: couldn't load class categories ({e}); 'Class Category' left blank.")
        return {}


def load_instructor_display_names():
    """employee_public_profile id -> schedule_display_name.
    Report 294 shows coaches by the display name set on their schedule
    profile ('Sam', 'Tammy', 'Olivia A.', 'Recovery Boots'), NOT by their
    legal first name -- so nicknames and two coaches sharing a first name
    come out right when we use the same field."""
    out = {}
    try:
        for p in pull_pages("employee_public_profiles", {}):
            name = (p["attributes"].get("schedule_display_name") or "").strip()
            if name:
                out[p["id"]] = name
        print(f"Loaded {len(out)} coach display names.")
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: couldn't load coach display names ({e}); falling back to the first-name rule.")
    return out


def load_holds():
    """Unreleased admin / unavailable holds, counted per class. The report
    counts only holds that have not been released (validated: 100% match),
    and released holds stay in the data with a release_date, so nothing is
    lost."""
    active_admin = Counter()
    active_unavailable = Counter()
    for hold_type, bucket in (("admin", active_admin), ("unavailable", active_unavailable)):
        for h in pull_pages("spot_holds", {"hold_type": hold_type}):
            cs = rel_id(h, "class_session")
            if cs is not None and not h["attributes"].get("release_date"):
                bucket[cs] += 1
    print(f"Holds loaded: {sum(active_admin.values())} unreleased admin, {sum(active_unavailable.values())} unreleased unavailable.")
    return active_admin, active_unavailable


# --------------------------------------------------------------------------
# Per-location pulls
# --------------------------------------------------------------------------
def pull_sessions(loc_id, start, end, label=""):
    """All class sessions (held AND cancelled) for one location. Date
    filters work on this endpoint (min_date / max_date); validated."""
    seen = {}
    for w_start, w_end in month_windows(start, end):
        for rec in pull_pages("class_sessions", {"location": loc_id, "min_date": w_start, "max_date": w_end}):
            seen[rec["id"]] = rec
    print(f"  [{label}] class_sessions: {len(seen)} pulled ({start} -> {end})", flush=True)
    return list(seen.values())


def aggregate_reservations(loc_id, start, end, label=""):
    """Stream reservations and keep only per-class status counters (never
    the rows themselves). Pulled by class date + location, then counted
    client-side -- the reservations 'status' filter is not reliable."""
    counts = defaultdict(Counter)          # class id -> Counter of status keys
    n = 0
    for w_start, w_end in month_windows(start, end):
        for rec in pull_pages("reservations", {
            "class_session_location": loc_id,
            "class_session_min_date": w_start,
            "class_session_max_date": w_end,
        }):
            n += 1
            at = rec["attributes"]
            cs = rel_id(rec, "class_session")
            if cs is None:
                continue
            status = at.get("status")
            counts[cs][status] += 1
            if status == "pending":
                counts[cs][f"pending:{at.get('reservation_type')}"] += 1
        print(f"  [{label}] reservations through {w_end}: {n} so far", flush=True)
    print(f"  [{label}] {n} reservations aggregated.", flush=True)
    return counts


# --------------------------------------------------------------------------
# Build the report-shaped session table
# --------------------------------------------------------------------------
def first_name(full_name):
    """FALLBACK only (used when a class has no schedule-profile link):
    first name, with exceptions from INSTRUCTOR_NAME_OVERRIDES in config.py
    (keyed by full name or by first name)."""
    full_name = str(full_name).strip()
    if not full_name:
        return ""
    first = full_name.split()[0]
    return INSTRUCTOR_NAME_OVERRIDES.get(full_name) or INSTRUCTOR_NAME_OVERRIDES.get(first) or first


def instructors_text(value):
    if value is None:
        return ""
    parts = list(value) if isinstance(value, (list, tuple)) else str(value).split(",")
    return ", ".join(first_name(p) for p in parts if str(p).strip())


def build_raw_frames(start, end):
    categories = load_class_categories()
    display_names = load_instructor_display_names()
    name_source = Counter()
    active_admin, active_unavailable = load_holds()

    def fetch_location(loc_name, loc_id):
        t0 = time.time()
        recs = pull_sessions(loc_id, start, end, loc_name)
        counts = aggregate_reservations(loc_id, start, end, loc_name)
        print(f"[{loc_name}] finished in {time.time() - t0:.0f}s", flush=True)
        return loc_name, recs, counts

    workers = max(1, min(WORKERS, len(LOCATIONS)))
    print(f"\nPulling {len(LOCATIONS)} studios with {workers} in parallel ({start} -> {end}) ...", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(fetch_location, n, i) for n, i in LOCATIONS.items()]
        fetched = [f.result() for f in futures]          # keeps studio order; re-raises any error

    session_rows, cancelled_rows = [], []
    for loc_name, recs, counts in fetched:
        print(f"[{loc_name}] {len(recs)} sessions ({sum(1 for r in recs if r['attributes'].get('cancellation_datetime'))} cancelled)")

        for rec in recs:
            at = rec["attributes"]
            if at.get("cancellation_datetime"):
                cancelled_rows.append({
                    "location": loc_name, "start_date": at.get("start_date"),
                    "start_time": at.get("start_time"), "class_type": at.get("class_type_display"),
                    "cancellation_datetime": at.get("cancellation_datetime"),
                    "instructor_names": at.get("instructor_names"),
                })
                continue

            cs = rec["id"]
            c = counts.get(cs, Counter())
            capacity = at.get("capacity") or 0
            checked_in = c[STATUS_CHECKED_IN]
            sub = (rec.get("relationships") or {}).get("substituted_instructors", {}).get("data")
            profile_ids = rel_ids(rec, "employee_public_profiles")
            shown = [display_names[i] for i in profile_ids if i in display_names]
            if profile_ids and len(shown) == len(profile_ids):
                instructors = ", ".join(shown)
                name_source["schedule display name"] += 1
            else:
                instructors = instructors_text(at.get("instructor_names"))
                name_source["first-name fallback"] += 1
            start_date = at.get("start_date")
            session_rows.append({
                "Location": loc_name,
                "Class Date": start_date,
                "Class Time": at.get("start_time"),
                "Class Day of Week": pd.Timestamp(start_date).day_name() if start_date else "",
                "Instructors": instructors,
                "Has Substitute?": bool(sub),
                "Classroom": at.get("classroom_display"),
                "Class Tags": "",
                "Class Is Free?": bool(at.get("is_free_class")),
                "Class Type": at.get("class_type_display"),
                "Class Category": categories.get(rel_id(rec, "class_session_type"), ""),
                "Pending Standard Reservations": c["pending:standard"],
                "Pending Standby Reservations": c["pending:standby"],
                "Pending Waitlist Reservations": c["pending:waitlist"],
                "Checked In Reservations": checked_in,
                "Late Cancelled Reservations": c[STATUS_LATE_CANCEL],
                "No Showed Reservations": c[STATUS_NO_SHOW],
                "Admin Holds": active_admin.get(cs, 0),
                "Unavailable Holds": active_unavailable.get(cs, 0),
                "Layout Capacity": capacity,
                "Actual Capacity": capacity,
                "% Utilization": round(100 * checked_in / capacity, 1) if capacity else np.nan,
                # extras, not in report 294
                "Waived Late Cancels": c[STATUS_LATE_CANCEL_WAIVED],
                "Waived No Shows": c[STATUS_NO_SHOW_WAIVED],
            })

    print(f"Instructor names came from: {dict(name_source)}")

    status_totals = Counter()
    for _, _, counts in fetched:
        for per_class in counts.values():
            status_totals.update({k: v for k, v in per_class.items() if not str(k).startswith("pending:")})
    print("\nReservation statuses seen (count):")
    for st, n in status_totals.most_common():
        print(f"  {str(st):<20} {n:>8}{'' if st in KNOWN_STATUSES else '   <-- UNKNOWN STATUS'}")
    unknown = {k: v for k, v in status_totals.items() if k not in KNOWN_STATUSES}
    if unknown:
        print(f"\n!!! WARNING: {len(unknown)} reservation status(es) not recognised: {unknown}. "
              f"They are NOT counted as paid visits. Tell Claude so they can be classified.")
    raw_sessions = pd.DataFrame(session_rows)
    cancelled = pd.DataFrame(cancelled_rows)
    if len(cancelled):
        cancelled["start_date"] = pd.to_datetime(cancelled["start_date"], errors="coerce")
    else:
        cancelled = pd.DataFrame(columns=["location", "start_date", "cancellation_datetime"])
        cancelled["start_date"] = pd.to_datetime(cancelled["start_date"])
    return raw_sessions, cancelled


# --------------------------------------------------------------------------
# Comparison against report 294 (while report access still exists)
# --------------------------------------------------------------------------
COMPARE_COLUMNS = [
    "Checked In Reservations", "Late Cancelled Reservations", "No Showed Reservations",
    "Admin Holds", "Admin Holds Counted", "Unavailable Holds", "Layout Capacity", "Actual Capacity",
    "% Utilization", "Paid Visits", "Instructors", "Class Type",
]


def compare_to_report(raw_clean, days):
    """Class-by-class comparison over the last `days` days. Returns
    (summary_df, detail_df)."""
    end = date.today() - timedelta(days=1)          # today's classes are still in progress
    start = max(end - timedelta(days=days - 1), date.fromisoformat(base.HISTORY_START))
    print(f"\nComparing against report 294 for {start} -> {end} ...")
    try:
        rep = base.clean_sessions_df(base.get_table_report_294(start.isoformat(), end.isoformat()))
    except Exception as e:  # noqa: BLE001
        msg = f"Report 294 unavailable ({type(e).__name__}: {e}) -- comparison skipped."
        print(msg)
        return pd.DataFrame([{"metric": "status", "value": msg}]), pd.DataFrame()

    def key(df):
        out = df.copy()
        out["_date"] = pd.to_datetime(out["Class Date"]).dt.strftime("%Y-%m-%d")
        out["_time"] = out["Class Time"].astype(str).str[:5]
        out["_key"] = out["Location"].astype(str) + "|" + out["_date"] + "|" + out["_time"] + "|" + out["Classroom"].astype(str)
        return out.drop_duplicates("_key")

    rep = key(rep)
    raw = key(raw_clean[(raw_clean["Class Date"] >= pd.Timestamp(start)) & (raw_clean["Class Date"] <= pd.Timestamp(end))])
    m = rep.merge(raw, on="_key", how="outer", suffixes=("_report", "_raw"), indicator=True)

    both = m[m["_merge"] == "both"].copy()
    rows = [
        {"metric": "window", "value": f"{start} to {end}"},
        {"metric": "rows in report", "value": int((m["_merge"] != "right_only").sum())},
        {"metric": "rows in raw", "value": int((m["_merge"] != "left_only").sum())},
        {"metric": "matched classes", "value": len(both)},
        {"metric": "only in report", "value": int((m["_merge"] == "left_only").sum())},
        {"metric": "only in raw", "value": int((m["_merge"] == "right_only").sum())},
    ]
    # Excluded class types (Recovery boots, Full Body) never feed a metric, and
    # report 294 leaves their utilization blank, so they are left out of the
    # utilization and instructor comparisons.
    excl = both["Class Type_raw"].astype(str).str.strip().str.lower().isin(base.EXCLUDED_CLASS_TYPES)
    rows.append({"metric": "excluded class types in window (not compared on utilization / instructors)", "value": int(excl.sum())})
    diff_flags = pd.DataFrame(index=both.index)
    for col in COMPARE_COLUMNS:
        a, b = both[f"{col}_report"], both[f"{col}_raw"]
        if col in ("Instructors", "Class Type"):
            same = a.astype(str).str.strip().str.lower() == b.astype(str).str.strip().str.lower()
        else:
            same = np.isclose(pd.to_numeric(a, errors="coerce"), pd.to_numeric(b, errors="coerce"),
                              atol=0.11, equal_nan=True)
        same = pd.Series(same, index=both.index)
        if col in ("% Utilization", "Instructors"):
            same = same | excl
        diff_flags[col] = ~same
        rows.append({"metric": f"match % -- {col}", "value": f"{100 * same.mean():.1f}%" if len(both) else "n/a"})

    # Show the actual disagreements in the log, not just percentages.
    for _, r in both.loc[diff_flags["% Utilization"]].head(12).iterrows():
        rows.append({"metric": f"UTIL MISMATCH {r['_key']}",
                     "value": (f"report {r['% Utilization_report']} vs raw {r['% Utilization_raw']} | checked in "
                               f"{r['Checked In Reservations_raw']}, capacity {r['Actual Capacity_raw']}, "
                               f"class type {r['Class Type_raw']}")})
    name_pairs = both.loc[diff_flags["Instructors"], ["Instructors_report", "Instructors_raw"]].value_counts().head(12)
    for (rep_name, raw_name), n in name_pairs.items():
        rows.append({"metric": f"INSTRUCTOR NAME DIFFERS: report '{rep_name}' vs raw '{raw_name}'", "value": int(n)})

    # How the majority-of-the-room rule behaves in this window:
    has_holds = raw["Admin Holds"] > 0
    counted = raw["Admin Holds Counted"] > 0
    rows.append({"metric": "classes with any admin hold", "value": int(has_holds.sum())})
    rows.append({"metric": "...counted as private classes (holds > half the room)", "value": int(counted.sum())})
    rows.append({"metric": "...ignored as waitlist-style holds (not counted as paid visits)", "value": int((has_holds & ~counted).sum())})
    rows.append({"metric": "waived late cancels in window", "value": int(raw["Waived Late Cancels"].sum())})
    rows.append({"metric": "waived no shows in window", "value": int(raw["Waived No Shows"].sum())})

    detail_cols = ["_key"] + [f"{c}_{s}" for c in COMPARE_COLUMNS for s in ("report", "raw")]
    detail = both.loc[diff_flags.any(axis=1), detail_cols].copy()
    detail.insert(1, "differs_in", diff_flags.loc[detail.index].apply(
        lambda r: ", ".join(c for c in COMPARE_COLUMNS if r[c]), axis=1))
    # Classes that differ in something OTHER than just instructor names come first.
    detail = detail.assign(_only_names=(detail["differs_in"] == "Instructors")).sort_values("_only_names").drop(columns="_only_names")
    detail = detail.rename(columns={"_key": "class (location|date|time|room)"})
    unmatched = m[m["_merge"] != "both"][["_key", "_merge"]].rename(
        columns={"_key": "class (location|date|time|room)", "_merge": "present_in"})
    unmatched["present_in"] = unmatched["present_in"].map({"left_only": "report only", "right_only": "raw only"})
    detail = pd.concat([unmatched, detail], ignore_index=True).head(3000)
    return pd.DataFrame(rows), detail


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    end_date = date.today().isoformat()
    custom_start = os.environ.get("CP_RAW_HISTORY_START", "").strip()
    start_date = custom_start or base.HISTORY_START
    compare_days = int(os.environ.get("CP_COMPARE_DAYS", "56"))

    raw_sessions, cancelled = build_raw_frames(start_date, end_date)
    sessions_df = base.clean_sessions_df(raw_sessions)
    print(f"\nRaw build: {len(sessions_df)} real class-session rows after cleaning.")

    cancellations_df = base.build_cancellations_history(cancelled, start_date)
    print(f"Raw build: {int(cancellations_df['cancelled_classes'].sum()) if len(cancellations_df) else 0} cancelled classes since {start_date}.")
    history_df = base.build_class_history(sessions_df)
    scorecard_df = base.build_coach_scorecard(sessions_df)
    sessions_export = sessions_df.drop(columns=["Instructor_List"]).copy()

    summary_df, detail_df = (pd.DataFrame(), pd.DataFrame())
    if compare_days > 0:
        summary_df, detail_df = compare_to_report(sessions_df, compare_days)
        print("\n--- Comparison summary ---")
        print(summary_df.to_string(index=False))

    if custom_start and os.environ.get("DEBUG_ONLY_SKIP_SHEET_WRITE") != "true":
        sys.exit(f"Refusing to write: this run used a shortened history start ({custom_start}) and would overwrite "
                 f"the Sheet tabs with partial history. Re-run with debug_only=true, or leave history_start blank.")

    if os.environ.get("DEBUG_ONLY_SKIP_SHEET_WRITE") == "true":
        print("\nDEBUG_ONLY_SKIP_SHEET_WRITE is set -- skipping the actual write.")
        print("\n--- Sessions (head) ---")
        print(sessions_export.head(15).to_string())
        print("\n--- History ---")
        print(history_df.tail(12).to_string())
        print("\n--- Cancellations ---")
        print(cancellations_df.tail(12).to_string())
        return

    base.write_to_sheet(sessions_export, TAB_CLASS_SESSIONS_RAW)
    base.write_to_sheet(history_df, TAB_CLASS_HISTORY_RAW)
    base.write_to_sheet(scorecard_df, TAB_COACH_SCORECARD_RAW)
    base.write_to_sheet(cancellations_df, TAB_CLASS_CANCELLATIONS_RAW)
    if len(summary_df):
        base.write_to_sheet(summary_df, TAB_CLASS_COMPARE_SUMMARY)
    if len(detail_df):
        base.write_to_sheet(detail_df, TAB_CLASS_COMPARE_DETAIL)


if __name__ == "__main__":
    main()
