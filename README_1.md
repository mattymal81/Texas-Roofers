# IGA Lead Scraper — Roofers (Texas, v1)

## Runs on GitHub — no local Python install needed

This scrapes via a GitHub Actions workflow: you click a button on github.com, GitHub spins up a temporary Linux machine, runs the script there (with normal internet access, unlike Claude's own cloud workspace), and commits the resulting CSVs back into your repo. Nothing to install on your own computer.

## One-time setup

1. **Create the repo.** On github.com, click "New repository" (top right → the `+` icon). Name it something like `roofer-lead-scraper`. Private is fine. Create it empty (no README needed, this ships one).

2. **Upload these files**, preserving the folder structure exactly:
   - `roofer_scraper.py` → repo root
   - `README.md` → repo root
   - `.github/workflows/scrape.yml` → must stay under that exact `.github/workflows/` folder path

   Easiest way: on the repo's github.com page, click "Add file" → "Upload files," drag in `roofer_scraper.py` and `README.md`, commit. Then click "Add file" → "Create new file," type `.github/workflows/scrape.yml` as the filename (GitHub auto-creates the folders from the slashes), paste the workflow file's contents, and commit.

3. **Add your API keys as GitHub Secrets** (this keeps them out of the code, never exposed in logs):
   - Go to the repo → Settings → Secrets and variables → Actions → "New repository secret"
   - Add one named `GOOGLE_PLACES_API_KEY` with your Places API key as the value
   - Add `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` (optional — enables mobile/landline verification on every phone number). Sign up at twilio.com (a trial account works fine for Lookup), grab the Account SID and Auth Token from your Twilio Console dashboard, add both as secrets.

   Since you shared your Places key in chat earlier, it's worth regenerating/restricting it in Google Cloud Console before pasting it in as the secret, just to be safe.

4. Make sure the **Places API** (and ideally **Places API (New)**) is enabled on that Google Cloud project, with billing enabled — Places Text Search and Place Details both draw from your monthly free credit, then billed pay-as-you-go.

## Running it

Go to the repo's **Actions** tab → click "Scrape Roofer Leads" in the left sidebar → click the **"Run workflow"** button → confirm. It takes a few minutes; refresh to watch progress. When it finishes, the CSVs will be committed straight into the repo's file list, and you can view or download them from there.

The workflow only runs when you click that button — it won't run on a schedule unless you ask me to add one later.

Under the hood, each run:
- Tile a 50-mile radius around The Woodlands, TX with overlapping search circles (Google caps each search at ~31 miles, so we grid it)
- Search for roofing contractors/companies/repair/residential/commercial roofing across that grid
- De-duplicate by Google's place_id
- Pull details (phone, website, rating, review count) for every match
- Verify each phone number's line type (mobile / landline / voip) via Twilio Lookup if `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` are set — new `phone_line_type` and `is_mobile` columns tell you which numbers are actually cell phones worth voicemail-dropping vs. office landlines
- Score each lead 0–100 by **opportunity** — the businesses with the weakest existing web presence (no website, low/no rating, few reviews) rank at the top, since those are your best cold-call targets, not the ones who already have great marketing
- Write two files:
  - `roofer_leads_YYYY-MM-DD.csv` — this run only
  - `roofer_leads_master.csv` — running master list, new leads appended, duplicates skipped by place_id

Both are plain CSVs — ready to import into GHL directly.

## Getting it into Google Sheets

Easiest path for now: download `roofer_leads_master.csv` from the repo → open your master Google Sheet → File → Import → upload it → "Append to current sheet." Or send the CSV back to Claude in a future session and ask it to push the rows into your Sheet via the Google Drive connector — that part *can* run from a Claude session since it goes through Anthropic's connector infrastructure rather than direct internet access.

## Tuning knobs (top of the script)

- `RADIUS_MILES` — currently 50; bump this when you expand statewide
- `CENTER_LAT` / `CENTER_LNG` — currently The Woodlands, TX; change or add more centers as you expand
- `SEARCH_TERMS` — the phrases searched against Places; add more if you're missing roofers who describe themselves differently (e.g. "storm damage roof repair", "metal roofing")
- `GRID_STEP_MILES` / `SUB_RADIUS_METERS` — grid density; smaller step = more thorough but more API calls (cost)

## Cost awareness

Each grid point × search term is one Text Search call (up to 3 pages/20 results each), plus one Details call per unique business found. At the default grid density (~50-mile radius, 20-mile grid step, 5 search terms) expect roughly 100–200 Places API calls per full run. Check Google Cloud's current Places API pricing before running this repeatedly — costs can add up if you re-run the full grid often instead of just checking for new leads.

Twilio Lookup with line-type intelligence costs about $0.005 per phone number checked (check Twilio's current pricing) — for ~300 leads that's roughly $1.50 per full run. It's skipped entirely (free) if you don't set the Twilio secrets.
