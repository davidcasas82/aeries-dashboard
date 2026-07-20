# Plan: Full-picture data + deeper insights

**Goal:** Expand Aeries (and calendar) inputs and precompute correlations so the dashboard and Grok briefings show a fuller academic picture—without inventing causes.

**Out of scope for this plan:** Parent notes UI, discipline/medical scraping, light/dark redesign.

**Repo:** `aeries-dashboard`  
**Main files:** `scraper.py`, `grades_data.json`, `grade_history.json`, `school_calendar.json`, `index.html`, `.github/workflows/scrape.yml`

**Status:** Planning only — implement in later sessions (see phase order below).

---

## Principles (keep these)

1. **Aeries portal = source of truth** (plus public TUSD calendar).
2. **Precompute facts in Python; Grok only interprets** (same pattern as grade analytics).
3. **No invented psychology** — phrase as portal patterns.
4. **Summer/calendar pause stays** — grades/Grok off outside school year; attendance can still refresh.
5. **Ship in slices** — each phase leaves the app usable.

---

## What already exists (baseline)

| Bucket | Status |
|--------|--------|
| Classes / grades / Aeries trend | Yes |
| Assignments + categories | Yes |
| Missing / upcoming / recent | Yes (derived) |
| Daily grade history | Yes (`grade_history.json`) |
| Attendance year totals (calendar-aligned) | Yes |
| School calendar first/last → scrape pause | Yes |
| AI briefing (unified family voice) | Yes |
| Empty-term / summer UI messaging | Yes |

---

## Phase 0 — Baseline inventory (½ day)

Document fields on student / class / assignment / attendance (data dictionary).

**Deliverable:** This section kept up to date, or a short `docs/data-model.md`.

**Exit criteria:** Future sessions know what not to re-scrape.

---

## Phase 1 — Richer attendance (1–2 days)

**Why first:** Portal already has detail; high insight value; scrape path exists.

### 1A. Scrape

- Keep year totals (absences, present/enrolled, tardies).
- From **Attendance History Details** (same page tabs already hit):
  - Per-code counts for **target calendar year** only  
    e.g. Illness, Unexcused, Tardy, School Act, Independent Study…
  - Optional: period breakdown for tardies (P1–P7) if easy to parse.
- Classify codes using legend when available:
  - `excused` / `unexcused` / `verified_not_absent` / `tardy`
- Store shape:

```json
"attendance": {
  "year": "2025-2026",
  "absences": 2,
  "tardies": 4,
  "days_present": 178,
  "days_enrolled": 180,
  "by_code": [
    {"code": "F", "label": "Illness", "kind": "excused", "day_count": 2, "period_total": 8}
  ],
  "tardies_by_period": {"1": 0, "2": 1, "6": 1, "7": 1}
}
```

### 1B. UI

- Masthead: keep chips; add optional “Illness 2 · Unexcused 0”.
- Expanded detail later if needed—not required for v1.

### 1C. Tests

- Unit tests with saved HTML fixtures from probe (redact names).
- Assert prefer_year still calendar-aligned.

**Exit criteria:** Live chips match Aeries for the calendar year; codes visible in JSON.

---

## Phase 2 — Category intelligence (1–2 days)

**Why:** Tonight actions should prioritize work that counts.

### 2A. Rules (no new scrape if possible)

From assignment `category` strings (and names):

| Signal | Heuristic examples |
|--------|-------------------|
| `counts_toward_grade` | false if “not calculated”, “completion tracking”, etc. |
| `kind` | `assessment` / `practice` / `project` / `participation` / `other` |
| `is_formative` / `is_summative` | only if category/name clearly says so; else `unknown` |

### 2B. Optional scrape upgrade

- If portal shows Formative/Summative column or weight % → parse when present; soft-fail otherwise.

### 2C. Analytics

Per class:

- `assessment_avg` vs `practice_avg`
- `missing_graded_points` vs `missing_non_graded_points`
- `assessment_gap` flag (assessments lag practice by threshold)

### 2D. Briefing prompt

- Prefer Tonight items with `counts_toward_grade != false` and high points.
- Mention assessment gap only when flag is set.

**Exit criteria:** Missing lists and Tonight skew toward graded/high-leverage work.

---

## Phase 3 — Calendar-aware analysis (½–1 day)

### 3A. Extend `school_calendar.json`

Beyond first/last day, optionally:

- Fall / winter / spring recess ranges
- Non-student / staff days (if easy from TUSD page)

### 3B. Analytics rules

- Don’t treat recess weeks as “slipping” grade trends.
- Soften overdue language across multi-day breaks.
- Optional: “days since last instructional day” in summer empty state (partially done).

**Exit criteria:** No false “crisis” insights on break weeks.

---

## Phase 4 — Cross-signal insights engine (2–3 days)

**Core idea:** Pure functions in `scraper.py` (or `insights.py`) → `student["insights"]` array of structured facts for UI + Grok.

### 4A. Insight record shape

```json
{
  "id": "att_then_missing_bio",
  "severity": "medium",
  "title": "Biology missing work rose after absences",
  "detail": "In 3 of the last 4 weeks with an absence, Biology had new missing items within 5 days.",
  "evidence": {
    "class_name": "Tech of Biology",
    "absence_weeks": 4,
    "matched_weeks": 3
  },
  "confidence": "medium"
}
```

### 4B. v1 insight set (implement only if data supports)

| ID | Logic sketch |
|----|--------------|
| `assessment_gap` | assessment_avg + threshold below practice |
| `chronic_missing` | same assignment open across N scrapes |
| `grade_up_missing_up` | grade Δ > 0 and missing count ↑ (grading lag) |
| `attendance_pressure` | absences or tardies above soft thresholds for year |
| `period_tardy_hotspot` | one period has ≥ X% of tardies |
| `multi_class_critical` | ≥2 classes critical/watch |
| `week_load` | many due dates in next 7 instructional days |
| `post_absence_score_dip` | needs dated attendance; Phase 1+ history |

Start with **rule-based only**; no ML.

### 4C. History requirements

- Keep daily class snapshots (already).
- Optionally snapshot attendance weekly: `{date, absences, tardies}` so correlations improve mid-year.
- For absence→missing links you may need **per-day attendance** later (heavier scrape)—defer if year totals only.

### 4D. Grok

- Add `PRECOMPUTED_INSIGHTS` to the user message.
- Rules: only rephrase insights; don’t invent new correlations; empty list OK.

### 4E. UI

- Masthead or strip under briefing: 0–3 insight chips (title only; expand for detail).
- Severity colors: info / watch / critical—quiet teal/amber/red.

**Exit criteria:** At least 3 insight types fire on real data during a school year; summer shows few/none honestly.

---

## Phase 5 — Report card history (1–2 days, optional)

- Scrape **Report Card History** if page exists for Tustin.
- Store official marks by term/course.
- Insight: running gradebook % vs last official mark (gap / recovery).

**Soft-fail** if page missing or empty in summer.

---

## Phase 6 — Grade curves polish (½–1 day)

- Ensure `fetch_gradebook_summary` series is stored and used when daily history is thin.
- Prefer Aeries series for within-term sparklines; daily snapshots for multi-week Δ.

---

## Phase 7 — Hardening (ongoing)

| Item | Action |
|------|--------|
| Fixtures | Save redacted HTML for attendance history, class summary |
| Privacy | Don’t log full student names in CI probes; keep secrets in GH |
| Summer | Grades/Grok paused; attendance-only refresh continues |
| Prompt | Empty classes → static summer copy (already partly done) |
| Docs | README: data sources, calendar pause, how to force scrape |

---

## Suggested PR / session split

| Session | Ship |
|---------|------|
| **A** | Phase 1 attendance detail + tests |
| **B** | Phase 2 category intelligence + prompt/UI tweak |
| **C** | Phase 3 calendar recesses + trend guards |
| **D** | Phase 4 insights engine + Grok + 2–3 chips |
| **E** | Phase 5–6 if still hungry |

---

## Success metrics

1. Parent can answer in &lt;10s: grades risk, missing leverage, attendance picture, one “why this week” insight.
2. No insight without evidence object.
3. Summer: no bogus “mixed performance”; attendance year = calendar-aligned.
4. CI stays green; probe mode remains for HTML drift.

---

## Open decisions (decide when implementing)

1. **How aggressive on attendance?** Year totals only vs code breakdown vs full calendar days.
2. **Sibling insights?** Off by default.
3. **Show restored spring grades in summer?** (Currently yes—keep unless you prefer empty-term-only.)
4. **Insight max count** on phone (recommend 3).

---

## Prompt for a future Agent session

```
Implement Phase 1 of docs/insights-plan.md in aeries-dashboard:
richer attendance from Attendance History (by_code + calendar year alignment),
unit tests with fixtures if possible, masthead chips for excused/unexcused if available.
Do not start Phase 4 yet. Keep summer calendar pause and attendance-only refresh.
```

---

## File touch map (by phase)

| Phase | Files |
|-------|--------|
| 1 | `scraper.py`, `grades_data.json` (via scrape), `index.html`, fixtures optional |
| 2 | `scraper.py` (analytics + prompt), maybe `index.html` |
| 3 | `school_calendar.json`, `scraper.py` |
| 4 | `scraper.py` or new `insights.py`, prompt, `index.html` |
| 5–6 | `scraper.py`, UI sparklines |
| 7 | tests, README, workflow comments |

---

## Other data sources (reference)

### High value
- Attendance History Details (codes + periods)
- Category weights / formative-summative if portal shows them
- GradebookSummary series (partially started)
- Report Card History
- Schedule / period map
- School calendar recesses

### Medium / optional
- State test scores
- Graduation / transcript
- Fees (usually noise)

### Skip
- Contacts, demographics, medical (sensitive, low academic payoff)

---

## Correlation ideas (for Phase 4+)

1. **Attendance × grades** — post-absence missing spikes; period tardy vs class grade  
2. **Category × outcomes** — assessment gap; graded vs non-graded missing  
3. **Time patterns** — Friday due piles; late completion; chronic missing age  
4. **Cross-class load** — multi-critical; weekly recoverable points  
5. **Trend quality** — grade up + missing up (grading lag); forecast vs actual  
6. **YoY attendance** — multi-year history cards already on portal  

**Phrase as portal patterns, not character judgments.**
