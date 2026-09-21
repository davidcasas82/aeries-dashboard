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
    if not item:
        return False
    if item.get("turned_in") or item.get("turned_in_classroom"):
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
    cr = cls.get("classroom") or {}
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
            "link": note.get("link") or cr.get("link") or "https://classroom.google.com/",
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


def _item_link(item, cls):
    cr = (item or {}).get("classroom") or {}
    return (
        cr.get("link")
        or ((cls or {}).get("classroom") or {}).get("link")
        or "https://classroom.google.com/"
    )


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
            "link": _item_link(item, cls),
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


def standing_cards(view_classes, last_checked=""):
    cards = []
    for cls in view_classes or []:
        name = (cls.get("course_name") or "").strip()
        if not name:
            continue
        mark, pct = _grade_display(cls)
        trend, symbol, text = _trend(cls)
        cr = cls.get("classroom") or {}
        notes = newest_announcements(cr.get("announcements"), limit=1)
        words = (notes[0]["text"] if notes else "") or "No teacher wording in this export."
        classroom_line = "No unfinished Classroom work in the current window."
        only = cr.get("classroom_only") or []
        if only:
            top = only[0]
            classroom_line = _classroom_status({
                "name": top.get("title"),
                "due_date": top.get("due_date"),
                "classroom": top,
            })
        catch = cr.get("turned_in_aeries_missing") or []
        if catch:
            when = _month_day(_parse_posted(catch[0].get("turned_in_on")))
            classroom_line = (
                f"{catch[0].get('title') or 'Work'} turned in"
                + (f" {when}" if when else "")
                + " · Aeries has not caught up"
            )
        aeries_line = _aeries_status(None, cls, last_checked)
        cards.append({
            "id": f"standing-{cls.get('period') or name}",
            "course": name,
            "period": cls.get("period"),
            "mark": mark,
            "percent": pct,
            "trend": trend,
            "trend_symbol": symbol,
            "trend_text": text,
            "drawer": {
                "kicker": "Standing · verified detail",
                "title": name,
                "course": name,
                "teacher_words": words,
                "classroom": classroom_line,
                "aeries": aeries_line,
                "link": cr.get("link") or "https://classroom.google.com/",
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
    standing = standing_cards(view_classes, last_checked=last_checked)
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
