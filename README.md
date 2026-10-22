# Aeries family grade dashboard

A static parent dashboard for Tustin USD Aeries grades, live at [the same URL](https://davidcasas82.github.io/aeries-dashboard/). A GitHub Action logs into the parent portal, then POSTs `grades_data.json` / history to `family-data`. The page unlocks with the household PIN.

Python precomputes facts (missing work, tonight’s list, urgency, trends). Official v1 glance is standing (mark / % / 7-day trend) plus Tonight as 0–3 class-level bullets. The scraper does not call xAI; the old Grok briefing is retired.

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
python scraper.py                 # full scrape + dashboard view
python scraper.py --attendance-only
python scraper.py --rebuild-view  # dashboard view payload only
python scraper.py --publish-only  # POST existing local JSON to family-data
python scraper.py --probe-gradebook
python scraper.py --probe-gradebook-totals  # footer structure + weights only, no scores, no publish
python scraper.py --probe-attendance
```

In CI, run the footer probe from Actions → Daily Grade Scrape → Run workflow with `probe_gradebook_totals` checked.

`SUMMER_BREAK=true` pauses grades (attendance can still refresh). Leave it unset to follow `school_calendar.json`.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Schedule

Cloudflare cron on `family-data` dispatches these workflows. The afternoon scrape stays at 4:45 PM Pacific year-round: two UTC crons (`45 23 * * *` during daylight time and `45 0 * * *` during standard time) wake the Worker, and it dispatches only when local Pacific time is 4:45 PM.

- Overnight ~2:07am PT
- After school 4:45 PM PT
- Evening ~8:07pm PT

On the mini, the grade insights runner follows the after-school and evening scrapes on school days (see "When it runs (trial)" below).

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

Publishing is off unless `GRADE_INSIGHTS_PUBLISH=1`. With it unset, the publish step records `{"published": false}` and sends nothing. The dashboard reads the write-ups with `GET /v1/grades/insights` on the same PIN session as the grades and shows them under **Grade notes**, as plain text only. Every class with an opener gets a story. A gated class (a scale break, or a rebuilt grade that doesn't match the posted one) gets at most its move opener, with no keep, step or drag lines; with no move since the last snapshot it gets no story. When the posted letter and the rebuilt grade's letter differ, the story likewise stops at the opener. Code writes every sentence; there is no model. A quiet class still gets code's story. The opener names the letter the dashboard shows (the Aeries mark, else the percent's letter). The engine works out that letter for the last snapshot and for now once, and the opener and its checks use only those two: "Still a B" exactly when they match, and otherwise "Now a B" or the crossing ("Slipped to a B", "Up to a B"). The crossing verb is used only when the shown letter and the percent moved the same way; a mark that moved against the percent opens "Now an A, down 1.4 since Oct 14." The checker holds every sentence to the same direction ("slipped to an A" fails when the letter went up). When the mark's letter isn't the percent's, the opener states no gap to the next letter ("Still an A, down 0.7 since Oct 14."). The facts are put in one canonical order where they enter the writer, so the same facts in any order give the same bytes. Code ends it with the next item's move-up step and keep sentence: the next named item's, or, when nothing is posted in a category, the same at the size of that category's past items, said as an assumption ("If the next test is worth about 50 points like the last ones, ..."; "a perfect 10" when it takes full points). There's no assumed line for a category whose past sizes vary more than 2x, and the engine marks such a keep so it is never assumed. Assumed lines go heaviest category first (one with no weight last), then by category name, then by fact id. On a class with no recent move, code also names the past item pulling the grade down most, only when that item really costs the most points and isn't a zero, and on an A only within 2 points of the B line. A story over 600 characters drops that line first, then its second assumed line. Code tags each kid's easiest win and biggest risk (at most one of each, only on a story that says that step or keep). Named items rank ahead of assumed ones across all the kid's classes, then the needed score decides. Those classes come first, with an **Easiest win** or **Biggest risk** tag after the class name.

### When it runs (trial)

`deploy/com.davidcasas.grade-insights.plist` starts the runner at 4:55 and 5:25 PM and at 8:20 and 8:50 PM, Monday to Friday, on the mini's Pacific clock. The scrapes themselves run in GitHub Actions and land on family-data a couple of minutes after dispatch, so nothing lands on the mini to watch. The after-school scrape is dispatched at 4:45 PM Pacific all year; the evening scrape's cron is fixed in UTC (03:07 UTC), so it lands at about 8:08 PM in daylight time and about 7:08 PM in standard time (from Nov 1). Each start reads the public `GET /v1/grades/freshness` stamp and goes on only for a sync that landed in the last 2 hours and hasn't been handled yet, which both evening starts are in either season. The second start of each pair is the retry for a late scrape or a missing SSD. The 2 AM scrapes are never fresh by then.

For a fresh sync the runner unlocks with the Keychain PIN, makes its two GETs (latest grades, scored log) and fingerprints, per student, the assignments and scores, the engine's facts, the scored log, the upcoming items and the engine and writer code (lists sorted first, so a reorder is no change). It unlocks at most twice per run: once, and once more only if a GET answers 401 because the session expired; each unlock reads the PIN from the Keychain, and a 401 after the second unlock fails the run. If every fingerprint matches the last published one it logs `skip: no change` and stops: no write-up, no PUT, the stored notes stay.

Otherwise code writes only the students whose fingerprint changed (or who have no entry in the last published doc), and `InsightsPublisher` sends the whole doc (only with `GRADE_INSIGHTS_PUBLISH=1`, which the plist sets). The Worker replaces the whole doc on every PUT, and no notes beat wrong numbers, so the PUT carries:

- fresh write-ups for the changed students. A changed student whose write-up comes out empty is left out of the doc, and their fingerprint doesn't move, so the next sync writes them again;
- the last published entry, byte for byte, for each unchanged student;
- the last published entry of a student missing from the grades, for at most 2 syncs in a row. On the third they are aged out of the doc (a doc-only PUT, no write-up). The count lives in `missed.json` and resets when they come back.

A run that left a changed student out or carried a missing one ends `published_partial` instead of `published`, logged with counts only. A sync where nothing changed and nobody ages out is a skip, though the missed counts are still saved.

When those rules leave nobody in the doc and the last published doc had someone, the runner sends `{"students":{}}`, saves state and ends `published`, logged as `published an empty doc` with the counts left out and aged out. The dashboard then shows no notes rather than stale ones. Only the two rules above can clear: a changed student with an empty write-up, or a missing student aged out. A failed run never clears. A scrape or GET error, a failed load or any other exception sends nothing, saves no state, keeps the current notes and logs only the error type. A load that succeeds but returns zero students isn't a failure: every student in the last doc counts as missing, is carried for 2 syncs, then aged out, so the third such sync clears. If nobody is left to send and there is nothing to clear, the run logs `nothing to send, the stored notes stay` with the count of students due to be removed.

State lives on the SSD under `/Volumes/David C SSD/grade-insights/state/`, mode 600 in a 700 folder, never in git: `fingerprint.json` (SHA-256 hashes only, keyed by a hash of the student key), `missed.json` (missed-sync counts, keyed the same way) and `published.json` (the last doc sent: student numbers and the story text, which can include scores). All three are written atomically with an fsync before the rename. A failed run leaves them alone and the next start tries again. Without `published.json` every student is written again, so a student whose write-up then comes out empty is left out of that PUT. If every write-up comes out empty then, nothing is sent and the Worker's last doc stays, because with no last doc on the mini there is nothing to clear. Each run's working files stay on the SSD under `grade-insights/runs/`, owner-only. A missing SSD skips that start with one log line and keeps the last good write-up; the sync stays unhandled, so the retry start tries again. A second start while one is running exits at the lock. The runner's own lines carry counts and outcomes only, never names, student numbers or scores. The scrape's class matcher (`class_match`) can also print `::warning::class match:` lines into the same log, and those can include class names and periods (no student names, numbers or scores).

### Installing the launchd job

Load it only after #57 is merged, both password steps are done (the parent PIN and the insights write secret are in the mini's login Keychain), and the PR that added this section is merged. Then, on the mini (the `git clone` line creates `~/code/aeries-dashboard`, the path the plist expects; skip it if that checkout already exists and `git pull` there instead):

```bash
git clone https://github.com/davidcasas82/aeries-dashboard.git ~/code/aeries-dashboard
cp ~/code/aeries-dashboard/deploy/com.davidcasas.grade-insights.plist ~/Library/LaunchAgents/
plutil -lint ~/Library/LaunchAgents/com.davidcasas.grade-insights.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.davidcasas.grade-insights.plist
```

The plist expects the checkout at `~/code/aeries-dashboard` and the venv at `/Volumes/David C SSD/grade-insights/venv`. Logs go to `~/Library/Logs/grade-insights.log` and `grade-insights.err.log`. After the first live run, check both logs for macOS privacy (TCC) blocks on the SSD reads, such as `Operation not permitted` on a `/Volumes/David C SSD` path. If one shows up, give the venv's Python (the Homebrew python3.12 it links to) access to removable volumes, or Full Disk Access, in System Settings → Privacy & Security, and let the next start retry. To stop the trial: `launchctl bootout gui/$(id -u)/com.davidcasas.grade-insights`.
