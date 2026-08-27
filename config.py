"""
config.py

Shared constants for the Enmei Commercial Dashboard project.
"""

BASE_URL = "https://enmei.marianatek.com/api"

# Studio locations. Shoreditch is now confirmed live (real membership
# instances exist for it as of the transition to real historical data).
STUDIOS = [
    "Chelsea",
    "Marylebone",
    "Shoreditch",
]

# Google Sheet used as the dashboard's data layer. Shared with the
# commercial-dashboard service account (Editor access) and published to
# web (File -> Share -> Publish to web) so docs/index.html can read it
# live via the gviz endpoint.
DASHBOARD_SHEET_ID = "1Nq5xLYTw9Bx_aGXrZbAExp1h32fLwMBE6037LIHn-Gc"

# Tab names -- see build_membership_dashboard_data.py for what each contains.
# All three are fully overwritten on every run (not appended to), since
# real history is reconstructed from source data each time, not
# accumulated snapshot-by-snapshot.
TAB_LIVE_STATUS = "Membership_Live_Status"
TAB_HISTORY = "Membership_History"
TAB_CUSTOMER_LOG = "Membership_Customer_Log"
TAB_AGE_PYRAMID = "Membership_Age_Pyramid"

TAB_INTRO_LIVE = "IntroOffers_Live"
TAB_INTRO_HISTORY = "IntroOffers_History"
TAB_INTRO_FLOW = "IntroOffers_Flow"
TAB_INTRO_ATTENDANCE = "IntroOffers_Attendance"
TAB_INTRO_STARTERS = "IntroOffers_Starters"
TAB_INTRO_EXPIRING = "IntroOffers_Expiring"

TAB_SUMMER_STRONG_PERFORMANCE = "SummerStrong_Performance"
TAB_SUMMER_STRONG_EXPIRING = "SummerStrong_Expiring"
# Per-customer detail, ID-only (no names) -- same treatment as
# Membership_Customer_Log and the At Risk/Term Ending list. Added because
# Summer Strong, unlike 1 Week Unlimited/Welcome 3, has no existing
# outreach automation sending a real client list -- without this, there
# was genuinely no way to know who to reach out to before it expires.
TAB_SUMMER_STRONG_EXPIRING_DETAIL = "SummerStrong_Expiring_Detail"

# Class Performance -- see build_class_performance_dashboard_data.py.
# TAB_CLASS_SESSIONS is the granular ID-only per-class-session log (mirrors
# the IntroOffers_Starters pattern: full rows, all filtering done
# client-side in the dashboard so studio/timeframe/coach toggles all work
# off one table). TAB_CLASS_HISTORY is a weekly rollup for trend charts.
# TAB_COACH_SCORECARD is one row per coach x studio x rolling window.
TAB_CLASS_SESSIONS = "ClassPerformance_Sessions"
TAB_CLASS_HISTORY = "ClassPerformance_History"
TAB_COACH_SCORECARD = "ClassPerformance_CoachScorecard"
TAB_CLASS_CANCELLATIONS = "ClassPerformance_Cancellations"

