# Aeries family grade dashboard

A static parent dashboard for Tustin USD Aeries grades. A GitHub Action logs into the parent portal a few times a day, writes `grades_data.json`, and GitHub Pages serves [the same URL](https://davidcasas82.github.io/aeries-dashboard/).

Python precomputes facts (missing work, tonight’s list, urgency, trends). Grok only writes the briefing from those facts.

## Local preview

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m http.server 8765
```

Open http://localhost:8765/ — browsers cannot load `grades_data.json` from a `file://` page.

## Scrape (needs a `.env`)

Copy `.env.example` to `.env` and fill in the Aeries login plus each student’s SN.

```bash
python scraper.py                 # full scrape + briefing
python scraper.py --grok-only     # rewrite briefings from existing JSON
python scraper.py --attendance-only
python scraper.py --rebuild-view  # dashboard view payload only
python scraper.py --probe-gradebook
python scraper.py --probe-attendance
```

`SUMMER_BREAK=true` pauses grades and Grok (attendance can still refresh). Leave it unset to follow `school_calendar.json`.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Schedule

- Overnight ~2:07am PT
- After school ~4:07 / 4:12pm PT
- Evening ~8:07pm PT

Actions logs print `student 1`, not names or student numbers. The Pages site is still public — treat the URL as a household bookmark, not a secret.

## Data files

| File | Role |
|------|------|
| `grades_data.json` | Latest scrape the page reads |
| `grade_history.json` | Daily snapshots for trends |
| `school_calendar.json` | First/last day + term cutovers |
