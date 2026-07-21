"""
config.py

Shared constants for the Enmei Commercial Dashboard project.
"""

BASE_URL = "https://enmei.marianatek.com/api"

# Studio locations, as they appear in the "home_location" / studio fields
# returned by Mariana Tek. "Shoreditch" is coming soon -- uncomment once
# it's live in Mariana Tek.
STUDIOS = [
    "Chelsea",
    "Marylebone",
    # "Shoreditch",
]

# Google Sheet used as the dashboard's data layer. Shared with the
# commercial-dashboard service account (Editor access) and published to
# web (File -> Share -> Publish to web) so site/index.html can read it
# live via the gviz endpoint.
DASHBOARD_SHEET_ID = "1Nq5xLYTw9Bx_aGXrZbAExp1h32fLwMBE6037LIHn-Gc"
DASHBOARD_MEMBERSHIP_TAB = "Membership_Data"
DASHBOARD_HISTORY_TAB = "Membership_History_Detail"
