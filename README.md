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

Publishing is off unless `GRADE_INSIGHTS_PUBLISH=1`. With it unset, the publish step records `{"published": false}` and sends nothing. The dashboard reads the write-ups with `GET /v1/grades/insights` on the same PIN session as the grades and shows them under **Grade notes**, as plain text only. Every class with an opener gets a story. A gated class (a scale break, or a rebuilt grade that doesn't match the posted one) gets at most its move opener and that move's causes, with no keep, step or drag lines; with no move since the last snapshot it gets no story. When the posted letter and the rebuilt grade's letter differ, the story likewise stops at the opener and the move's causes. A quiet class skips the model call but still gets code's story. The opener names the letter the dashboard shows (the Aeries mark, else the percent's letter): "Still a B" only when that letter is unchanged since the last snapshot, and "Now a B" otherwise. When the mark's letter isn't the percent's, the opener states no gap to the next letter ("Still an A, down 0.7 since Oct 14."). The facts are put in one canonical order where they enter the writer, so the same facts in any order give the same bytes. Code, not the model, ends it with the next item's move-up step and keep sentence: the next named item's, or, when nothing is posted in a category, the same at the size of that category's past items, said as an assumption ("If the next test is worth about 50 points like the last ones, ..."; "a perfect 10" when it takes full points). There's no assumed line for a category whose past sizes vary more than 2x, and the engine marks such a keep so it is never assumed. Assumed lines are ordered by category weight, then category name. On a class with no recent move, code also names the past item pulling the grade down most, only when that item really costs the most points and isn't a zero, and on an A only within 2 points of the B line. A story over 600 characters drops that line first, then its second assumed line. Code tags each kid's easiest win and biggest risk (at most one of each, only on a story that says that step or keep). Named items rank ahead of assumed ones across all the kid's classes, then the needed score decides. Those classes come first, with an **Easiest win** or **Biggest risk** tag after the class name.
