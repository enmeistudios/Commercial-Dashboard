# Enmei Commercial Dashboard

A live commercial performance dashboard for Enmei, built on Mariana Tek
data. First page: membership performance (active members, revenue by
type, cancellations) -- filterable by studio, membership type, and time
frame. Planned: class utilization and intro offer conversion pathways.

## Architecture

```
enmei-commercial-dashboard/
├── config.py                  # studio list, Google Sheet ID/tab constants
├── requirements.txt
├── scripts/
│   └── build_membership_dashboard_data.py   # pulls MT data, writes to Sheet
├── .github/workflows/
│   └── weekly_membership_dashboard_data.yml  # runs the pull weekly
└── site/
    └── index.html              # the dashboard itself -- one static file
```

**How it fits together:**
1. `build_membership_dashboard_data.py` pulls membership data from
   Mariana Tek, aggregates it (counts/revenue only -- **no client PII**),
   and writes it into the `Membership_Data` tab of a shared Google Sheet.
2. This runs automatically every Monday via GitHub Actions.
3. `site/index.html` is a single, self-contained HTML file (no build
   step, no npm, no framework) that reads that sheet live via Google's
   public `gviz` endpoint and renders the charts/filters.
4. Netlify serves `site/index.html` directly.

## ⚠️ Two things worth knowing

1. **The Google Sheet is technically public once "published to web."**
   Anyone with the Sheet ID can query it directly via the `gviz`
   endpoint, bypassing the dashboard entirely. This is why only
   aggregated counts/revenue are written -- never names, emails, or
   other client PII.
2. **Unconfirmed studio field** -- `membership_instances` doesn't have
   an obvious "which studio" field in Mariana Tek's documented API
   response. The script currently uses each member's `home_location`
   (from the `users` resource) as a proxy. Before trusting per-studio
   numbers, inspect `scripts/membership_instances_raw_sample.json`
   (generated locally each run, gitignored) and confirm this holds --
   or find a better field (e.g. via the order/order_line that created
   the membership) and update `build_snapshots()` in the script
   accordingly.

## One-time setup

**1. Google Cloud service account** (if not already done):
- Create a project at console.cloud.google.com, enable the Google Sheets API
- Create a service account, generate a JSON key, note its `client_email`

**2. Create and share the Google Sheet:**
- Create a new Google Sheet
- Share it with the service account's email, with Editor access
- File -> Share -> Publish to web (Entire Document)
- Copy the Sheet ID from the URL, confirm it matches `DASHBOARD_SHEET_ID`
  in `config.py`

**3. Add GitHub Actions secrets** (repo -> Settings -> Secrets and
variables -> Actions):

| Secret name | Value |
|---|---|
| `MARIANA_API_KEY` | Your Mariana Tek bearer token |
| `MARIANA_BASE_URL` | `https://enmei.marianatek.com/api` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full contents of the service account JSON key file |

**4. Manually trigger the workflow once** (Actions tab -> "Weekly
Membership Dashboard Data Refresh" -> Run workflow) to populate the
sheet for the first time.

**5. Deploy to Netlify:**
- Add new site -> Import an existing project -> connect this repo
- Netlify reads `netlify.toml` and publishes `site/` directly -- no
  build command needed

## Adding more dashboard pages (utilization, conversion pathways)

Same pattern: a new script that pulls + aggregates data into a new tab
in the same Google Sheet, a matching weekly GitHub Actions workflow, and
either a new page in `site/` or an additional section in `index.html`
that fetches and renders that tab.
