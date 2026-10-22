"""Grade facts for the insights job. Pure functions: code does the math, a model only words it.

Every fact is a dict with a stable `id` and a `kind`. A writer may only restate facts by id.
Scale is plain 90/80/70/60 with whole letters; +/- marks count as the bare letter.
"""

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import combinations
from math import factorial

SCALE = (("A", 90.0), ("B", 80.0), ("C", 70.0), ("D", 60.0))
SCALE_LABEL = "90/80/70/60"
MATCH_TOLERANCE = 0.51
MOVE_MIN_POINTS = 0.5
CAUSE_MIN_POINTS = 0.05
SHARE_MAX_CAUSES = 10
CAUSES_MAX = 8  # a move keeps its top causes; the rest fold into other_share
PATTERN_WINDOW_DAYS = 28
PATTERN_MIN_SCORES = 8
PATTERN_MIN_SHARE = 0.6
FACTS_CAP_BYTES = 40_000  # the whole serialized insight_facts, before fit_facts trims
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_STATUS_CODE = re.compile(r"\b(NA|TX|EX|NT)\b", re.IGNORECASE)


def slug(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def letter_for(pct):
    for letter, floor in SCALE:
        if pct >= floor:
            return letter
    return "F"


def letter_floor(pct):
    """(letter, floor) of the letter pct holds now, or None for an F, which has no floor to keep."""
    for letter, floor in SCALE:
        if pct >= floor:
            return letter, floor
    return None


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

    The lowest tenth whose grade, cut off at the tenths the way Aeries shows it, is at `target` or
    above, so it never names a score that leaves the grade at 89.9. 0.0 when any score does. The
    floor and cap clamp each score, so the grade only moves while the raw score is between them.
    """
    found = lowest_holding(model, bucket_name, points, target)
    if found is None:
        return None
    pct, always = found
    return 0.0 if always else pct


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


def _exact_grade(model):
    """Weighted class percent with no rounding, or None when nothing is scored."""
    total_w = total = 0.0
    for bucket in model.buckets:
        earned, possible = _bucket_totals(model, bucket)
        if possible <= 0:
            continue
        total_w += bucket.weight
        total += bucket.weight * earned / possible * 100.0
    return total / total_w if total_w > 0 else None


def truncated_grade(model):
    """The class percent the way Aeries shows it: cut off at the tenths, never rounded up.

    89.96 shows as 89.9, a B, where grade() would round it to 90.0.
    """
    exact = _exact_grade(model)
    return None if exact is None else math.floor(exact * 10.0 + 1e-9) / 10.0


def holds(model, bucket_name, pct, points, floor):
    """True when a raw `pct` on the next `points` in the bucket keeps the truncated grade at `floor` or above."""
    after = truncated_grade(with_score(model, bucket_name, pct / 100.0 * points, points))
    return after is not None and after >= floor


def _score_range(model):
    lowest = model.min_pct if model.min_pct is not None else 0.0
    ceiling = min(model.max_pct, 100.0) if model.max_pct is not None else 100.0
    return lowest, ceiling


def lowest_holding(model, bucket_name, points, floor):
    """(pct, always) for the next `points` in a bucket against a truncated-grade `floor`.

    pct is the lowest raw tenth that keeps truncated_grade at `floor` or above; always is True when
    even the lowest score the floor allows does. None when not even the cap does, or `points` is 0.
    Shared by the next-letter what-if (needed_score) and the keep-the-letter fact (keep_score).
    """
    if not points or points <= 0:
        return None
    lowest, ceiling = _score_range(model)
    if not holds(model, bucket_name, ceiling, points, floor):
        return None
    if holds(model, bucket_name, lowest, points, floor):
        return lowest, True
    # The grade is linear in the score between the clamps: start at the straight-line answer.
    low = _exact_grade(with_score(model, bucket_name, lowest / 100.0 * points, points))
    high = _exact_grade(with_score(model, bucket_name, ceiling / 100.0 * points, points))
    guess = lowest + (floor - low) / (high - low) * (ceiling - lowest) if high > low else ceiling
    pct = min(max(math.floor(guess * 10.0) / 10.0, lowest), ceiling)
    while pct < ceiling and not holds(model, bucket_name, pct, points, floor):
        pct = min(round(pct + 0.1, 1), ceiling)
    while pct - 0.1 >= lowest and holds(model, bucket_name, round(pct - 0.1, 1), points, floor):
        pct = round(pct - 0.1, 1)
    return pct, False


def keep_score(model, bucket_name, points, floor):
    """(needed_pct, cannot_lose) for keeping the current letter on the next `points` in a bucket.

    The lowest tenth that keeps the truncated grade at the letter's floor, so the score it names
    never lands at 89.99. (None, True) when the lowest score the floor allows still keeps the
    letter; (None, False) when not even the cap does. None when `points` is 0: an item worth
    nothing can't cost or keep anything.
    """
    if not points or points <= 0:
        return None
    found = lowest_holding(model, bucket_name, points, floor)
    if found is None:
        return None, False
    pct, always = found
    return (None, True) if always else (pct, False)


def _upcoming(cls, model, today):
    """[(due, title, bucket, points)] of unscored work due on or after today, soonest first.

    Filed into a bucket the same way build_class_model files a scored item.
    """
    counted = cls.get("counted_insight") or {}
    categories = [c for c in counted.get("categories") or [] if c.get("name")]
    summative = model.layout == "summative_formative"
    names = {b.name for b in model.buckets}
    items = []
    for a in cls.get("assignments") or []:
        possible = a.get("points_possible")
        if a.get("points_earned") is not None or not possible:
            continue
        due = _parse_due(a.get("due_date") or a.get("due"))
        title = (a.get("description") or a.get("title") or "").strip()
        cat = _category_bucket(categories, a.get("category"))
        if due is None or due < today or not title or cat is None:
            continue
        bucket = cat.get("kind") if summative else cat["name"]
        if bucket in names:
            items.append((due, title, bucket, float(possible)))
    return sorted(items)


def _parse_due(value):
    raw = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw[:10] if fmt == "%Y-%m-%d" else raw, fmt).date()
        except ValueError:
            continue
    return None


def keep_candidates(model, upcoming=(), move=None):
    """(bucket, points) to try for the class's one keep fact, in the order the writer picks one:
    the bucket of the soonest named next item (at its size), then the bucket of the biggest cause
    that pulled a drop down, then the heaviest bucket. Sizes default to the bucket's typical item."""
    typical = {b.name: _typical_sizes(b) for b in model.buckets}
    out = []
    for _due, _title, bucket, points in upcoming:
        out.append((bucket, points))
    if move and isinstance(move.get("delta"), (int, float)) and move["delta"] < 0:
        for cause in move.get("causes") or []:
            size = cause.get("share") if cause.get("share") is not None else cause.get("points")
            if (size or 0) < 0:
                out.extend((cause["bucket"], p) for p in typical.get(cause["bucket"], ()))
    heaviest = sorted(model.buckets, key=lambda b: (-live_weight_pct(model, b.name), b.name))
    for b in heaviest:
        out.extend((b.name, p) for p in typical[b.name])
    seen, ordered = set(), []
    for key in out:
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def keep_fact(model, pct, upcoming=(), move=None):
    """The class's one keep-the-letter fact, kind keep_letter, or None.

    Of the candidates (keep_candidates order) that can keep the letter, one that needs a score comes
    first, else one the next item can't cost. Kind keep_letter, never what_if, so no next-letter rule
    reads it. Only what the class entry doesn't already say: no threshold (the letter's floor), no
    bucket weight, no target.

    A can't-cost (cannot_lose) fact only for a named upcoming item, worked out at that item's posted
    points: at a typical size it is false whenever the real next item is bigger, so none is made.
    """
    held = letter_floor(pct)
    if held is None:
        return None
    letter, floor = held
    named = {(bucket, points) for _due, _title, bucket, points in upcoming or ()}
    safe = None
    for bucket, points in keep_candidates(model, upcoming, move):
        result = keep_score(model, bucket, points, floor)
        if result is None:
            continue
        needed, cannot_lose = result
        fact = {
            "id": f"keep:{slug(model.name)}:{slug(bucket)}:{points:g}",
            "kind": "keep_letter",
            "class": model.name,
            "letter": letter,
            "bucket": bucket,
            "points": points,
            "needed_pct": needed,
            "cannot_lose": cannot_lose,
        }
        if needed is not None:
            return fact
        if cannot_lose and safe is None and (bucket, points) in named:
            safe = fact
    return safe


def keep_search(model, pct, sizes=None):
    """Every bucket and size's keep answer, for tests and probes; the scrape emits only keep_fact's one.

    Same fields as keep_fact plus the floor (threshold) and reachable."""
    held = letter_floor(pct)
    if held is None:
        return []
    letter, floor = held
    facts = []
    for bucket in model.buckets:
        for points in (sizes or {}).get(bucket.name) or _typical_sizes(bucket):
            result = keep_score(model, bucket.name, points, floor)
            if result is None:
                continue
            needed, cannot_lose = result
            facts.append({
                "id": f"keep:{slug(model.name)}:{slug(bucket.name)}:{points:g}",
                "kind": "keep_letter",
                "class": model.name,
                "letter": letter,
                "threshold": floor,
                "bucket": bucket.name,
                "points": points,
                "needed_pct": needed,
                "cannot_lose": cannot_lose,
                "reachable": cannot_lose or needed is not None,
            })
    return facts


def _typical_sizes(bucket):
    """The median points-possible of what's scored there, so 'the next test' means a real size."""
    sizes = sorted(s.possible for s in bucket.scores if s.possible > 0)
    return (sizes[len(sizes) // 2],) if sizes else ()


def revert(model, changes):
    """The class as it stood before `changes`: {key: None} drops a new score, {key: (earned, possible)} restores an old one."""
    buckets = []
    for b in model.buckets:
        scores = []
        for s in b.scores:
            if s.key not in changes:
                scores.append(s)
            elif changes[s.key] is not None:
                earned, possible = changes[s.key]
                scores.append(Score(s.key, s.title, s.category, float(earned), float(possible)))
        buckets.append(Bucket(b.name, b.weight, tuple(scores)))
    return ClassModel(model.name, model.layout, tuple(buckets), model.min_pct, model.max_pct)


def _shares(model, changes):
    """Each change's order-averaged share of the move (Shapley). The shares sum to `explained` exactly.

    Leave-one-out costs (a cause's `points`) don't add up when a bucket gains several scores at once,
    so the shares are what a "down mainly from" sentence should rank by. None past SHARE_MAX_CAUSES.
    """
    keys = sorted(changes)
    n = len(keys)
    if n == 0 or n > SHARE_MAX_CAUSES:
        return None
    memo = {}

    def value(applied):
        if applied not in memo:
            memo[applied] = grade(revert(model, {k: changes[k] for k in keys if k not in applied}))
        return memo[applied]

    weights = [factorial(size) * factorial(n - size - 1) / factorial(n) for size in range(n)]
    shares = {}
    for key in keys:
        others = [k for k in keys if k != key]
        total = 0.0
        for size in range(n):
            for subset in combinations(others, size):
                with_key, without_key = value(frozenset(subset + (key,))), value(frozenset(subset))
                if with_key is None or without_key is None:
                    return None
                total += weights[size] * (with_key - without_key)
        shares[key] = total
    return shares


def explain_move(model, name, from_pct, to_pct, since, new_keys, prior_scores=None, scores_trimmed=False):
    """Why the posted % moved since `since`: each new or changed score's cost in grade points.

    new_keys: scores that weren't there at `since`. prior_scores: {key: (earned, possible)} for scores
    that were there but have changed since. A cause's `points` is the leave-one-out cost (undo that one
    change and rebuild); `share` is its order-averaged part of `explained`; `explained` is the move that
    undoing every change gives back; `coverage` is explained / delta. If the prior snapshot's score map
    was trimmed to fit, the move is unexplained (no causes) and coverage_trimmed says why.
    """
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
        "coverage": None,
        "coverage_trimmed": bool(scores_trimmed),
    }
    if model is None or scores_trimmed:
        return fact
    present = {s.key for b in model.buckets for s in b.scores}
    changes = {k: None for k in new_keys or [] if k in present}
    for key, old in (prior_scores or {}).items():
        if key in present and key not in changes and old is not None:
            changes[key] = (float(old[0]), float(old[1]))
    now = grade(model)
    if now is None or not changes:
        return fact
    before_all = grade(revert(model, changes))
    shares = _shares(model, changes) if before_all is not None else None
    seen = set()
    for bucket in model.buckets:
        for score in bucket.scores:
            if score.key not in changes or score.key in seen:
                continue
            seen.add(score.key)
            before = grade(revert(model, {score.key: changes[score.key]}))
            cost = round(now - before, 2) if before is not None else round(now - from_pct, 2)
            share = round(shares[score.key], 2) if shares else None
            if abs(cost) < CAUSE_MIN_POINTS and (share is None or abs(share) < CAUSE_MIN_POINTS):
                continue
            cause = {
                "id": f"cause:{slug(name)}:{since}:{score.key}",
                "title": score.title,
                "category": score.category,
                "bucket": bucket.name,
                "bucket_weight_pct": live_weight_pct(model, bucket.name),
                "change": "new" if changes[score.key] is None else "changed",
                "earned": score.earned,
                "possible": score.possible,
                "points": cost,
                "share": share,
            }
            if changes[score.key] is not None:
                cause["prior_earned"], cause["prior_possible"] = changes[score.key]
            fact["causes"].append(cause)
    fact["causes"].sort(key=lambda c: (-abs(c["share"] if c["share"] is not None else c["points"]), c["id"]))
    if before_all is not None:
        fact["explained"] = round(now - before_all, 2)
        fact["coverage"] = round(fact["explained"] / delta, 2)
    raw = {f"cause:{slug(name)}:{since}:{k}": v for k, v in shares.items()} if shares else None
    return fold_causes(fact, shares=raw)


def fold_causes(move, keep=None, shares=None):
    """Keep a move's top `keep` causes (by share, or points with no shares); fold the rest.

    Folded causes leave nothing behind, not even a title or id, so no sentence can name them.
    other_causes counts them. other_share is the sum of the folded causes' own shares (from the
    unrounded `shares` by cause id when given, else their rounded shares), never a leftover of
    `explained`. With no shares (more than SHARE_MAX_CAUSES changes) it is None: leave-one-out
    points don't add up, so there is no exact share to give. Coverage doesn't change.
    """
    keep = CAUSES_MAX if keep is None else keep
    causes = move.get("causes") or []
    if len(causes) <= keep:
        return move
    kept, folded = causes[:keep], causes[keep:]
    other = None
    if all(c.get("share") is not None for c in folded):
        other = round(sum((shares or {}).get(c["id"], c["share"]) for c in folded), 2)
    return {**move, "causes": kept, "other_causes": len(folded), "other_share": other}


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


def gate_reason(model, broken):
    if broken:
        return "scale_break"
    if model is None:
        return "no_footer_weights"
    return "weights_do_not_match_posted"


def class_facts(cls, snapshots, prior_snapshot, today):
    """Facts for one class. cls: name, pct, mark, counted_insight, assignments, new_keys, prior_scores, scores_trimmed."""
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
                model if trusted else None,
                name,
                from_pct,
                pct,
                prior_snapshot.get("date"),
                cls.get("new_keys") or [],
                cls.get("prior_scores"),
                cls.get("scores_trimmed", False),
            )
            if move:
                facts.append(move)
    if not trusted or broken:
        facts.append({
            "id": f"gate:{slug(name)}",
            "kind": "no_raise_math",
            "class": name,
            "reason": gate_reason(model, broken),
        })
        return facts
    step = next_letter(name, pct)
    if step:
        facts.append(step)
    # The posted letter and the letter the rebuilt grade shows (cut at the tenths) must agree before
    # any score is named: otherwise a what-if or keep score is about a grade that isn't on the page.
    posted = mark_letter(cls.get("mark")) or letter_for(pct)
    rebuilt = truncated_grade(model)
    if rebuilt is None or letter_for(rebuilt) != posted:
        return facts
    if step:
        facts.extend(what_if_search(model, step["threshold"]))
    move = next((f for f in facts if f.get("kind") == "grade_move"), None)
    keep = keep_fact(model, pct, _upcoming(cls, model, today), move)
    if keep:
        facts.append(keep)
    return facts


def student_facts(classes, snapshots, prior_snapshot, today, log_items=None):
    facts = []
    for cls in classes:
        if cls.get("pct") is None:
            continue
        facts.extend(class_facts(cls, snapshots, prior_snapshot, today))
    facts.extend(find_patterns(log_items, today))
    return fit_facts({"scale": SCALE_LABEL, "as_of": today.isoformat(), "facts": facts}, log_items)


def facts_size(doc):
    """Bytes of insight_facts as the payload carries it (compact JSON, UTF-8)."""
    return len(json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _pattern_last_seen(log_items):
    last = {}
    for item in log_items or []:
        if item.get("class") and item.get("first_seen"):
            last[item["class"]] = max(last.get(item["class"], ""), item["first_seen"])
    return last


def _kept_what_ifs(facts):
    """Per class, the one what-if about the next item: the class's keep-fact bucket and size when it
    has one (the soonest named item, else the drop's or the heaviest bucket), else its heaviest bucket."""
    keep_ids = {}
    for f in facts:
        if f.get("kind") == "keep_letter":
            keep_ids[f.get("class")] = (f.get("bucket"), f.get("points"))
    best = {}
    for f in facts:
        if f.get("kind") != "what_if":
            continue
        name = f.get("class")
        if keep_ids.get(name) == (f.get("bucket"), f.get("points")):
            best[name] = (float("inf"), f["id"])
        elif best.get(name, (-1.0, ""))[0] < (f.get("bucket_weight_pct") or 0.0):
            best[name] = (f.get("bucket_weight_pct") or 0.0, f["id"])
    return {fid for _w, fid in best.values()}


def fit_facts(doc, log_items=None, cap=FACTS_CAP_BYTES):
    """Trim insight_facts to `cap` bytes: keep facts first (last class first), then what-ifs past
    the next item, then the oldest patterns. grade_move facts and their causes are never trimmed,
    nor next_letter, scale_break or gate facts. doc["trimmed"] lists the steps that ran. If what's
    left is still over the cap, one ::warning:: line gives the size and the cap, nothing else."""
    if facts_size(doc) <= cap:
        return doc
    facts = list(doc["facts"])
    keep_next = _kept_what_ifs(facts)
    last_seen = _pattern_last_seen(log_items)
    steps = (
        ("keep_letter", [f["id"] for f in reversed(facts) if f.get("kind") == "keep_letter"]),
        ("what_if", [f["id"] for f in reversed(facts) if f.get("kind") == "what_if" and f["id"] not in keep_next]),
        ("posting_day", [f["id"] for f in sorted((f for f in facts if f.get("kind") == "posting_day"),
                                                 key=lambda f: (last_seen.get(f.get("class"), ""), f["id"]))]),
    )
    trimmed = []
    out = {**doc, "facts": facts}
    for step, ids in steps:
        for fid in ids:
            if facts_size(out) <= cap:
                break
            if step not in trimmed:
                trimmed.append(step)
                out["trimmed"] = trimmed
            facts = [f for f in facts if f["id"] != fid]
            out["facts"] = facts
    size = facts_size(out)
    if size > cap:
        # Only moves, causes and the per-class lines are left. Numbers only: no class or item names.
        print(f"::warning::insight_facts is {size} bytes after every trim step, over the {cap}-byte cap")
    return out
