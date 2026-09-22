"""Official v1 glance: standing + Due soon + look-next packet + drawer facts.

Due soon = every unsubmitted assignment due today or on the next 5
school days including today (Mon–Fri, Pacific). Saturday and Sunday are
skipped. On Fri/Sat/Sun the window is the coming school days. No item
cap. Soonest due first, weekday and date on each row. Turned in
(Classroom TURNED_IN/RETURNED, or Aeries scored / date completed) leaves
immediately. If neither records a turn-in, the row still leaves once
the due date is before today — that work stays on the class chip as
past due. No fact-line strip, no Later, no Today, no Done today.
Under Due soon, no extra heading: look_next_packet picks at most three
facts the list does not show (nearest work after the window, the class
with the most past due, one turned-in unscored item in the window).
look_next_sentences writes up to two short lines from that packet.
Grok may only rephrase the same packet; look_next_grok_accepts throws
the sentence out when a title or date is not in the packet. Never the
sentence “X is the lowest class, at N%.” Never the raw ai_summary
headline. Empty Due soon: “Nothing due in the next 5 school days.”
Empty packet: “Nothing else outside this list.”

Nothing here logs student names or numbers.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from classroom import TURNED_IN_STATES, teacher_card_body

BAND_LIMIT = 4
DUE_SOON_SCHOOL_DAYS = 5
DUE_SOON_EMPTY = "Nothing due in the next 5 school days."
DUE_SOON_SUBTITLE = "Not turned in, due in the next 5 school days"
DUE_SOON_HEADING = "Due soon"
LOOK_NEXT_EMPTY = "Nothing else outside this list."
TONIGHT_SCHOOL_DAYS = DUE_SOON_SCHOOL_DAYS
FOCUS_SCHOOL_DAYS = DUE_SOON_SCHOOL_DAYS
FOCUS_EMPTY = DUE_SOON_EMPTY
FOCUS_SUBTITLE = DUE_SOON_SUBTITLE
FORECAST_MAX_AGE_DAYS = 14
TREND_STEADY_PTS = 2.0

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_ASSESS = re.compile(
    r"\b(?:unit\s+\d+\s+)?(?:quiz|test|exam|assessment|midterm|final)\b",
    re.I,
)
_ASSESS_STUDY = re.compile(
    r"\b(?:review|practice|prep|study|packet|homework|hw)\b",
    re.I,
)
_WEEKDAY = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tomorrow)\b",
    re.I,
)
_MONTH_DAY = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+(\d{1,2})\b",
    re.I,
)


def _as_date(today):
    return today.date() if hasattr(today, "date") else today


def _parse_mmdd(value):
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _month_day(d):
    if d is None:
        return ""
    return f"{d.strftime('%b')} {d.day}"


def _weekday_date(d):
    """Monday, Sep 21 — never weekday-only or date-only."""
    if d is None:
        return ""
    return f"{d.strftime('%A')}, {_month_day(d)}"


def _late_label(days):
    if days == 1:
        return "1 day late"
    return f"{days} days late"


def due_when_label(due, today, *, kind=None, done=False):
    """Parent due copy: always weekday + date. Tonight is not a due date."""
    if due is None:
        return "Missing" if kind == "missing" else ""
    wd = _weekday_date(due)
    today_d = _as_date(today) if today is not None else None
    if today_d is None:
        return f"Due {wd}"
    days = (due - today_d).days
    if kind == "weekend":
        return f"This weekend · due {wd}"
    if days == 0:
        return f"Due today · {wd}"
    if days == 1:
        return f"Due tomorrow · {wd}"
    if days > 1:
        return f"Due {wd}"
    if done:
        return f"Due {wd}"
    return f"{_late_label(-days)} · was due {wd}"


def _parse_posted(value):
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return _parse_mmdd(text)


def is_weekend_night(today):
    return _as_date(today).weekday() >= 4


def weekend_dates(today):
    """Sat/Sun/Mon for the current weekend window. None on Mon–Thu."""
    day = _as_date(today)
    wd = day.weekday()
    if wd < 4:
        return None
    if wd == 4:
        sat = day + timedelta(days=1)
    elif wd == 5:
        sat = day
    else:
        sat = day - timedelta(days=1)
    return {
        "Saturday": sat,
        "Sunday": sat + timedelta(days=1),
        "Monday": sat + timedelta(days=2),
    }


def submitted_in_classroom(item):
    """Already in: Classroom TURNED_IN/RETURNED, or Aeries Date Completed.

    Same predicate Tonight uses to suppress a row. Aeries-missing after
    this is gradebook lag, not late / not-turned-in.
    """
    if not item:
        return False
    if item.get("turned_in") or item.get("turned_in_classroom"):
        return True
    if str(item.get("date_completed") or "").strip():
        return True
    cr = item.get("classroom") or {}
    state = (cr.get("state") or "").upper()
    return state in TURNED_IN_STATES


def newest_announcements(announcements, limit=3):
    rows = [a for a in (announcements or []) if (a.get("text") or "").strip()]
    rows.sort(key=lambda a: (_parse_posted(a.get("posted_on")) or datetime.min.date()), reverse=True)
    return rows[:limit]


_ASSESS_TITLE = re.compile(
    r"((?:unit\s+\d+\s+)?(?:quiz|test|exam|assessment|midterm|final)"
    r"(?:\s+(?:on\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))?)",
    re.I,
)


def _assessment_title(text, weekday_word=None):
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return "Upcoming assessment"
    m = _ASSESS_TITLE.search(text)
    if not m:
        return _ASSESS.search(text).group(0) if _ASSESS.search(text) else text[:48]
    title = m.group(1).strip()
    return title[:72] or "Upcoming assessment"


def resolve_forecast_date(text, posted, today):
    """Date named in a teacher note. Weekday is resolved from the post date."""
    today_d = _as_date(today)
    posted_d = posted or today_d
    raw = text or ""
    month = _MONTH_DAY.search(raw)
    if month:
        try:
            dt = datetime.strptime(f"{month.group(1)} {month.group(2)} {today_d.year}", "%B %d %Y")
        except ValueError:
            try:
                dt = datetime.strptime(f"{month.group(1)} {month.group(2)} {today_d.year}", "%b %d %Y")
            except ValueError:
                dt = None
        if dt:
            d = dt.date()
            if d < posted_d - timedelta(days=30):
                d = d.replace(year=d.year + 1)
            return d, _month_day(d)
    wd = _WEEKDAY.search(raw)
    if not wd:
        return None, None
    word = wd.group(1).lower()
    if word == "today":
        return posted_d, "today"
    if word == "tomorrow":
        return posted_d + timedelta(days=1), "tomorrow"
    target = _WEEKDAYS[word]
    delta = (target - posted_d.weekday()) % 7
    when = posted_d + timedelta(days=delta)
    return when, word.capitalize()


def _is_assessment(text):
    """In-class test/quiz/exam. Review packets stay ordinary work."""
    raw = text or ""
    if not _ASSESS.search(raw):
        return False
    return not _ASSESS_STUDY.search(raw)


def _standing_id(cls):
    name = ((cls or {}).get("course_name") or "").strip()
    period = (cls or {}).get("period")
    return f"standing-{period if period is not None else name}"


def _scored_class(cls):
    mark, pct = _grade_display(cls)
    return mark not in ("", "—") or bool(pct)


def _pct_value(cls):
    raw = (cls or {}).get("percent")
    try:
        if raw in (None, ""):
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def _same_class(a, b):
    if not a or not b:
        return False
    if a.get("period") is not None and b.get("period") is not None:
        return a.get("period") == b.get("period")
    return (a.get("course_name") or "").lower() == (b.get("course_name") or "").lower()


def _lowest_class(view_classes):
    scored = [
        c for c in (view_classes or [])
        if _scored_class(c) and _pct_value(c) is not None
    ]
    if len(scored) < 2:
        return None
    return min(
        scored,
        key=lambda c: (
            _pct_value(c),
            str(c.get("period") or ""),
            c.get("course_name") or "",
        ),
    )


def _forecast_matches(item, when, title):
    name = _work_name(item)
    if not name:
        return False
    due = _work_due_key(item)
    if when is not None and due is not None and due != when:
        return False
    name_l = name.lower().strip()
    title_l = (title or "").lower().strip()
    if title_l and name_l == title_l:
        return True
    return _is_assessment(name) and _is_assessment(title)


def matching_forecast_work(cls, when, title):
    for item in _class_raw_work(cls):
        if _forecast_matches(item, when, title):
            return item
    return None


def today_fact_label(item, today, due=None):
    """Facts only. Say turned in only when submission says so. Never a score."""
    due = due if due is not None else _work_due_key(item)
    if submitted_in_classroom(item):
        on = _work_turned_in_on(item)
        if on:
            return f"Turned in · {_weekday_date(on)}"
        return "Turned in"
    return due_when_label(due, today, done=_work_done(item) if item else False)


def pick_forecast(view_classes, today):
    """Newest dated upcoming assessment across classes. Never the oldest-three dump."""
    today_d = _as_date(today)
    best = None
    for cls in view_classes or []:
        cr = cls.get("classroom") or {}
        for note in newest_announcements(cr.get("announcements"), limit=8):
            text = (note.get("text") or "").strip()
            if not _ASSESS.search(text):
                continue
            posted = _parse_posted(note.get("posted_on"))
            if posted and (today_d - posted).days > FORECAST_MAX_AGE_DAYS:
                continue
            when, word = resolve_forecast_date(text, posted, today_d)
            if when is None or when < today_d:
                continue
            if not _WEEKDAY.search(text) and not _MONTH_DAY.search(text):
                continue
            key = posted or datetime.min.date()
            cand = (key, when, cls, note, word)
            if best is None or key > best[0] or (key == best[0] and when < best[1]):
                best = cand
    if not best:
        return None
    _posted, when, cls, note, word = best
    text = (note.get("text") or "").strip()
    title = _assessment_title(text, word)
    posted = _parse_posted(note.get("posted_on"))
    teacher_when = f"teacher, {_month_day(posted)}" if posted else "teacher"
    label = due_when_label(when, today_d)
    return {
        "id": f"forecast-{cls.get('period') or 'x'}",
        "kind": "forecast",
        "icon": "Q",
        "label": label,
        "title": title,
        "course": cls.get("course_name") or "",
        "period": cls.get("period"),
        "due_date": when.strftime("%m/%d/%Y"),
        "source": "teacher",
        "drawer": {
            "kicker": f"{label} · {teacher_when}",
            "title": title,
            "course": cls.get("course_name") or "",
            "teacher_words": text,
            "classroom": (
                f"Teacher note · posted {_month_day(posted)}"
                if posted
                else "Teacher note"
            ),
            "aeries": "No row yet · dated from the teacher note",
        },
    }


def _trend(cls):
    delta = cls.get("delta_7d")
    try:
        delta = float(delta) if delta is not None else None
    except (TypeError, ValueError):
        delta = None
    if delta is None:
        return "none", "•", "no baseline"
    if abs(delta) < TREND_STEADY_PTS:
        return "flat", "→", "steady"
    if delta > 0:
        pts = int(delta) if float(delta) == int(delta) else f"{delta:g}"
        return "up", "▴", f"up {pts} pts"
    pts = int(abs(delta)) if float(delta) == int(delta) else f"{abs(delta):g}"
    return "down", "▾", f"down {pts} pts"


def _grade_display(cls):
    mark = (cls.get("mark") or "").strip()
    raw = cls.get("percent")
    try:
        pct = None if raw in (None, "") else round(float(raw))
    except (TypeError, ValueError):
        pct = None
    if cls.get("phantom_zero") or (not mark and pct in (None, 0)):
        return "—", ""
    if mark and pct is not None:
        return mark, f"{pct}%"
    if mark:
        return mark, ""
    if pct is not None:
        return f"{pct}%", ""
    return "—", ""


def _pct_grade_class(n):
    """Same A/B/C/D/F bands as the pre-glance dashboard getGradeClass."""
    if n is None:
        return "grade-none"
    if n >= 90:
        return "grade-a"
    if n >= 80:
        return "grade-b"
    if n >= 70:
        return "grade-c"
    if n >= 60:
        return "grade-d"
    return "grade-f"


def _grade_class(cls, mark=""):
    """Same A/B/C/D/F bands as the pre-glance dashboard getGradeClass."""
    if (mark or "").strip() in ("", "—") and not (cls or {}).get("percent"):
        return "grade-none"
    raw = (cls or {}).get("percent")
    try:
        n = None if raw in (None, "") else float(raw)
    except (TypeError, ValueError):
        return "grade-none"
    return _pct_grade_class(n)


def _score_grade_class(item):
    """Color a points score from earned/possible. 10/10 is A; 5/10 is F."""
    earned = (item or {}).get("points_earned")
    poss = (item or {}).get("points_possible")
    if earned is None or poss in (None, 0):
        return ""
    try:
        return _pct_grade_class(100.0 * float(earned) / float(poss))
    except (TypeError, ValueError, ZeroDivisionError):
        return ""


def _classroom_status(item, fallback="No Classroom row"):
    cr = (item or {}).get("classroom") or {}
    if not cr and not item:
        return fallback
    parts = []
    label = (cr.get("state_label") or "").strip()
    if label:
        parts.append(label)
    elif submitted_in_classroom(item):
        parts.append("Turned in on Classroom")
    elif item:
        parts.append("Not submitted")
    if cr.get("turned_in_on"):
        parts.append(f"turned in {_month_day(_parse_posted(cr.get('turned_in_on')))}")
    if cr.get("assigned_on"):
        parts.append(f"assigned {_month_day(_parse_posted(cr.get('assigned_on')))}")
    due = _parse_mmdd((item or {}).get("due_date") or cr.get("due") or cr.get("due_date"))
    if due:
        parts.append(f"due {_month_day(due)}")
    return " · ".join(parts) if parts else fallback


def _aeries_status(item, cls, last_checked, *, missing=False):
    checked = f"last checked {last_checked}" if last_checked else "last checked from Aeries"
    if missing:
        due = _parse_mmdd((item or {}).get("due_date"))
        posted = f" · posted {_month_day(due)}" if due else ""
        return f"Missing{posted} · {checked}"
    if item and item.get("source") == "classroom":
        return f"No row yet · {checked}"
    mark, pct = _grade_display(cls or {})
    if mark and mark != "—":
        shown = f"{mark} {pct}".strip()
        return f"{shown} current · {checked}"
    return f"No row yet · {checked}"


def _teacher_words(item, cls):
    cr = (item or {}).get("classroom") or {}
    text = (cr.get("instructions") or "").strip()
    if text:
        return text
    comment = ((item or {}).get("teacher_comment") or "").strip()
    if comment:
        return comment
    notes = newest_announcements(((cls or {}).get("classroom") or {}).get("announcements"), limit=1)
    if notes:
        return notes[0].get("text") or "No teacher wording in this export."
    return "No teacher wording in this export."


def _action(kind, item, cls, last_checked, *, weekend_label=None, today=None):
    name = (item.get("name") or item.get("title") or item.get("description") or "").strip()
    course = cls.get("course_name") or ""
    period = cls.get("period")
    missing = kind == "missing"
    icons = {
        "today": ("today", "•"),
        "tomorrow": ("tomorrow", "→"),
        "missing": ("missing", "!"),
        "weekend": ("weekend", (weekend_label or "W")[:1]),
    }
    icon_kind, icon = icons[kind]
    due = _due_days(item)
    done = submitted_in_classroom(item) or (item or {}).get("points_earned") is not None
    label = due_when_label(due, today, kind=kind, done=done)
    return {
        "id": f"{kind}-{period}-{name}".lower().replace(" ", "-")[:80],
        "kind": icon_kind,
        "icon": icon,
        "label": label,
        "weekend_day": weekend_label,
        "title": name,
        "course": course,
        "period": period,
        "due_date": item.get("due_date") or "",
        "source": item.get("source") or "",
        "drawer": {
            "kicker": f"{label} · verified",
            "title": name,
            "course": course,
            "teacher_words": _teacher_words(item, cls),
            "classroom": _classroom_status(item),
            "aeries": _aeries_status(item, cls, last_checked, missing=missing),
        },
    }


def _due_days(item):
    return _parse_mmdd(item.get("due_date") or item.get("due"))


def _as_work_item(raw):
    if not raw:
        return None
    if raw.get("name") or raw.get("description") or raw.get("title"):
        item = dict(raw)
        if not item.get("name"):
            item["name"] = raw.get("description") or raw.get("title")
        return item
    return None


def _class_work(cls):
    upcoming = [_as_work_item(a) for a in cls.get("upcoming") or []]
    missing = [_as_work_item(a) for a in cls.get("missing") or []]
    extra = []
    cr = cls.get("classroom") or {}
    seen = {(i.get("name") or "").lower() for i in upcoming if i}
    for e in cr.get("classroom_only") or []:
        title = (e.get("title") or "").strip()
        if not title or title.lower() in seen:
            continue
        extra.append({
            "name": title,
            "due_date": e.get("due_date") or "",
            "source": "classroom",
            "classroom": {
                "id": e.get("id"),
                "link": e.get("link"),
                "state": e.get("state"),
                "state_label": e.get("state_label"),
                "instructions": e.get("instructions"),
                "due": e.get("due"),
            },
        })
    return [i for i in upcoming if i] + extra, [i for i in missing if i]


def _band_item(*, band, kind, icon, label, title, cls, item_key="", due=None):
    course = cls.get("course_name") or ""
    period = cls.get("period")
    slug = re.sub(r"\s+", "-", f"{band}-{period}-{item_key or title}".lower())[:80]
    return {
        "id": slug,
        "standing_id": _standing_id(cls),
        "band": band,
        "kind": kind,
        "icon": icon,
        "label": label,
        "title": title,
        "course": course,
        "period": period,
        "due_key": due.isoformat() if due else "",
        "line": "class" if kind == "fact" else "item",
    }


def school_days_from(today, n=DUE_SOON_SCHOOL_DAYS):
    """n Mon–Fri dates starting at today when today is a school day."""
    day = _as_date(today)
    out = []
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def school_days_after(today, n=DUE_SOON_SCHOOL_DAYS):
    """Next n Mon–Fri dates after today. Skips Saturday and Sunday."""
    return school_days_from(_as_date(today) + timedelta(days=1), n)


def in_due_soon_window(due, today):
    """Due today or on the next school days in the 5-day window. Not weekend-only."""
    if due is None:
        return False
    return _as_date(due) in set(school_days_from(today))


def in_tonight_window(due, today):
    """Alias for the Due soon school-day window."""
    return in_due_soon_window(due, today)


def in_focus_window(due, today):
    """Alias for the Due soon school-day window."""
    return in_due_soon_window(due, today)


def _monday_of(day):
    day = _as_date(day)
    return day - timedelta(days=day.weekday())


def this_school_week_monday(today):
    """Monday of the current school week. Fri/Sat/Sun use the coming Monday."""
    day = _as_date(today)
    wd = day.weekday()
    if wd >= 4:
        return day + timedelta(days=(7 - wd) % 7)
    return day - timedelta(days=wd)


def _week_of_label(monday):
    monday = _as_date(monday)
    return f"Week of {monday.strftime('%a')}, {_month_day(monday)}"


def this_week_has_later_days(today):
    """True when this school week still has Mon–Fri dates after Tonight."""
    this_mon = this_school_week_monday(today)
    last_tonight = school_days_after(today)[-1]
    return any(this_mon + timedelta(days=i) > last_tonight for i in range(5))


def later_week_label(due, today):
    """This week / Next week / Week of Mon, Oct 5. School weeks are Mon–Fri."""
    if due is None:
        return ""
    week_mon = _monday_of(due)
    this_mon = this_school_week_monday(today)
    next_mon = this_mon + timedelta(days=7)
    if week_mon == this_mon and this_week_has_later_days(today):
        return "This week"
    if week_mon == next_mon:
        return "Next week"
    return _week_of_label(week_mon)


def group_later_weeks(items):
    """Adjacent items that share a week_label. Empty weeks are never created."""
    groups = []
    for item in items or []:
        label = item.get("week_label") or ""
        if not groups or groups[-1]["label"] != label:
            groups.append({"label": label, "items": []})
        groups[-1]["items"].append(item)
    return groups


def collect_due_soon(view_classes, today):
    """Unsubmitted work due today or on the next school days in the window."""
    today_d = _as_date(today)
    rows = []
    suppressed = 0
    seen = set()
    for cls in view_classes or []:
        course = (cls.get("course_name") or "").strip()
        if not course:
            continue
        for item in _class_raw_work(cls):
            name = _work_name(item)
            if not name:
                continue
            key = (course.lower(), name.lower())
            if key in seen:
                continue
            seen.add(key)
            due = _work_due_key(item)
            if _work_done(item):
                suppressed += 1
                continue
            if due is None or due < today_d:
                continue
            if not in_due_soon_window(due, today_d):
                continue
            rows.append(_band_item(
                band="due_soon", kind="due_soon", icon="→",
                label=due_when_label(due, today_d),
                title=name, cls=cls, item_key=name, due=due,
            ))
    rows.sort(key=lambda r: (r.get("due_key") or "", (r.get("title") or "").lower()))
    return rows, suppressed


def collect_bands(view_classes, today, last_checked=""):
    """Due soon only. Later / Today / fact chips are gone."""
    due_soon, suppressed = collect_due_soon(view_classes, today)
    return due_soon, [], [], suppressed


def _look_next_item(course, name, due):
    return {
        "course": course,
        "title": name,
        "iso": due.isoformat() if due else "",
        "date_label": _month_day(due) if due else "",
        "weekday_date": _weekday_date(due) if due else "",
    }


def _and_join(parts):
    parts = [p for p in parts if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def _date_span_label(dates):
    dates = sorted(d for d in dates if d)
    if not dates:
        return ""
    if dates[0] == dates[-1]:
        return _month_day(dates[0])
    return f"{_month_day(dates[0])}–{_month_day(dates[-1])}"


def look_next_packet(view_classes, today):
    """At most one fact of each kind. Skip a kind when it is not there.

    1. Nearest unsubmitted work due after the 5 school days. If that
       class has 3 or fewer such items, name them; otherwise the nearest
       only.
    2. Class with the most past due. Name items with dates if 3 or
       fewer; otherwise count and date span only.
    3. One item due inside the Due soon window that is turned in and
       has no score yet.
    """
    today_d = _as_date(today)
    window = set(school_days_from(today_d))
    past_rows = []
    outside_rows = []
    unscored_rows = []
    for cls in view_classes or []:
        course = (cls.get("course_name") or "").strip()
        if not course:
            continue
        past_items = []
        outside_items = []
        for item in _class_raw_work(cls):
            name = _work_name(item)
            if not name:
                continue
            due = _work_due_key(item)
            if (
                _work_turned_in(item)
                and item.get("points_earned") is None
                and due is not None
                and due in window
            ):
                unscored_rows.append((due, course, name, cls.get("period")))
            if _work_done(item):
                continue
            if _work_bucket(item, today_d) == "past_due":
                past_items.append(_look_next_item(course, name, due))
            elif due is not None and due > today_d and due not in window:
                outside_items.append(_look_next_item(course, name, due))
        if past_items:
            past_items.sort(key=lambda r: (r.get("iso") or "", (r.get("title") or "").lower()))
            past_rows.append((len(past_items), course, cls.get("period"), past_items))
        outside_rows.extend(outside_items)
    outside = None
    if outside_rows:
        outside_rows.sort(key=lambda r: (r.get("iso") or "", (r.get("course") or "").lower(), (r.get("title") or "").lower()))
        nearest = outside_rows[0]
        same = [
            r for r in outside_rows
            if (r.get("course") or "").lower() == (nearest.get("course") or "").lower()
        ]
        named = same if len(same) <= 3 else [nearest]
        outside = {
            "course": nearest["course"],
            "title": nearest["title"],
            "iso": nearest["iso"],
            "date_label": nearest["date_label"],
            "weekday_date": nearest["weekday_date"],
            "items": named,
        }
    past_due = None
    if past_rows:
        past_rows.sort(key=lambda r: (-r[0], (r[1] or "").lower(), str(r[2] or "")))
        n, course, _period, items = past_rows[0]
        dates = [_parse_iso(it.get("iso")) for it in items]
        past_due = {
            "course": course,
            "count": n,
            "date_span": _date_span_label(dates),
            "items": items if n <= 3 else [],
        }
    unscored = None
    if unscored_rows:
        unscored_rows.sort(key=lambda r: (r[0], r[1].lower(), r[2].lower()))
        due, course, name, _period = unscored_rows[0]
        unscored = _look_next_item(course, name, due)
    return {"outside": outside, "past_due": past_due, "unscored": unscored}


def _parse_iso(value):
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _past_due_sentence(fact):
    course = (fact.get("course") or "").strip()
    items = fact.get("items") or []
    if items:
        bits = [
            f"{it['title']} due {it['date_label']}"
            for it in items
            if it.get("title") and it.get("date_label")
        ]
        if bits:
            return f"{course} still has {_and_join(bits)}."
    n = fact.get("count") or 0
    span = (fact.get("date_span") or "").strip()
    if n and span:
        return f"{course} still has {n} out, {span}."
    if n:
        return f"{course} still has {n} out."
    return ""


def _outside_sentence(fact):
    course = (fact.get("course") or "").strip()
    items = fact.get("items") or []
    if not items and fact.get("title"):
        items = [fact]
    bits = []
    for it in items:
        title = (it.get("title") or "").strip()
        label = (it.get("date_label") or "").strip()
        if title and label:
            bits.append(f"{title} is due {label}")
    if not bits:
        return ""
    if len(bits) == 1:
        return f"{course} {bits[0]}."
    return f"{course} {_and_join(bits)}."


def _unscored_sentence(fact):
    title = (fact.get("title") or "").strip()
    if not title:
        return ""
    return f"{title} is already turned in, no score yet."


def look_next_sentences(packet):
    """Up to two short sentences. Skip a missing fact. Empty packet → one line."""
    packet = packet or {}
    parts = []
    past = _past_due_sentence(packet.get("past_due") or {})
    if past:
        parts.append(past)
    outside = _outside_sentence(packet.get("outside") or {})
    if outside:
        parts.append(outside)
    unscored = _unscored_sentence(packet.get("unscored") or {})
    if unscored:
        parts.append(unscored)
    if not parts:
        return LOOK_NEXT_EMPTY
    if len(parts) <= 2:
        return " ".join(parts)
    return f"{parts[0]} {parts[1][:-1]}; {parts[2]}"


def look_next_paragraph(view_classes, today):
    """Plain sentences from the packet. Grok may only rephrase this packet."""
    return look_next_sentences(look_next_packet(view_classes, today))


def look_next_known_titles(view_classes):
    titles = []
    for cls in view_classes or []:
        for item in _class_raw_work(cls):
            name = _work_name(item)
            if name:
                titles.append(name)
    return titles


def _packet_titles(packet):
    titles = []
    packet = packet or {}
    outside = packet.get("outside") or {}
    if outside.get("title"):
        titles.append(outside["title"])
    for it in outside.get("items") or []:
        if it.get("title"):
            titles.append(it["title"])
    past = packet.get("past_due") or {}
    if past.get("course"):
        titles.append(past["course"])
    for it in past.get("items") or []:
        if it.get("title"):
            titles.append(it["title"])
    unscored = packet.get("unscored") or {}
    if unscored.get("title"):
        titles.append(unscored["title"])
    if outside.get("course"):
        titles.append(outside["course"])
    if unscored.get("course"):
        titles.append(unscored["course"])
    return titles


def _packet_dates(packet):
    dates = set()
    packet = packet or {}

    def add_text(*texts):
        for text in texts:
            dates.update(extract_look_next_dates(text or ""))

    def add_iso(value):
        d = _parse_iso(value)
        if d:
            dates.add((d.month, d.day))

    outside = packet.get("outside") or {}
    add_iso(outside.get("iso"))
    add_text(outside.get("date_label"), outside.get("weekday_date"), outside.get("title"))
    for it in outside.get("items") or []:
        add_iso(it.get("iso"))
        add_text(it.get("date_label"), it.get("weekday_date"), it.get("title"))
    past = packet.get("past_due") or {}
    add_text(past.get("date_span"))
    for it in past.get("items") or []:
        add_iso(it.get("iso"))
        add_text(it.get("date_label"), it.get("weekday_date"), it.get("title"))
    unscored = packet.get("unscored") or {}
    add_iso(unscored.get("iso"))
    add_text(unscored.get("date_label"), unscored.get("weekday_date"), unscored.get("title"))
    return dates


_MONTH_NUM = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_LOOK_DATE_WORD = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+(\d{1,2})\b",
    re.I,
)
_LOOK_DATE_NUM = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/\d{2,4})?\b")


def extract_look_next_dates(text):
    """Calendar (month, day) pairs named in a sentence or title."""
    found = set()
    for m in _LOOK_DATE_WORD.finditer(text or ""):
        month = _MONTH_NUM.get(m.group(1).lower().rstrip("."))
        if month:
            found.add((month, int(m.group(2))))
    for m in _LOOK_DATE_NUM.finditer(text or ""):
        month, day = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            found.add((month, day))
    return found


def _title_in_text(title, text):
    title = (title or "").strip()
    if not title or not text:
        return False
    if re.search(re.escape(title), text, re.I):
        return True
    lead = re.match(r"^(\d+\.\d+[a-z]?)\b", title, re.I)
    if lead and re.search(r"\b" + re.escape(lead.group(1)) + r"\b", text, re.I):
        return True
    return False


def look_next_grok_accepts(text, packet, known_titles=None):
    """True only when every title and date in the sentence is in the packet."""
    text = (text or "").strip()
    if not text:
        return False
    if text == LOOK_NEXT_EMPTY:
        packet = packet or {}
        return not (packet.get("outside") or packet.get("past_due") or packet.get("unscored"))
    allowed_dates = _packet_dates(packet)
    if not allowed_dates and text != LOOK_NEXT_EMPTY:
        # A sentence that names a date the packet does not have is a miss.
        if extract_look_next_dates(text):
            return False
    for d in extract_look_next_dates(text):
        if d not in allowed_dates:
            return False
    allowed = {_norm_title(t) for t in _packet_titles(packet)}
    for title in known_titles or []:
        if _norm_title(title) in allowed:
            continue
        if _title_in_text(title, text):
            return False
    return True


def _norm_title(title):
    return re.sub(r"\s+", " ", (title or "").strip()).lower()


def look_next_for_page(view_classes, today, grok_text=""):
    """Sentences the page should show. Accept stored Grok text only if it matches."""
    packet = look_next_packet(view_classes, today)
    plain = look_next_sentences(packet)
    grok_text = (grok_text or "").strip()
    if grok_text and look_next_grok_accepts(grok_text, packet, look_next_known_titles(view_classes)):
        return packet, grok_text
    return packet, plain


def apply_look_next_grok(glance_obj, grok_text, view_classes=None, today=None):
    """Store Grok phrasing; keep plain sentences when a title or date is new."""
    glance_obj = glance_obj or {}
    text = (grok_text or "").strip()
    glance_obj["look_next_grok"] = text
    packet = glance_obj.get("look_next_packet")
    if packet is None and view_classes is not None:
        packet, plain = look_next_for_page(view_classes, today)
        glance_obj["look_next_packet"] = packet
        glance_obj["look_next"] = plain
    known = look_next_known_titles(view_classes) if view_classes is not None else None
    if text and look_next_grok_accepts(text, glance_obj.get("look_next_packet"), known):
        glance_obj["look_next"] = text
    return glance_obj


def collect_facts(view_classes, today):
    """One line per class that needs a look. Facts already on the page."""
    today_d = _as_date(today)
    lowest = _lowest_class(view_classes)
    forecast = pick_forecast(view_classes, today_d)
    when = _parse_mmdd((forecast or {}).get("due_date")) if forecast else None
    forecast_due_today = bool(forecast and when == today_d)
    rows = []
    for cls in view_classes or []:
        course = (cls.get("course_name") or "").strip()
        if not course:
            continue
        past_n = 0
        due_today = False
        for item in _class_raw_work(cls):
            if not _work_name(item):
                continue
            if _work_done(item):
                continue
            if _work_bucket(item, today_d) == "past_due":
                past_n += 1
            if _work_due_key(item) == today_d:
                due_today = True
        if (
            forecast_due_today
            and forecast.get("course") == course
            and forecast.get("period") == cls.get("period")
        ):
            match = matching_forecast_work(cls, when, forecast.get("title"))
            if not (match and _work_done(match)):
                due_today = True
        reasons = []
        if lowest is not None and _same_class(cls, lowest):
            reasons.append("lowest mark")
        if past_n == 1:
            reasons.append("1 past due")
        elif past_n > 1:
            reasons.append(f"{past_n} past due")
        if due_today:
            reasons.append("due today")
        if not reasons:
            continue
        rows.append(_band_item(
            band="facts", kind="fact", icon="•",
            label=" · ".join(reasons),
            title=course, cls=cls, item_key="fact",
        ))

    def sort_key(row):
        label = row.get("label") or ""
        past_m = re.search(r"(\d+) past due", label)
        past_n = int(past_m.group(1)) if past_m else 0
        return (
            0 if past_n else 1,
            -past_n,
            0 if "due today" in label else 1,
            0 if "lowest mark" in label else 1,
            str(row.get("period") or ""),
        )

    rows.sort(key=sort_key)
    return rows


def collect_tonight(view_classes, today, last_checked=""):
    """Due soon lines. Prefer collect_due_soon."""
    due_soon, suppressed = collect_due_soon(view_classes, today)
    return due_soon, [], suppressed


def _fmt_pts(n):
    if isinstance(n, float) and n == int(n):
        return str(int(n))
    return f"{n:g}"


def _work_name(item):
    return (
        (item or {}).get("name")
        or (item or {}).get("description")
        or (item or {}).get("title")
        or ""
    ).strip()


def _work_description(item):
    """Teacher sentence for the card. Not topic, rubric, or student answer."""
    name = _work_name(item)
    body = teacher_card_body(item, name)
    if body:
        return body
    comment = (
        (item or {}).get("teacher_comment")
        or (item or {}).get("comment")
        or ""
    ).strip()
    if comment and comment != name:
        return comment[:300]
    return ""


def _work_score(item):
    earned = (item or {}).get("points_earned")
    if earned is not None:
        poss = (item or {}).get("points_possible")
        if poss is not None:
            return f"{_fmt_pts(earned)}/{_fmt_pts(poss)}"
        return _fmt_pts(earned)
    raw = str((item or {}).get("score_raw") or "").strip()
    if raw and not raw.replace(".", "", 1).isdigit():
        return raw
    return "awaiting"


def _work_missing(item):
    if not item:
        return False
    if item.get("aeries_missing"):
        return True
    return (item.get("status") or "") == "missing"


def _work_turned_in(item):
    return bool((item or {}).get("turned_in")) or submitted_in_classroom(item)


def _work_turned_in_on(item):
    cr = (item or {}).get("classroom") or {}
    for raw in (
        cr.get("turned_in_on"),
        (item or {}).get("turned_in_on"),
        (item or {}).get("date_completed"),
    ):
        d = _parse_posted(raw) or _parse_mmdd(raw)
        if d:
            return d
    return None


def _work_done(item):
    """Classroom in or Aeries scored/completed. Same already-in as Tonight."""
    return _work_turned_in(item) or (item or {}).get("points_earned") is not None


def _work_bucket(item, today=None):
    """Past due / missing / coming up / turned in. Classroom-in is never missing."""
    if _work_done(item):
        return "turned_in"
    if _work_missing(item):
        return "missing"
    due = _work_due_key(item)
    today_d = _as_date(today) if today is not None else None
    if due is not None and today_d is not None and due < today_d:
        return "past_due"
    return "coming_up"


def _turned_in_phrase(item):
    if not _work_turned_in(item):
        return ""
    on = _work_turned_in_on(item)
    if on:
        return f"turned in {_weekday_date(on)}"
    return "turned in"


def _work_status(item):
    """Leftover Aeries state only. Turned-in date lives on the due line."""
    missing = _work_missing(item)
    if _work_turned_in(item) and missing:
        return "Aeries missing"
    if missing:
        return "missing"
    return ""


def _work_when(item, today):
    """Due / late and turned-in on one line: `due … / turned in …`.

    Late only if the due date passed AND Classroom does not show submitted
    AND Aeries does not show scored or handed in. Same reconcile as Tonight.
    Always weekday + date.
    """
    due = _work_due_key(item)
    scored = _work_done(item)
    due_s = due_when_label(due, today, done=scored)
    turned = _turned_in_phrase(item)
    if due_s and turned:
        return f"{due_s} / {turned}"
    return due_s or turned


def _work_row(item, today=None):
    bucket = _work_bucket(item, today)
    return {
        "name": _work_name(item),
        "description": _work_description(item),
        "score": _work_score(item),
        "score_class": _score_grade_class(item),
        "missing": bucket == "missing",
        "turned_in": _work_turned_in(item),
        "done": bucket == "turned_in",
        "bucket": bucket,
        "status": _work_status(item),
        "when": _work_when(item, today),
    }


def _work_due_key(item):
    due = _parse_mmdd((item or {}).get("due_date") or (item or {}).get("due"))
    if due is None:
        cr = (item or {}).get("classroom") or {}
        due = _parse_mmdd(cr.get("due") or cr.get("due_date")) or _parse_posted(cr.get("due"))
    return due


def _class_raw_work(cls):
    """Assignments plus Classroom-only cards. Same set the drawer lists."""
    raws = []
    seen = set()
    for a in (cls or {}).get("assignments") or []:
        name = _work_name(a)
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        raws.append(a)
    cr = (cls or {}).get("classroom") or {}
    for extra in cr.get("classroom_only") or []:
        title = (extra.get("title") or extra.get("name") or "").strip()
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        raws.append({
            "name": title,
            "due_date": extra.get("due_date") or extra.get("due") or "",
            "source": "classroom",
            "classroom": {
                "state": extra.get("state"),
                "state_label": extra.get("state_label"),
                "instructions": extra.get("instructions"),
                "description": extra.get("description"),
                "excerpt": extra.get("excerpt"),
                "due": extra.get("due"),
                "due_date": extra.get("due_date"),
                "turned_in_on": extra.get("turned_in_on"),
            },
        })
    return raws


def class_work_rows(cls, today=None):
    """Every assignment in this class. Not Tonight’s 0–3. No warehouse fields."""
    raws = _class_raw_work(cls)
    raws.sort(key=lambda a: (
        _BUCKET_RANK.get(_work_bucket(a, today), 9),
        _work_due_key(a) is None,
        -(_work_due_key(a).toordinal() if _work_due_key(a) else 0),
        _work_name(a).lower(),
    ))
    rows = []
    for item in raws:
        row = _work_row(item, today)
        if row["name"]:
            rows.append(row)
    return rows


_BUCKET_RANK = {"past_due": 0, "missing": 1, "coming_up": 2, "turned_in": 3}
_BUCKET_PHRASE = {
    "past_due": "past due",
    "missing": "missing",
    "coming_up": "coming up",
    "turned_in": "turned in",
}


def work_counts(rows):
    counts = {"past_due": 0, "missing": 0, "coming_up": 0, "turned_in": 0}
    for row in rows or []:
        key = row.get("bucket")
        if key in counts:
            counts[key] += 1
    return counts


def count_line(counts, *, show_zero_missing=False):
    """Standing chips: '1 past due · 2 missing · 3 coming up · 4 turned in'."""
    parts = []
    for key in ("past_due", "missing", "coming_up", "turned_in"):
        n = (counts or {}).get(key) or 0
        if n or (key == "missing" and show_zero_missing):
            parts.append(f"{n} {_BUCKET_PHRASE[key]}")
    return " · ".join(parts)


def standing_cards(view_classes, last_checked="", today=None):
    cards = []
    for cls in view_classes or []:
        name = (cls.get("course_name") or "").strip()
        if not name:
            continue
        mark, pct = _grade_display(cls)
        trend, symbol, text = _trend(cls)
        standing_line = f"{mark} {pct}".strip()
        work = class_work_rows(cls, today)
        counts = work_counts(work)
        line = count_line(counts)
        header = count_line(counts, show_zero_missing=bool(work))
        cards.append({
            "id": f"standing-{cls.get('period') or name}",
            "kind": "standing",
            "course": name,
            "period": cls.get("period"),
            "mark": mark,
            "percent": pct,
            "grade_class": _grade_class(cls, mark),
            "trend": trend,
            "trend_symbol": symbol,
            "trend_text": text,
            "counts": counts,
            "count_line": line,
            "drawer": {
                "kicker": header or "All the work",
                "title": name,
                "course": standing_line or name,
                "work": work,
                "counts": counts,
            },
        })
    return cards


def _last_checked_label(iso):
    raw = (iso or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        d = _parse_posted(raw)
        return _month_day(d) if d else ""
    return dt.strftime("%b %-d")


def _band_block(heading, subtitle, items, *, empty_line=None):
    if items:
        return {"heading": heading, "subtitle": subtitle, "items": items, "empty": False}
    if empty_line:
        return {
            "heading": heading,
            "subtitle": subtitle,
            "items": [],
            "empty": True,
            "empty_line": empty_line,
        }
    return None


def build_glance(view_classes, today, last_checked_iso=""):
    last_checked = _last_checked_label(last_checked_iso)
    standing = standing_cards(view_classes, last_checked=last_checked, today=today)
    due_soon, suppressed = collect_due_soon(view_classes, today)
    packet, look_next = look_next_for_page(view_classes, today)
    weekend_night = bool(weekend_dates(today))
    count = len(due_soon)
    empty = count == 0
    description = DUE_SOON_EMPTY if empty else (
        "One verified thing to handle." if count == 1
        else f"{count} verified things to handle."
    )
    due_soon_band = _band_block(
        DUE_SOON_HEADING,
        DUE_SOON_SUBTITLE,
        due_soon,
        empty_line=DUE_SOON_EMPTY,
    )
    return {
        "standing": standing,
        "bands": {
            "due_soon": due_soon_band,
            "focus": due_soon_band,
            "later": None,
            "today": None,
        },
        "tonight": {
            "weekend_night": weekend_night,
            "description": description,
            "empty": empty,
            "empty_line": DUE_SOON_EMPTY,
            "empty_detail": "",
            "items": due_soon,
            "later": [],
            "today": [],
            "weekend": None,
        },
        "facts": [],
        "look_next": look_next,
        "look_next_packet": packet,
        "look_next_grok": "",
        "suppressed": suppressed,
        "verified_count": count,
    }
