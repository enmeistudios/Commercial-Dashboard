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

