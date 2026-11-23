"""Progress view from daily snapshots, the scored log, and the current gradebook.

Sentences and impact lines are built here. Impact points come from insights.explain_move,
and a cause is shown only when insights_writer.cause_allowed says the mini may state one.
Wins and risks are not ranked here; the page shows the mini's published tags.
"""

import json
from datetime import date, timedelta

import insights
import insights_writer
from class_match import assignments_for_class

HISTORY_START = date(2026, 9, 11)
STEP_DAYS = 14
STEP_DROP = 3.0
DECLINE_UNDER_HIGH = 5.0
RECENT_DAYS = 7
TIMELINE_POINTS = 3
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def js_round(value):
    """Math.round for a positive percent: half away from zero."""
    return int(value + 0.5)


def shown_letter(pct, mark):
    return insights.mark_letter(mark) or insights.letter_for(pct)


def shown_pair(pct, mark):
    return shown_letter(pct, mark), js_round(pct)


def one_decimal(value):
    scaled = abs(value) * 10
    whole = int(scaled + 0.5)
    return f"{whole // 10}.{whole % 10}"


def score_text(earned, possible):
    def fmt(number):
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            return str(number)
        if float(number).is_integer():
            return str(int(number))
        return f"{number:g}"

    return f"{fmt(earned)}/{fmt(possible)}"


def short_date(day):
    return f"{MONTHS[day.month - 1]} {day.day}"


def weekday_date(day):
    return f"{WEEKDAYS[day.weekday()]}, {short_date(day)}"


def _parse_day(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _series(snapshots, course):
    points = []
    for snap in snapshots or []:
        day = _parse_day(snap.get("date"))
        row = (snap.get("classes") or {}).get(course)
        if day is None or not isinstance(row, dict) or row.get("pct") is None:
            continue
        try:
            pct = float(row["pct"])
        except (TypeError, ValueError):
            continue
        letter, rounded = shown_pair(pct, row.get("mark"))
        points.append({
            "date": day,
            "pct": pct,
            "mark": row.get("mark") or "",
            "letter": letter,
            "rounded": rounded,
            "scores": row.get("scores") if isinstance(row.get("scores"), dict) else None,
            "scores_trimmed": bool(row.get("scores_trimmed")),
        })
    points.sort(key=lambda point: point["date"])
    return points


def _at(points, target):
    found = None
    for point in points:
        if point["date"] <= target and (found is None or point["date"] > found["date"]):
            found = point
    return found


def decline_flag(points, today):
    """Two or more two-week drops of 3+ points, still 5 under the high."""
    if not points:
        return False, 0, 0.0
    start = max(HISTORY_START, points[0]["date"])
    marks = []
    day = start
    while day <= today:
        hit = _at(points, day)
        if hit is not None:
            marks.append(hit["pct"])
        day += timedelta(days=STEP_DAYS)
    newest = _at(points, today)
    if newest is None:
        return False, 0, 0.0
    if not marks or marks[-1] != newest["pct"]:
        marks.append(newest["pct"])
    drops = 0
    for earlier, later in zip(marks, marks[1:]):
        if later - earlier <= -STEP_DROP:
            drops += 1
    high = max(point["pct"] for point in points)
    under = high - newest["pct"]
    return drops >= 2 and under >= DECLINE_UNDER_HIGH, drops, under


def _changed(previous, current):
    return previous["letter"] != current["letter"] or previous["rounded"] != current["rounded"]


def _moves(points):
    found = []
    for earlier, later in zip(points, points[1:]):
        if _changed(earlier, later):
            found.append((earlier, later))
    return found


def _grade_began(points, index):
    """First day the shown letter and rounded percent at index were continuously in effect."""
    shown = (points[index]["letter"], points[index]["rounded"])
    began = points[index]
    cursor = index
    while cursor > 0 and (points[cursor - 1]["letter"], points[cursor - 1]["rounded"]) == shown:
        cursor -= 1
        began = points[cursor]
    return began


def _article(letter):
    return "an" if letter in ("A", "F") else "a"


def was_now(points, today):
    """The line under a class that changed in the last 7 days, or None."""
    window = today - timedelta(days=RECENT_DAYS - 1)
    changes = [(earlier, later) for earlier, later in _moves(points) if later["date"] >= window]
    if not changes:
        return None
    letter_changes = [(earlier, later) for earlier, later in changes if earlier["letter"] != later["letter"]]
    earlier, _later = letter_changes[-1] if letter_changes else changes[-1]
    began = _grade_began(points, points.index(earlier))
    when = began["date"] if began["date"] >= window else earlier["date"]
    current = points[-1]
    direction = "up" if current["pct"] > earlier["pct"] else "down"
    text = (
        f"was {earlier['letter']} {earlier['rounded']}% {short_date(when)}"
        f" → now {current['letter']} {current['rounded']}%"
    )
    return {"text": text, "direction": direction}


def _impact_line(cause):
    earned, possible = cause.get("earned"), cause.get("possible")
    points = cause.get("points")
    if points is None or earned is None or possible is None:
        return None
    word = "up" if points > 0 else "down"
    return {
        "title": cause.get("title") or "",
        "score": score_text(earned, possible),
        "direction": word,
        "points": one_decimal(points),
        "text": f"{cause.get('title') or ''} · {score_text(earned, possible)} · {word} {one_decimal(points)}",
    }


def _impact_missing(move_day, log_rows, course):
    """Plain reason when explain_move cannot state a share."""
    named = [
        row for row in log_rows
        if row.get("class") == course and not row.get("baseline") and _parse_day(row.get("first_seen")) == move_day
    ]
    if named:
        row = named[0]
        score = ""
        if row.get("earned") is not None and row.get("possible") is not None:
            score = f" · {score_text(row['earned'], row['possible'])}"
        title = row.get("title") or "A new score"
        return {
            "text": (
                f"{title}{score} · impact not kept. "
                "The day before was not saved, so the point share can’t be computed."
            ),
            "impacts": [],
        }
    seen_days = [
        _parse_day(row.get("first_seen")) for row in log_rows
        if (row.get("class") == course or _norm_class(row.get("class")) == _norm_class(course))
    ]
    seen_days = [day for day in seen_days if day]
    if not seen_days or move_day < min(seen_days):
        return {
            "text": "Impact not kept. This is before the scored log. No assignment points were saved.",
            "impacts": [],
        }
    return {
        "text": "Impact not kept. Scores from the day before were not saved.",
        "impacts": [],
    }


def _model_for(class_meta, student, score_map):
    assignments = []
    for assignment in assignments_for_class(student, class_meta):
        key = insights.slug(assignment.get("description"))
        scored = score_map.get(key) if score_map else None
        if not isinstance(scored, (list, tuple)) or len(scored) < 2:
            continue
        assignments.append({
            **assignment,
            "points_earned": scored[0],
            "points_possible": scored[1],
        })
    return insights.build_class_model(
        class_meta.get("course_name") or "",
        class_meta.get("counted_insight"),
        assignments,
    )


# Snapshots keep score maps for three days. A map further out is a later gradebook, not the move.
SCORE_MAP_LAG_DAYS = 3


def _map_for_move(points, later, log_rows, course):
    """Score map saved on the move day or within SCORE_MAP_LAG_DAYS after it."""
    future = next((point for point in points if point["date"] >= later["date"] and point["scores"]), None)
    if future is None or future["date"] > later["date"] + timedelta(days=SCORE_MAP_LAG_DAYS):
        return None, None, False
    score_map = {}
    for key, value in future["scores"].items():
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                continue
        score_map[key] = value
    for row in log_rows:
        if row.get("class") != course or row.get("baseline"):
            continue
        seen = _parse_day(row.get("first_seen"))
        if seen and seen > later["date"]:
            score_map.pop(insights.slug(row.get("title")), None)
    return score_map, future, bool(future.get("scores_trimmed"))


def _explain(class_meta, student, course, earlier, later, points, log_rows):
    if earlier.get("scores") and later.get("scores"):
        prior, current = earlier["scores"], later["scores"]
        new_keys = [key for key in current if key not in prior]
        prior_scores = {}
        for key, value in current.items():
            old = prior.get(key)
            if not isinstance(old, (list, tuple)) or len(old) < 2 or not isinstance(value, (list, tuple)) or len(value) < 2:
                continue
            if (old[0], old[1]) != (value[0], value[1]):
                prior_scores[key] = (old[0], old[1])
        model = _model_for(class_meta, student, current)
        trimmed = earlier.get("scores_trimmed", False)
    else:
        score_map, _future, trimmed = _map_for_move(points, later, log_rows, course)
        if not score_map:
            return None
        new_keys = []
        for row in log_rows:
            if row.get("class") == course or _norm_class(row.get("class")) == _norm_class(course):
                if row.get("baseline"):
                    continue
                if _parse_day(row.get("first_seen")) == later["date"]:
                    new_keys.append(insights.slug(row.get("title")))
        if not new_keys:
            return None
        prior_scores = {}
        model = _model_for(class_meta, student, score_map)
    if model is None:
        return None
    fact = insights.explain_move(
        model, course, earlier["pct"], later["pct"], earlier["date"].isoformat(),
        new_keys, prior_scores, trimmed,
    )
    if fact is None or not insights_writer.cause_allowed(fact):
        return None
    lines = [line for line in (_impact_line(cause) for cause in fact.get("causes") or []) if line]
    return lines or None


def _norm_class(name):
    return insights.slug(name)


def _move_view(class_meta, student, course, earlier, later, log_rows, today, points):
    impacts = _explain(class_meta, student, course, earlier, later, points, log_rows) if class_meta is not None else None
    missing = None if impacts else _impact_missing(later["date"], log_rows, course)
    recent = later["date"] >= today - timedelta(days=RECENT_DAYS - 1)
    return {
        "date": later["date"].isoformat(),
        "when": weekday_date(later["date"]) if recent else short_date(later["date"]),
        "from_letter": earlier["letter"],
        "from_percent": earlier["rounded"],
        "to_letter": later["letter"],
        "to_percent": later["rounded"],
        "direction": "up" if later["pct"] > earlier["pct"] else "down",
        "impacts": impacts or [],
        "missing": None if impacts else missing["text"],
    }


def _log_rows(scored_log, student_key):
    students = (scored_log or {}).get("students") or {}
    entry = students.get(str(student_key)) or {}
    return [row for row in entry.get("items") or [] if isinstance(row, dict)]


def _weak_spots(class_meta):
    insight = (class_meta or {}).get("counted_insight") or {}
    spots = []
    for category in insight.get("categories") or []:
        if not isinstance(category, dict) or category.get("perc") is None:
            continue
        counts = bool(category.get("counts"))
        spots.append({
            "name": category.get("name") or "",
            "percent": js_round(float(category["perc"])) if counts or category.get("perc") else None,
            "weight": category.get("weight_pct"),
            "counts": counts,
            "kind": category.get("kind") or "",
        })
    return spots


def _trend_note(flagged, drops, points, today):
    if not points:
        return ""
    if flagged and points[0]["rounded"] >= 99:
        word = "drop" if drops == 1 else "drops"
        return f"{points[0]['rounded']}% in September, then {_count(drops)} later {word}."
    if flagged:
        word = "drop" if drops == 1 else "drops"
        return f"{_count(drops).capitalize()} {word}. Has not come back."
    recent = [(earlier, later) for earlier, later in _moves(points) if later["date"] >= today - timedelta(days=RECENT_DAYS - 1)]
    if recent and points[-1]["pct"] + 1 < max(point["pct"] for point in points) and points[-1]["pct"] > points[0]["pct"]:
        return f"Rose into a {recent[0][1]['letter']}, then down {WEEKDAYS[points[-1]['date'].weekday()]}."
    if points[-1]["rounded"] >= points[0]["rounded"] + 3 and points[-1]["letter"] != points[0]["letter"]:
        return f"Up from a {points[0]['letter']}."
    if not _moves(points):
        return "Steady."
    if len(recent) == 1 and recent[0][1]["pct"] < recent[0][0]["pct"] and recent[0][1]["letter"] != points[0]["letter"]:
        return "One drop. Impact not saved."
    return ""


def _count(number):
    return {1: "one", 2: "two"}.get(number, str(number))


def _summary(classes):
    flagged = [item for item in classes if item["decline"]]
    sentences = []
    if not flagged:
        sentences.append("No multi-week decline.")
    elif len(flagged) == 1:
        sentences.append("One class is in a multi-week decline.")
    else:
        sentences.append(f"{_count(len(flagged)).capitalize()} classes are in a multi-week decline.")
    if flagged:
        worst = max(flagged, key=lambda item: item["under_high"])
        times = "once" if worst["drop_steps"] == 1 else "twice" if worst["drop_steps"] == 2 else f"{worst['drop_steps']} times"
        sentences.append(
            f"{worst['course']} fell {times} and is still a {worst['letter']}, "
            f"about {js_round(worst['under_high'])} points under its high."
        )
        for item in flagged:
            if item is worst:
                continue
            if item["fell_this_week"]:
                sentences.append(f"{item['course']} fell again this week.")
    rising = [item for item in classes if item["rose"] and not item["decline"]]
    if rising:
        rising.sort(key=lambda item: item["series"][-1]["pct"] - item["series"][0]["pct"], reverse=True)
        sentences.append(f"{rising[0]['course']} moved up.")
    if not flagged:
        bare = [
            item for item in classes
            if item["recent"] and any(move["direction"] == "down" and move["from_letter"] != move["to_letter"] and not move["impacts"] for move in item["recent"])
        ]
        if bare:
            move = next(move for move in bare[0]["recent"] if move["direction"] == "down" and not move["impacts"])
            sentences.append(
                f"{bare[0]['course']} fell once, from {_article(move['from_letter'])} {move['from_letter']} "
                f"to {_article(move['to_letter'])} {move['to_letter']}, "
                "and the impact was not saved."
            )
        named = bare[0] if bare else None
        slipped = [
            item for item in classes
            if item is not named and any(
                move["from_letter"] == "A" and move["to_letter"] != "A" for move in item["recent"]
            )
        ]
        if slipped:
            sentences.append(f"{slipped[0]['course']} slipped off an A.")
    return " ".join(sentences)


def _class_block(course, class_meta, student, points, log_rows, today):
    flagged, drops, under = decline_flag(points, today)
    moves = [
        _move_view(class_meta, student, course, earlier, later, log_rows, today, points)
        for earlier, later in _moves(points)
        if earlier["letter"] != later["letter"] or abs(later["rounded"] - earlier["rounded"]) >= 1
    ]
    # Timeline hides sub-3-point same-letter wiggles. Recent keeps every shown change.
    window = today - timedelta(days=RECENT_DAYS - 1)
    recent = [move for move in moves if date.fromisoformat(move["date"]) >= window]
    earlier_moves = [
        move for move in moves
        if date.fromisoformat(move["date"]) < window
        and (move["from_letter"] != move["to_letter"] or abs(move["to_percent"] - move["from_percent"]) >= TIMELINE_POINTS)
    ]
    recent.sort(key=lambda move: move["date"], reverse=True)
    earlier_moves.sort(key=lambda move: move["date"], reverse=True)
    current = points[-1]
    fell_this_week = any(move["direction"] == "down" and date.fromisoformat(move["date"]) >= window for move in recent)
    rose = current["rounded"] >= points[0]["rounded"] + 3 and not flagged
    return {
        "course": course,
        "letter": current["letter"],
        "percent": current["rounded"],
        "decline": flagged,
        "drop_steps": drops,
        "under_high": under,
        "fell_this_week": fell_this_week,
        "rose": rose,
        "note": _trend_note(flagged, drops, points, today),
        "series": [{"date": point["date"].isoformat(), "pct": round(point["pct"], 1)} for point in points],
        "was_now": was_now(points, today),
        "recent": recent,
        "earlier": earlier_moves,
        "weak_spots": _weak_spots(class_meta),
    }


def _by_course(student):
    found = {}
    for class_meta in student.get("classes") or []:
        name = (class_meta.get("course_name") or "").strip()
        if name:
            found[name] = class_meta
    return found


def build_student(student, snapshots, log_rows, today):
    courses = _by_course(student)
    classes = []
    for course, points in ((name, _series(snapshots, name)) for name in courses):
        if not points or points[-1]["letter"] is None:
            continue
        if points[-1]["pct"] is None:
            continue
        classes.append(_class_block(course, courses.get(course), student, points, log_rows, today))
    classes = [item for item in classes if item["series"]]
    def rank(item):
        recent_drop = 0 if any(move["direction"] == "down" for move in item["recent"]) else 1
        return (0 if item["decline"] else 1, recent_drop, -item["under_high"], item["course"])
    classes.sort(key=rank)
    return {
        "name": student.get("name") or "",
        "sn": str(student.get("sn") or ""),
        "summary": _summary(classes),
        "classes": classes,
    }


def build_progress(history, scored_log, students, today=None):
    """View model for the progress page. `students` is the latest grade payload's list."""
    today = today or date.today()
    if isinstance(today, str):
        today = date.fromisoformat(today)
    built = []
    stored = (history or {}).get("students") or {}
    for student in students or []:
        key = str(student.get("sn") or "")
        entry = stored.get(key) or {}
        snapshots = entry.get("snapshots") or []
        if not snapshots and entry.get("name"):
            pass
        built.append(build_student(student, snapshots, _log_rows(scored_log, key), today))
    return {"as_of": today.isoformat(), "students": built}
