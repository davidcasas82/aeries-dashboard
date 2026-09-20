"""Parent memo: the 4–6 sentence briefing at the top of the dashboard.

Built from precomputed facts only (scraper.build_class_analytics plus the
Classroom block), at scrape time, with no model in the loop. Every sentence
names the assignment, class, score or date it rests on, so the page renders
it verbatim and never has to rewrite copy after the fact.

Sentence order is fixed (tonight, Classroom, grades, scores, trend, admin),
importance decides what survives the six-sentence cap, and factual fillers
bring a thin day up to four sentences. Nothing here logs student names or
numbers; the display name is only placed into the memo text itself.
"""

import re
from datetime import datetime, timezone

MIN_SENTENCES = 4
MAX_SENTENCES = 6
RECENT_SCORE_DAYS = 7
CLASSROOM_LOOKAHEAD_DAYS = 7
ANNOUNCEMENT_MAX_AGE_DAYS = 3
ANNOUNCEMENT_CLIP = 140
LOW_GRADE_PCT = 80
HIGH_GRADE_PCT = 90
LOW_SCORE_PCT = 70
TREND_MIN_DELTA = 2

_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}


# --------------------------------------------------------------------------- formatting


def _count(n):
    return _WORDS.get(n, str(n))


def _pts(v):
    if v is None:
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _pct(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return f"{int(round(f))}%"


def _grade(c):
    mark = (c.get("current_grade_mark") or "").strip()
    pct = _pct(c.get("current_grade_pct"))
    if mark and pct:
        return f"{pct} ({mark})"
    return pct or mark


def _join(parts):
    parts = [p for p in parts if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def _month_day(d):
    return f"{d.strftime('%b')} {d.day}"


def _iso_day_label(iso_day):
    try:
        return _month_day(datetime.strptime(iso_day, "%Y-%m-%d"))
    except (TypeError, ValueError):
        return ""


def _mmdd_label(mmddyyyy):
    try:
        return _month_day(datetime.strptime(mmddyyyy, "%m/%d/%Y"))
    except (TypeError, ValueError):
        return ""


def _weekday_label(mmddyyyy, today):
    try:
        d = datetime.strptime(mmddyyyy, "%m/%d/%Y").date()
    except (TypeError, ValueError):
        return ""
    days = (d - today.date()).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if 1 < days < 7:
        return d.strftime("%A")
    return _month_day(d)


def _sentence(text):
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return ""
    if text[-1] not in ".!?”\"":
        text += "."
    return text[0].upper() + text[1:]


def _clip(text, n):
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _score(entry):
    e, p = entry.get("points_earned"), entry.get("points_possible")
    if e is not None and p is not None:
        return f"{_pts(e)}/{_pts(p)}"
    if entry.get("percentage") is not None:
        return _pct(entry["percentage"])
    return (entry.get("score_raw") or "").strip()


# --------------------------------------------------------------------------- facts


def _tonight_groups(analytics):
    plan = (analytics or {}).get("tonight_plan") or {}
    groups = {"due_today": [], "missing": [], "due_tomorrow": []}
    for item in plan.get("items") or []:
        groups.setdefault(item.get("reason") or "", []).append(item)
    return groups


def _all_missing(analytics):
    """Aeries-flagged missing rows that are still to-dos (not turned in on Classroom)."""
    out = []
    for c in (analytics or {}).get("classes") or []:
        for m in c.get("missing_assignments") or []:
            if m.get("turned_in") or m.get("turned_in_classroom"):
                continue
            out.append((c, m))
    return out


def _fact_tonight(analytics, name, today):
    """Due today, then missing, then due tomorrow. Each a sentence of its own."""
    groups = _tonight_groups(analytics)
    out = []

    due_today = groups.get("due_today") or []
    if due_today:
        parts = [f"{i['name']} ({i['class_name']})" for i in due_today]
        out.append(("due_today", 100, _sentence(f"{name} has {_join(parts)} due today")))

    missing = _all_missing(analytics)
    if missing:
        named = [f"{m.get('name')} ({c.get('course_name')})" for c, m in missing[:3]]
        more = len(missing) - len(named)
        pts = sum(float(m.get("points_possible") or 0) for _, m in missing)
        text = f"Aeries flags {_count(len(missing))} missing item{'s' if len(missing) > 1 else ''}: {_join(named)}"
        if more > 0:
            text += f", and {more} more"
        if pts:
            text += f" ({_pts(pts)} points in all)"
        out.append(("missing", 95, _sentence(text)))

    due_tomorrow = groups.get("due_tomorrow") or []
    if due_tomorrow:
        parts = [f"{i['name']} ({i['class_name']})" for i in due_tomorrow]
        if due_today:
            verb = "is" if len(parts) == 1 else "are"
            text = f"Then {_join(parts)} {verb} due tomorrow"
        else:
            text = f"{name} has {_join(parts)} due tomorrow"
        out.append(("due_tomorrow", 90, _sentence(text)))

    if not out:
        out.append(("clear", 100, _sentence(
            f"Nothing is due today or tomorrow for {name}, and Aeries shows no missing work"
        )))
    return out


def _fact_classroom_handed_in(analytics, class_contexts):
    out = []
    for c in (analytics or {}).get("classes") or []:
        ctx = class_contexts.get(c.get("course_name")) or {}
        for e in ctx.get("turned_in_aeries_missing") or []:
            when = _iso_day_label(e.get("turned_in_on"))
            text = f"{e.get('title')} ({c.get('course_name')}) was turned in on Classroom"
            if when:
                text += f" on {when}"
            text += ", so it is not a to-do; Aeries just has not caught up"
            out.append(("classroom_handed_in", 92, _sentence(text)))
    return out[:2]


def _fact_grades(analytics):
    graded = [
        c for c in (analytics or {}).get("classes") or []
        if c.get("current_grade_pct") is not None and not c.get("phantom_zero")
    ]
    if not graded:
        return []
    lows = sorted(
        [c for c in graded if c["current_grade_pct"] < LOW_GRADE_PCT],
        key=lambda c: c["current_grade_pct"],
    )
    highs = sorted(graded, key=lambda c: -c["current_grade_pct"])
    top = highs[0]
    out = []
    if lows:
        parts = []
        for c in lows[:3]:
            part = f"{c['course_name']} at {_grade(c)}"
            n_missing = len([m for m in c.get("missing_assignments") or [] if not m.get("turned_in_classroom")])
            if n_missing:
                part += f" with {_count(n_missing)} missing"
            parts.append(part)
        lead = "One class is under 80%" if len(lows) == 1 else f"{_count(len(lows)).capitalize()} classes are under 80%"
        text = f"{lead}: {_join(parts)}"
        if top["current_grade_pct"] >= LOW_GRADE_PCT:
            text += f"; {top['course_name']} leads at {_grade(top)}"
        out.append(("grades", 85, _sentence(text)))
    else:
        low = sorted(graded, key=lambda c: c["current_grade_pct"])[0]
        if len(graded) == 1:
            text = f"{top['course_name']} is the only graded class so far, at {_grade(top)}"
        elif low["current_grade_pct"] >= HIGH_GRADE_PCT:
            text = (
                f"All {_count(len(graded))} graded classes are at 90% or better, "
                f"from {low['course_name']} at {_grade(low)} to {top['course_name']} at {_grade(top)}"
            )
        else:
            text = f"Lowest grade is {low['course_name']} at {_grade(low)}; {top['course_name']} leads at {_grade(top)}"
        out.append(("grades", 85, _sentence(text)))
    return out


def _fact_phantom(analytics):
    names = [
        c["course_name"] for c in (analytics or {}).get("classes") or []
        if c.get("phantom_zero")
    ]
    if not names:
        return []
    verb = "shows" if len(names) == 1 else "show"
    text = f"{_join(names)} {verb} 0% in Aeries only because nothing has been graded yet; that is not a real grade"
    return [("phantom", 80, _sentence(text))]


def _fact_recent_scores(analytics, today):
    rows = []
    for c in (analytics or {}).get("classes") or []:
        for e in c.get("recent_scores") or []:
            try:
                due = datetime.strptime(e.get("due_date") or "", "%m/%d/%Y")
            except ValueError:
                continue
            if (today - due).days > RECENT_SCORE_DAYS:
                continue
            rows.append((due, c, e))
    if not rows:
        return []
    rows.sort(key=lambda r: r[0], reverse=True)
    out = []
    low = [r for r in rows if r[2].get("percentage") is not None and float(r[2]["percentage"]) < LOW_SCORE_PCT]
    if low:
        due, c, e = low[0]
        out.append(("low_score", 78, _sentence(
            f"{e.get('name')} in {c['course_name']} came back {_score(e)} ({_pct(e['percentage'])}) on {_month_day(due)}"
        )))
    shown = [r for r in rows if not low or r is not low[0]][:2]
    if shown:
        parts = [f"{e.get('name')} {_score(e)} in {c['course_name']}" for _, c, e in shown]
        lead = "Recent score" if len(parts) == 1 else "Recent scores"
        out.append(("recent_scores", 60, _sentence(f"{lead}: {_join(parts)}")))
    return out


def _fact_trend(analytics):
    movers = []
    for c in (analytics or {}).get("classes") or []:
        h = c.get("history") or {}
        d = h.get("delta_7d")
        if d is None:
            continue
        try:
            d = float(d)
        except (TypeError, ValueError):
            continue
        if abs(d) >= TREND_MIN_DELTA:
            movers.append((d, c["course_name"]))
    if not movers:
        return []
    movers.sort(key=lambda m: -abs(m[0]))
    parts = []
    for i, (d, course) in enumerate(movers[:2]):
        direction = "up" if d > 0 else "down"
        part = f"{course} is {direction} {_pts(abs(d))} points"
        if i == 0:
            part += " from a week ago"
        parts.append(part)
    return [("trend", 70, _sentence(_join(parts)))]


def _fact_classroom_ahead(analytics, class_contexts, today):
    """Classroom-only work due in the next week that is not already tonight's."""
    tonight_names = {
        (i.get("name") or "").lower()
        for i in ((analytics or {}).get("tonight_plan") or {}).get("items") or []
    }
    cands = []
    for c in (analytics or {}).get("classes") or []:
        ctx = class_contexts.get(c.get("course_name")) or {}
        for e in ctx.get("classroom_only") or []:
            days = e.get("days_until_due")
            if days is None or days < 0 or days > CLASSROOM_LOOKAHEAD_DAYS:
                continue
            if (e.get("title") or "").lower() in tonight_names:
                continue
            cands.append((days, c["course_name"], e))
    if not cands:
        return []
    cands.sort(key=lambda x: x[0])
    parts = []
    for days, course, e in cands[:2]:
        when = _weekday_label(e.get("due_date"), today)
        state = (e.get("state_label") or "").lower()
        part = f"{e.get('title')} in {course} is due {when}"
        if state and "not" in state:
            part += f" and is {state.replace(' on classroom', '')} in Classroom"
        parts.append(part)
    lead = "Posted in Classroom but not in Aeries yet: "
    return [("classroom_ahead", 65, _sentence(lead + _join(parts)))]


def _fact_announcement(analytics, class_contexts, today):
    best = None
    for c in (analytics or {}).get("classes") or []:
        ctx = class_contexts.get(c.get("course_name")) or {}
        for a in ctx.get("announcements") or []:
            try:
                posted = datetime.strptime(a.get("posted_on") or "", "%Y-%m-%d")
            except ValueError:
                continue
            age = (today - posted).days
            if age > ANNOUNCEMENT_MAX_AGE_DAYS or not (a.get("text") or "").strip():
                continue
            if best is None or posted > best[0]:
                best = (posted, c["course_name"], a)
    if not best:
        return []
    posted, course, a = best
    quote = _clip(a["text"], ANNOUNCEMENT_CLIP).rstrip(".")
    return [("announcement", 55, _sentence(f"Teacher note in {course} ({_month_day(posted)}): “{quote}.”"))]


def _fact_awaiting(analytics):
    total = sum(int(c.get("awaiting_count") or 0) for c in (analytics or {}).get("classes") or [])
    if not total:
        return []
    stale = sum(int(c.get("stale_awaiting_count") or 0) for c in (analytics or {}).get("classes") or [])
    text = f"{_count(total).capitalize()} turned-in item{'s' if total > 1 else ''} {'are' if total > 1 else 'is'} waiting for a teacher score"
    if stale:
        text += f", {_count(stale)} of them for 10 or more days"
    text += "; that is not missing work"
    return [("awaiting", 50, _sentence(text))]


def _fact_coverage(analytics):
    cov = (analytics or {}).get("coverage") or {}
    scheduled = int(cov.get("scheduled_classes") or 0)
    with_work = int(cov.get("classes_with_portal_work") or 0)
    gap = scheduled - with_work
    if scheduled and gap >= 2:
        return [("coverage", 40, _sentence(
            f"{_count(gap).capitalize()} of {_count(scheduled)} classes have no work posted in Aeries yet, which is normal early in the term"
        ))]
    return []


def _fact_empty_bucket(analytics):
    for c in (analytics or {}).get("classes") or []:
        if c.get("phantom_zero"):
            continue
        for cat in c.get("empty_high_weight_categories") or []:
            w = cat.get("weight_pct")
            if not cat.get("name") or w is None:
                continue
            return [("empty_bucket", 45, _sentence(
                f"In {c['course_name']}, the {cat['name']} category ({_pts(w)}% of the grade) has nothing in it yet, so that grade can still move a lot"
            ))]
    return []


def _fillers(analytics, student_data, today):
    """True statements that only appear when the day is thin."""
    out = []
    groups = _tonight_groups(analytics)
    if not groups.get("due_tomorrow"):
        upcoming_tomorrow = any(
            a.get("days_until_due") == 1
            for c in (analytics or {}).get("classes") or []
            for a in c.get("upcoming") or []
        )
        if not upcoming_tomorrow:
            out.append(_sentence("Nothing is due tomorrow"))
    if not any(int(c.get("awaiting_count") or 0) for c in (analytics or {}).get("classes") or []):
        out.append(_sentence("No turned-in work is waiting for a teacher score"))
    block = (student_data or {}).get("classroom") or {}
    if block.get("captured_at"):
        cap = _iso_day_label(block["captured_at"][:10])
        out.append(_sentence(f"Classroom export from {cap} shows nothing else posted that Aeries is missing"))
    out.append(_sentence(f"Aeries figures as of {_month_day(today)}"))
    return out


# --------------------------------------------------------------------------- compose

_ORDER = [
    "due_today", "missing", "clear", "due_tomorrow", "classroom_handed_in",
    "grades", "phantom", "low_score", "empty_bucket", "classroom_ahead",
    "announcement", "trend", "recent_scores", "awaiting", "coverage",
]


def build_parent_memo(analytics, student_data, today, class_contexts=None, display_name=None):
    """Return {"sentences", "text", "as_of", "generated_at", "source"} for one student.

    ``class_contexts`` maps course_name -> classroom.class_context(...) (may be empty).
    ``display_name`` overrides the payload name (samples use "Student A").
    """
    analytics = analytics or {}
    class_contexts = class_contexts or {}
    name = (display_name or (student_data or {}).get("name") or "The student").strip()
    classes = analytics.get("classes") or []

    facts = []
    if classes:
        facts += _fact_tonight(analytics, name, today)
        facts += _fact_classroom_handed_in(analytics, class_contexts)
        facts += _fact_grades(analytics)
        facts += _fact_phantom(analytics)
        facts += _fact_recent_scores(analytics, today)
        facts += _fact_trend(analytics)
        facts += _fact_classroom_ahead(analytics, class_contexts, today)
        facts += _fact_announcement(analytics, class_contexts, today)
        facts += _fact_awaiting(analytics)
        facts += _fact_coverage(analytics)
        facts += _fact_empty_bucket(analytics)
    else:
        scheduled = int((analytics.get("coverage") or {}).get("scheduled_classes") or 0)
        if scheduled:
            facts.append(("clear", 100, _sentence(
                f"Schedules are up for {name} ({scheduled} class{'es' if scheduled != 1 else ''}); no grades or work posted yet"
            )))
        else:
            facts.append(("clear", 100, _sentence(
                f"The portal has no current classes or missing work for {name}"
            )))

    # Keep the most important facts, then put them back in reading order.
    kept = sorted(facts, key=lambda f: -f[1])[:MAX_SENTENCES]
    rank = {k: i for i, k in enumerate(_ORDER)}
    kept.sort(key=lambda f: rank.get(f[0], len(rank)))
    sentences = []
    seen = set()
    for _, _, text in kept:
        if text and text not in seen:
            sentences.append(text)
            seen.add(text)

    if classes:
        for filler in _fillers(analytics, student_data, today):
            if len(sentences) >= MIN_SENTENCES:
                break
            if filler not in seen:
                sentences.append(filler)
                seen.add(filler)

    return {
        "sentences": sentences,
        "text": " ".join(sentences),
        "as_of": today.date().isoformat() if hasattr(today, "date") else str(today),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "computed",
    }
