"""Grade facts for the insights job. Pure functions: code does the math, a model only words it.

Every fact is a dict with a stable `id` and a `kind`. A writer may only restate facts by id.
Scale is plain 90/80/70/60 with whole letters; +/- marks count as the bare letter.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

SCALE = (("A", 90.0), ("B", 80.0), ("C", 70.0), ("D", 60.0))
SCALE_LABEL = "90/80/70/60"
MATCH_TOLERANCE = 0.51
MOVE_MIN_POINTS = 0.5
CAUSE_MIN_POINTS = 0.05
PATTERN_WINDOW_DAYS = 28
PATTERN_MIN_SCORES = 8
PATTERN_MIN_SHARE = 0.6
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_STATUS_CODE = re.compile(r"\b(NA|TX|EX|NT)\b", re.IGNORECASE)


def slug(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def letter_for(pct):
    for letter, floor in SCALE:
        if pct >= floor:
            return letter
    return "F"


def next_threshold(pct):
    """(letter, floor) of the next letter up, or None at an A."""
    above = [(letter, floor) for letter, floor in SCALE if floor > pct]
    return above[-1] if above else None


def mark_letter(mark):
    letter = (mark or "").strip()[:1].upper()
    return letter if letter and letter in "ABCDF" else None


# ── Class model: the weighted buckets a class grade is built from ───────────


@dataclass(frozen=True)
class Score:
    key: str
    title: str
    category: str
    earned: float
    possible: float


@dataclass(frozen=True)
class Bucket:
    name: str
    weight: float
    scores: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class ClassModel:
    name: str
    layout: str
    buckets: tuple
    min_pct: float = None
    max_pct: float = None


def _effective(score, min_pct, max_pct):
    """(earned, possible) as the grade counts it. Extra credit adds earned with no possible."""
    if score.possible == 0:
        return score.earned, 0.0
    pct = score.earned / score.possible * 100.0
    if min_pct is not None:
        pct = max(pct, min_pct)
    if max_pct is not None:
        pct = min(pct, max_pct)
    return pct / 100.0 * score.possible, score.possible


def _bucket_totals(model, bucket):
    earned = possible = 0.0
    for score in bucket.scores:
        e, p = _effective(score, model.min_pct, model.max_pct)
        earned += e
        possible += p
    return earned, possible


def grade(model):
    """Weighted class percent. Buckets with nothing scored drop out, like Aeries."""
    total_w = total = 0.0
    for bucket in model.buckets:
        earned, possible = _bucket_totals(model, bucket)
        if possible <= 0:
            continue
        total_w += bucket.weight
        total += bucket.weight * earned / possible * 100.0
    return round(total / total_w, 2) if total_w > 0 else None


def live_weight_pct(model, bucket_name):
    """Share of the grade a bucket carries right now, after empty buckets drop out."""
    live = [b for b in model.buckets if _bucket_totals(model, b)[1] > 0]
    total_w = sum(b.weight for b in live)
    for b in live:
        if b.name == bucket_name and total_w > 0:
            return round(b.weight / total_w * 100.0, 1)
    return 0.0


def without(model, keys):
    keys = set(keys)
    return ClassModel(
        model.name,
        model.layout,
        tuple(Bucket(b.name, b.weight, tuple(s for s in b.scores if s.key not in keys)) for b in model.buckets),
        model.min_pct,
        model.max_pct,
    )


def with_score(model, bucket_name, earned, possible):
    return ClassModel(
        model.name,
        model.layout,
        tuple(
            Bucket(b.name, b.weight, b.scores + (Score("whatif", "", "", earned, possible),))
            if b.name == bucket_name else b
            for b in model.buckets
        ),
        model.min_pct,
        model.max_pct,
    )


def _category_bucket(categories, category):
    """Footer category an assignment belongs to: exact name first, then containment."""
    want = slug(category)
    if not want:
        return None
    for cat in categories:
        if slug(cat.get("name")) == want:
            return cat
    for cat in categories:
        name = slug(cat.get("name"))
        if name and (want in name or name in want):
            return cat
    return None


def build_class_model(name, counted_insight, assignments):
    """ClassModel from the footer weights (counted_insight) and the scored assignments."""
    if not counted_insight or not counted_insight.get("categories"):
        return None
    layout = counted_insight.get("layout") or "percent_of_grade"
    categories = [c for c in counted_insight["categories"] if c.get("name")]
    if layout == "summative_formative":
        weights = {
            "summative": counted_insight.get("summative_weight_pct"),
            "formative": counted_insight.get("formative_weight_pct"),
        }
        bucket_of = lambda cat: cat.get("kind") if cat.get("kind") in weights else None  # noqa: E731
    else:
        weights = {c["name"]: c.get("weight_pct") for c in categories}
        bucket_of = lambda cat: cat["name"]  # noqa: E731
    scores = {}
    for a in assignments or []:
        earned = a.get("points_earned")
        possible = a.get("points_possible")
        if earned is None or possible is None or _STATUS_CODE.search(str(a.get("score_raw") or "")):
            continue
        cat = _category_bucket(categories, a.get("category"))
        bucket = bucket_of(cat) if cat else None
        if bucket is None:
            continue
        title = (a.get("description") or "").strip()
        scores.setdefault(bucket, []).append(Score(
            slug(title), title, (a.get("category") or "").strip(), float(earned), float(possible),
        ))
    buckets = tuple(
        Bucket(bucket, float(weight), tuple(scores.get(bucket, ())))
        for bucket, weight in weights.items()
        if weight not in (None, 0)
    )
    if not buckets:
        return None
    return ClassModel(
        name,
        layout,
        buckets,
        counted_insight.get("min_assignment_pct"),
        counted_insight.get("max_assignment_pct"),
    )


# ── Facts ────────────────────────────────────────────────────────────────────


def scale_break(name, posted, snapshots):
    """Days where the mark's letter isn't the letter its % gets on 90/80/70/60."""
    points = [(s.get("date"), *_class_point(s, name)) for s in snapshots or []] + [posted]
    days = []
    for day, pct, mark in points:
        if pct is None or not (mark or "").strip():
            continue
        if mark_letter(mark) != letter_for(pct):
            days.append({"date": day, "pct": pct, "mark": mark})
    if not days:
        return None
    unique = {d["date"]: d for d in days}
    ordered = [unique[k] for k in sorted(unique)]
    return {
        "id": f"scale:{slug(name)}",
        "kind": "scale_break",
        "class": name,
        "scale": SCALE_LABEL,
        "days": len(ordered),
        "examples": ordered[-3:],
    }


def _class_point(snapshot, name):
    row = (snapshot.get("classes") or {}).get(name) or {}
    return row.get("pct"), row.get("mark") or ""


def next_letter(name, pct):
    target = next_threshold(pct)
    if target is None:
        return None
    letter, floor = target
    return {
        "id": f"next:{slug(name)}",
        "kind": "next_letter",
        "class": name,
        "pct": pct,
        "letter": letter_for(pct),
        "next_letter": letter,
        "threshold": floor,
        "gap": round(floor - pct, 2),
    }


def needed_score(model, bucket_name, points, target):
    """Raw percent needed on the next `points` in a bucket to reach `target`, or None if no score can.

    The floor and cap clamp each score, so the grade only moves while the raw score is between them.
    """
    floor = model.min_pct if model.min_pct is not None else 0.0
    ceiling = min(model.max_pct, 100.0) if model.max_pct is not None else 100.0
    low = grade(with_score(model, bucket_name, floor / 100.0 * points, points))
    high = grade(with_score(model, bucket_name, ceiling / 100.0 * points, points))
    if high is None or low is None or high < target:
        return None
    if low >= target:
        return 0.0
    return round(floor + (target - low) / (high - low) * (ceiling - floor), 1)


def what_if_search(model, target, sizes=None):
    """For each bucket and next-assignment size, the score that reaches target."""
    facts = []
    for bucket in model.buckets:
        for points in (sizes or {}).get(bucket.name) or _typical_sizes(bucket):
            pct = needed_score(model, bucket.name, points, target)
            facts.append({
                "id": f"whatif:{slug(model.name)}:{slug(bucket.name)}:{points:g}",
                "kind": "what_if",
                "class": model.name,
                "bucket": bucket.name,
                "bucket_weight_pct": live_weight_pct(with_score(model, bucket.name, 0.0, points), bucket.name),
                "points": points,
                "target": target,
                "needed_pct": pct,
                "reachable": pct is not None and pct <= 100.0,
            })
    return facts


def _typical_sizes(bucket):
    """The median points-possible of what's scored there, so 'the next test' means a real size."""
    sizes = sorted(s.possible for s in bucket.scores if s.possible > 0)
    return (sizes[len(sizes) // 2],) if sizes else ()


def explain_move(model, name, from_pct, to_pct, since, new_keys):
    """Why the posted % moved since `since`: each newly scored item's cost in grade points."""
    delta = round(to_pct - from_pct, 2)
    if abs(delta) < MOVE_MIN_POINTS:
        return None
    fact = {
        "id": f"move:{slug(name)}:{since}",
        "kind": "grade_move",
        "class": name,
        "since": since,
        "from_pct": from_pct,
        "to_pct": to_pct,
        "delta": delta,
        "causes": [],
        "explained": None,
    }
    if model is None or not new_keys:
        return fact
    now = grade(model)
    present = {s.key for b in model.buckets for s in b.scores}
    keys = [k for k in new_keys if k in present]
    if now is None or not keys:
        return fact
    for bucket in model.buckets:
        for score in bucket.scores:
            if score.key not in keys:
                continue
            before = grade(without(model, [score.key]))
            cost = round(now - before, 2) if before is not None else round(now - from_pct, 2)
            if abs(cost) < CAUSE_MIN_POINTS:
                continue
            fact["causes"].append({
                "id": f"cause:{slug(name)}:{since}:{score.key}",
                "title": score.title,
                "category": score.category,
                "bucket": bucket.name,
                "bucket_weight_pct": live_weight_pct(model, bucket.name),
                "earned": score.earned,
                "possible": score.possible,
                "points": cost,
            })
    fact["causes"].sort(key=lambda c: (-abs(c["points"]), c["id"]))
    before_all = grade(without(model, keys))
    if before_all is not None:
        fact["explained"] = round(now - before_all, 2)
    return fact


def find_patterns(log_items, today, window_days=PATTERN_WINDOW_DAYS):
    """Posting-day pattern per class from the scored-items log. Empty until the log is deep enough.

    log_items are scored-log entries: class, first_seen (YYYY-MM-DD), scrape, baseline.
    An overnight scrape's scores count toward the day before, when they were posted.
    """
    since = (today - timedelta(days=window_days)).isoformat()
    by_class = {}
    for item in log_items or []:
        if item.get("baseline") or (item.get("first_seen") or "") < since:
            continue
        posted = date.fromisoformat(item["first_seen"])
        if item.get("scrape") == "overnight":
            posted -= timedelta(days=1)
        by_class.setdefault(item["class"], []).append(posted.weekday())
    facts = []
    for name, days in sorted(by_class.items()):
        if len(days) < PATTERN_MIN_SCORES:
            continue
        top = max(range(7), key=lambda d: (days.count(d), -d))
        share = days.count(top) / len(days)
        if share < PATTERN_MIN_SHARE:
            continue
        facts.append({
            "id": f"pattern:posting_day:{slug(name)}",
            "kind": "posting_day",
            "class": name,
            "weekday": WEEKDAYS[top],
            "share": round(share, 2),
            "scores": len(days),
            "window_days": window_days,
        })
    return facts


def class_facts(cls, snapshots, prior_snapshot, today):
    """Facts for one class. cls: name, pct, mark, counted_insight, assignments, new_keys."""
    name, pct = cls["name"], cls["pct"]
    model = build_class_model(name, cls.get("counted_insight"), cls.get("assignments"))
    model_pct = grade(model) if model else None
    matches = (cls.get("counted_insight") or {}).get("matches_posted") is True
    trusted = matches and model_pct is not None and abs(model_pct - pct) < MATCH_TOLERANCE
    facts = []
    broken = scale_break(name, (today.isoformat(), pct, cls.get("mark") or ""), snapshots)
    if broken:
        facts.append(broken)
    if prior_snapshot:
        from_pct, _ = _class_point(prior_snapshot, name)
        if from_pct is not None:
            move = explain_move(
                model if trusted else None, name, from_pct, pct, prior_snapshot.get("date"), cls.get("new_keys") or []
            )
            if move:
                facts.append(move)
    if not trusted or broken:
        facts.append({
            "id": f"gate:{slug(name)}",
            "kind": "no_raise_math",
            "class": name,
            "reason": "scale_break" if broken else "weights_do_not_match_posted",
        })
        return facts
    step = next_letter(name, pct)
    if step:
        facts.append(step)
        facts.extend(what_if_search(model, step["threshold"]))
    return facts


def student_facts(classes, snapshots, prior_snapshot, today, log_items=None):
    facts = []
    for cls in classes:
        if cls.get("pct") is None:
            continue
        facts.extend(class_facts(cls, snapshots, prior_snapshot, today))
    facts.extend(find_patterns(log_items, today))
    return {"scale": SCALE_LABEL, "as_of": today.isoformat(), "facts": facts}
