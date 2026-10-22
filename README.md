# Aeries family grade dashboard

A static parent dashboard for Tustin USD Aeries grades, live at [the same URL](https://davidcasas82.github.io/aeries-dashboard/). A GitHub Action logs into the parent portal, then POSTs `grades_data.json` / history to `family-data`. The page unlocks with the household PIN.

Python precomputes facts (missing work, tonight’s list, urgency, trends). Official v1 glance is standing (mark / % / 7-day trend) plus Tonight as 0–3 class-level bullets; Grok only writes the per-class lines.

## Local preview

```bash
# In family-data
npm run dev

# In this repo
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m http.server 8765
```

Open http://localhost:8765/ and use the local family-data PIN (`.dev.vars`). Browsers cannot load the dashboard from a `file://` page.

## Scrape (needs a `.env`)

Copy `.env.example` to `.env` and fill in the Aeries login plus each student’s SN. To publish to the Worker, also set `FAMILY_DATA_URL` and `FAMILY_PIN`.

```bash
python scraper.py                 # full scrape + briefing
python scraper.py --grok-only     # rewrite briefings from existing JSON
python scraper.py --attendance-only
python scraper.py --rebuild-view  # dashboard view payload only
python scraper.py --publish-only  # POST existing local JSON to family-data
python scraper.py --probe-gradebook
python scraper.py --probe-gradebook-totals  # footer structure + weights only, no scores, no publish
python scraper.py --probe-attendance
```

In CI, run the footer probe from Actions → Daily Grade Scrape → Run workflow with `probe_gradebook_totals` checked.

`SUMMER_BREAK=true` pauses grades and Grok (attendance can still refresh). Leave it unset to follow `school_calendar.json`.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Schedule

Cloudflare cron on `family-data` dispatches these workflows. The afternoon scrape stays at 4:45 PM Pacific year-round: two UTC crons (`45 23 * * *` during daylight time and `45 0 * * *` during standard time) wake the Worker, and it dispatches only when local Pacific time is 4:45 PM.

- Overnight ~2:07am PT
- After school 4:45 PM PT
- Evening ~8:07pm PT

On the mini, the grade insights runner follows the 4:45 PM and 8:07 PM scrapes on school days (see Grade insights publishing below).

Actions logs print `student 1`, not names or student numbers. The Pages site is public; grades themselves are behind the PIN.

## Data files

| File | Role |
|------|------|
| `grades_data.json` | Local scrape cache (gitignored). The page reads `/v1/grades/latest`. |
| `grade_history.json` | Local history cache (gitignored). The Worker stores per-day snapshots. |
| `school_calendar.json` | First/last day + official 6–12 quarter ends + term cutovers |

## Grade insights publishing (Mac mini)

The mini's runner (`insights_runner.py`) publishes the local write-ups with `PUT /v1/grades/insights` on family-data. It never uses the PIN session for this. Its Bearer is family-data's `AERIES_INSIGHTS_SECRET`, kept in the mini's login Keychain as a generic password:

- service `family-data-insights-write`, account `aeries`
- read only when a publish is sent, never printed or logged

Publishing is off unless `GRADE_INSIGHTS_PUBLISH=1`. With it unset, the publish step records `{"published": false}` and sends nothing. The dashboard reads the write-ups with `GET /v1/grades/insights` on the same PIN session as the grades and shows them under **Grade notes**, as plain text only. Code, not the model, ends each story with the next named item's move-up step and keep sentence, and tags each kid's easiest win and biggest risk (at most one of each, only on published stories); those classes come first, with an **Easiest win** or **Biggest risk** tag after the class name.

### When it runs (trial)

`deploy/com.davidcasas.grade-insights.plist` starts the runner at 4:55 and 5:25 PM and at 8:20 and 8:50 PM, Monday to Friday, on the mini's Pacific clock. The scrapes themselves run in GitHub Actions and land on family-data (about a minute after the 4:45 PM and 8:07 PM dispatches), so nothing lands on the mini to watch. Instead each start reads the public `GET /v1/grades/freshness` stamp and goes on only for a sync that landed in the last 2 hours and hasn't been handled yet; the second start of each pair is the retry for a late scrape or a busy machine. The 2 AM scrapes are never fresh by then.

For a fresh sync the runner unlocks once with the Keychain PIN, makes its two GETs (latest grades, scored log) and fingerprints, per student, the assignments and scores, the engine's facts, the scored log, the upcoming items and the engine and writer code. If every fingerprint matches the last successful run it logs `skip: no change` and stops: no model, no PUT, the stored notes stay. Otherwise Qwen3 8B writes the write-ups and `InsightsPublisher` sends them (only with `GRADE_INSIGHTS_PUBLISH=1`, which the plist sets). The fingerprint file (`/Volumes/David C SSD/grade-insights/state/fingerprint.json`, mode 600, SHA-256 hashes only) is updated only after a publish or when there was nothing to send; a failed run leaves it alone and the next start tries again. The write-ups stay on the SSD under `grade-insights/runs/`, owner-only. A missing SSD skips the sync with one log line and keeps the last good write-up. Logs carry counts and outcomes only, never names, student numbers or scores.

### Installing the launchd job

Load it only after #57 is merged, both password steps are done (the parent PIN and the insights write secret are in the mini's login Keychain), and the PR that added this section is merged. Then, on the mini:

```bash
git clone https://github.com/davidcasas82/aeries-dashboard.git ~/code/aeries-dashboard
cp ~/code/aeries-dashboard/deploy/com.davidcasas.grade-insights.plist ~/Library/LaunchAgents/
plutil -lint ~/Library/LaunchAgents/com.davidcasas.grade-insights.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.davidcasas.grade-insights.plist
```

The plist expects the checkout at `~/code/aeries-dashboard` and the venv at `/Volumes/David C SSD/grade-insights/venv`. Logs go to `~/Library/Logs/grade-insights.log` and `grade-insights.err.log`. After the first live run, check both logs for macOS privacy (TCC) blocks on the SSD reads, such as `Operation not permitted` on a `/Volumes/David C SSD` path. If one shows up, give the venv's Python (the Homebrew python3.12 it links to) access to removable volumes, or Full Disk Access, in System Settings → Privacy & Security, and let the next start retry. To stop the trial: `launchctl bootout gui/$(id -u)/com.davidcasas.grade-insights`.
