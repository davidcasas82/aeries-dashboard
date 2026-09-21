"""Official v1 glance: standing + Tonight bullets + drawer facts.

Shape 1: Tonight IS the briefing. Each slot is one class-level fact.
Reconcile is suppression (Classroom already-in is not a slot). Weekend
Pacific rule: Fri/Sat/Sun never label Monday work “due tonight.”

Nothing here logs student names or numbers.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from classroom import TURNED_IN_STATES

TONIGHT_LIMIT = 3
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
    if weekday_word and weekday_word.lower() not in title.lower():
        title = f"{title} {weekday_word}".strip()
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
    return {
        "id": f"forecast-{cls.get('period') or 'x'}",
        "kind": "forecast",
        "icon": "Q",
        "label": f"Coming {word}" if word else "Coming",
        "title": title,
        "course": cls.get("course_name") or "",
        "period": cls.get("period"),
        "due_date": when.strftime("%m/%d/%Y"),
        "source": "teacher",
        "drawer": {
            "kicker": f"Upcoming · {teacher_when}",
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


def _action(kind, item, cls, last_checked, *, weekend_label=None):
    name = (item.get("name") or item.get("title") or item.get("description") or "").strip()
    course = cls.get("course_name") or ""
    period = cls.get("period")
    missing = kind == "missing"
    labels = {
        "today": ("today", "•", "Due today"),
        "tomorrow": ("tomorrow", "→", "Due tomorrow"),
        "missing": ("missing", "!", "Missing"),
        "weekend": ("weekend", (weekend_label or "W")[:1], weekend_label or "This weekend"),
    }
    icon_kind, icon, label = labels[kind]
    return {
        "id": f"{kind}-{period}-{name}".lower().replace(" ", "-")[:80],
        "kind": icon_kind,
        "icon": icon,
        "label": label,
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


def collect_tonight(view_classes, today, last_checked=""):
    """0–3 verified actions plus optional weekend bucket. Suppressed items omitted."""
    today_d = _as_date(today)
    weekend = weekend_dates(today_d)
    due_today, missing, due_tomorrow, weekend_items = [], [], [], []
    suppressed = 0
    seen = set()

    def take(bucket, kind, item, cls, weekend_label=None):
        name = (item.get("name") or "").strip()
        key = ((cls.get("course_name") or "").lower(), name.lower())
        if not name or key in seen:
            return
        if submitted_in_classroom(item):
            return
        seen.add(key)
        bucket.append(_action(kind, item, cls, last_checked, weekend_label=weekend_label))

    for cls in view_classes or []:
        upcoming, miss_rows = _class_work(cls)
        for item in upcoming:
            if submitted_in_classroom(item):
                suppressed += 1
                continue
            due = _due_days(item)
            if due is None:
                continue
            if due == today_d:
                take(due_today, "today", item, cls)
                continue
            if weekend:
                label = next((name for name, d in weekend.items() if d == due), None)
                if label:
                    take(weekend_items, "weekend", item, cls, weekend_label=label)
                    continue
            elif due == today_d + timedelta(days=1):
                take(due_tomorrow, "tomorrow", item, cls)
        for item in miss_rows:
            if submitted_in_classroom(item):
                suppressed += 1
                continue
            take(missing, "missing", item, cls)

    items = []
    for row in due_today:
        items.append(row)
        if len(items) >= TONIGHT_LIMIT:
            break
    weekend_out = []
    if weekend:
        order = {"Saturday": 0, "Sunday": 1, "Monday": 2}
        weekend_items.sort(key=lambda i: order.get(i.get("label"), 9))
        for row in weekend_items:
            if len(items) + len(weekend_out) >= TONIGHT_LIMIT:
                break
            weekend_out.append(row)
    if len(items) + len(weekend_out) < TONIGHT_LIMIT:
        for row in missing:
            items.append(row)
            if len(items) + len(weekend_out) >= TONIGHT_LIMIT:
                break
    if not weekend and len(items) < TONIGHT_LIMIT:
        for row in due_tomorrow:
            items.append(row)
            if len(items) >= TONIGHT_LIMIT:
                break
    if len(items) + len(weekend_out) < TONIGHT_LIMIT:
        forecast = pick_forecast(view_classes, today_d)
        if forecast:
            key = ((forecast.get("course") or "").lower(), (forecast.get("title") or "").lower())
            already = {
                ((i.get("course") or "").lower(), (i.get("title") or "").lower())
                for i in items + weekend_out
            }
            if key not in already:
                items.append(forecast)

    items = items[: max(0, TONIGHT_LIMIT - len(weekend_out))]
    return items, weekend_out, suppressed


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
    """Assignment wording only. Never Drive/Doc excerpts, rubrics, or history."""
    name = _work_name(item)
    comment = (
        (item or {}).get("teacher_comment")
        or (item or {}).get("comment")
        or ""
    ).strip()
    if comment and comment != name:
        return comment[:300]
    cr = (item or {}).get("classroom") or {}
    desc = (cr.get("description") or "").strip()
    if desc and desc != name:
        return desc[:300]
    instr = (cr.get("instructions") or "").strip()
    if instr:
        first = instr.split("\n\n", 1)[0].strip()
        if first and first != name:
            return first[:300]
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


def _work_status(item):
    missing = _work_missing(item)
    turned = _work_turned_in(item)
    when = _work_turned_in_on(item)
    turned_s = f"turned in {_month_day(when)}" if turned and when else ("turned in" if turned else "")
    if turned and missing:
        return f"{turned_s} · Aeries missing" if turned_s else "turned in · Aeries missing"
    if missing:
        return "missing"
    if turned:
        return turned_s
    return ""


def _late_label(days):
    if days == 1:
        return "1 day late"
    return f"{days} days late"


def _work_when(item, today):
    """Due / upcoming / how late. Empty when the payload has no due date.

    Late only if the due date passed AND Classroom does not show submitted
    AND Aeries does not show scored or handed in. Same reconcile as Tonight.
    """
    due = _work_due_key(item)
    if due is None:
        return ""
    date = _month_day(due)
    today_d = _as_date(today) if today is not None else None
    if today_d is None:
        return f"due {date}"
    days = (due - today_d).days
    if days > 1:
        return f"due {date}"
    if days == 1:
        return "due tomorrow"
    if days == 0:
        return "due today"
    if submitted_in_classroom(item) or (item or {}).get("points_earned") is not None:
        return f"due {date}"
    return f"{_late_label(-days)} · due {date}"


def _work_row(item, today=None):
    return {
        "name": _work_name(item),
        "description": _work_description(item),
        "score": _work_score(item),
        "missing": _work_missing(item),
        "turned_in": _work_turned_in(item),
        "status": _work_status(item),
        "when": _work_when(item, today),
    }


def _work_due_key(item):
    due = _parse_mmdd((item or {}).get("due_date") or (item or {}).get("due"))
    if due is None:
        cr = (item or {}).get("classroom") or {}
        due = _parse_mmdd(cr.get("due") or cr.get("due_date")) or _parse_posted(cr.get("due"))
    return due


def class_work_rows(cls, today=None):
    """Every assignment in this class. Not Tonight’s 0–3. No warehouse fields."""
    rows = []
    seen = set()
    raws = []
    for a in cls.get("assignments") or []:
        name = _work_name(a)
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        raws.append(a)
    cr = cls.get("classroom") or {}
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
                "due": extra.get("due"),
                "due_date": extra.get("due_date"),
                "turned_in_on": extra.get("turned_in_on"),
            },
        })
    raws.sort(key=lambda a: (
        _work_due_key(a) is None,
        -(_work_due_key(a).toordinal() if _work_due_key(a) else 0),
        _work_name(a).lower(),
    ))
    for item in raws:
        row = _work_row(item, today)
        if row["name"]:
            rows.append(row)
    return rows


def standing_cards(view_classes, last_checked="", today=None):
    cards = []
    for cls in view_classes or []:
        name = (cls.get("course_name") or "").strip()
        if not name:
            continue
        mark, pct = _grade_display(cls)
        trend, symbol, text = _trend(cls)
        standing_line = f"{mark} {pct}".strip()
        cards.append({
            "id": f"standing-{cls.get('period') or name}",
            "kind": "standing",
            "course": name,
            "period": cls.get("period"),
            "mark": mark,
            "percent": pct,
            "trend": trend,
            "trend_symbol": symbol,
            "trend_text": text,
            "drawer": {
                "kicker": "All the work",
                "title": name,
                "course": standing_line or name,
                "work": class_work_rows(cls, today),
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


def build_glance(view_classes, today, last_checked_iso=""):
    last_checked = _last_checked_label(last_checked_iso)
    standing = standing_cards(view_classes, last_checked=last_checked, today=today)
    items, weekend, suppressed = collect_tonight(view_classes, today, last_checked=last_checked)
    weekend_night = bool(weekend_dates(today))
    count = len(items) + len(weekend)
    if count == 0:
        description = "Nothing is asking for attention."
        empty = True
    elif weekend_night and not items and weekend:
        description = "Nothing is due today. These are not submitted in Classroom."
        empty = False
    elif count == 1:
        description = "One verified thing to handle."
        empty = False
    else:
        description = f"{count} verified things to handle."
        empty = False
    return {
        "standing": standing,
        "tonight": {
            "weekend_night": weekend_night,
            "description": description,
            "empty": empty,
            "empty_line": "Nothing verified needs action.",
            "empty_detail": "Classroom and Aeries have no unfinished work to surface.",
            "items": items,
            "weekend": {
                "heading": "This weekend — turn in before Monday",
                "subtitle": "Due Saturday, Sunday, or Monday · not submitted in Classroom",
                "items": weekend,
            } if weekend else None,
        },
        "suppressed": suppressed,
        "verified_count": count,
    }
