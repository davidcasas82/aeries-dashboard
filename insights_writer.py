"""The local write-up: Briefing headline and class stories built from insight_facts.

The writer reads only a student's insight_facts (from the scrape) and the scored-items
log (for posting-day patterns). It never sees the analytics dump, so it can't restate
the past-due list, which stays a plain code list on the page.

Each class runs as a pipeline:
  1. No filler: a class with no grade move, no reachable what-if and no pattern gets no story
     and no model call.
  2. The model drafts sentences, and each sentence cites fact ids.
  3. check_sentence (code) drops any sentence whose numbers, letters, dates, weekdays or wording
     aren't backed by the facts it cites. Spelled-out numbers are read as numbers first.
  4. A vote pass asks the model, several times, whether each surviving sentence is backed;
     a sentence needs a majority.
  5. eval_story (code) rejects the story unless it cites a grade_move, what_if or pattern
     fact, keeps the insight order, and never restates past-due or missing work.
"""

import json
import math
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

import insights

INSIGHT_KINDS = ("grade_move", "what_if", "posting_day")
# Story order: letter and distance, then the cause, then the what-if, then a pattern.
ORDER = {"next_letter": 0, "scale_break": 0, "grade_move": 1, "what_if": 2, "posting_day": 3}
VOTES = 3
# A move's causes may be stated only when they explain it: same sign, within 20% of delta.
# The engine's coverage field (explained / delta) carries both: 0.8 <= coverage <= 1.2.
CAUSE_TOLERANCE = 0.20
_CAUSE_CLAIM_RE = re.compile(
    r"\b((?:mainly|mostly|largely|primarily|chiefly)\s+(?:\w+\s+)?(?:from|because|due|thanks|owing|driven|"
    r"caused|after|by)|because|due to|owing to|thanks to|driven by|caused by|"
    r"result of|as a result|on account of|pulled|dragged|hurt|lifted|boosted|cost|came from|comes from|"
    r"after an? \d|from an? \d)",
    re.I,
)
PUBLISH_FLAG = "GRADE_INSIGHTS_PUBLISH"

WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEKDAY_RE = re.compile(r"\b(mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?s?\b", re.I)
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
SHORT_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTH_DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? (\d{1,2})(?:st|nd|rd|th)?\b", re.I
)
# N/M is a date only when it reads as month/day and isn't a cited score (31/50 is a score).
_SLASH_RE = re.compile(r"(?<![\w/.])(\d+(?:\.\d+)?) ?/ ?(\d+(?:\.\d+)?)(?![\w/])")
# Relative days never appear: the write-up is read on a later day than it was written.
_RELATIVE_RE = re.compile(
    r"\b(today|tomorrow|tonight|yesterday|(?:this|next|last) (?:week(?:end)?|month)|"
    r"(?:next|this|last|coming|upcoming) (?:mon|tues|wednes|thurs|fri|satur|sun)day|"
    r"in (?:\d+|a few|two|three|four|five|six) days)\b",
    re.I,
)
# Restating the to-do list is a FAIL: that list is code, not story.
_RESTATE_RE = re.compile(
    r"\b(missing|past[- ]due|overdue|late|not (?:been )?turned in|turned in|unsubmitted|"
    r"incomplete|awaiting|pending|not (?:yet )?graded|due(?! to\b))\b",
    re.I,
)
_ZERO_GRADE_RE = re.compile(r"(?<![\d.])0(?:\.0+)?\s?%")
# Digits after a bare period count too ("Ch.9"); a decimal's tail is part of its number.
_NUMBER_RE = re.compile(r"(?<!\w)(?<!\d\.)\d+(?:\.\d+)?")
_LETTER_RE = re.compile(r"(?<![\w'’])([A-DF])(?![\w'’+-])")
# The scale is whole letters only, so any plus or minus letter is wrong on its face.
_PLUS_MINUS_RE = re.compile(r"(?<![\w'’])([A-DF])(?:[+−–]|-(?![A-Za-z0-9])| ?-?(?:plus|minus)\b)", re.I)

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
         "sixteen seventeen eighteen nineteen").split()
_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
_WORD_VALUE = {**{w: i for i, w in enumerate(_ONES)}, **{w: 20 + 10 * i for i, w in enumerate(_TENS)}}
_W = r"(?:%s)" % "|".join(sorted(_WORD_VALUE, key=len, reverse=True))
_SPELLED_RE = re.compile(
    rf"\b(?:(?:a|one) hundred|{_W}(?:[- ]{_W})?)(?: point(?: {_W})+)?\b(?P<unit>\s*(?:-?points?|pts|percent|%|"
    rf"percentage points?|letters?|grades?))?",
    re.I,
)
# Which fact fields a number may come from, by what the words around it say it is.
_MOVE_BEFORE_RE = re.compile(
    r"\b(?:up|down|by|rose|fell|dropped|dipped|slipped|slid|climbed|jumped|gained|lost|cost|costs|costing|"
    r"moved|took|knocked)\s+(?:about |nearly |almost |roughly |just |over |under )?$",
    re.I,
)
_WEIGHT_AFTER_RE = re.compile(r"^\s*(?:%|percent)\s+of\s+(?:the |its |their )?(?:grade|class|total|mark)\b", re.I)
_WEIGHT_BEFORE_RE = re.compile(r"\b(?:worth|weighted|weight(?:ed)? (?:at|of)|weighs|counts?(?: for)?)\s+$", re.I)
_DISTANCE_AFTER_RE = re.compile(
    r"^\s*(?:%|percent|percentage points?|points?|pts\.?)?\s*(?:from|away|short|shy|below|under|to go|off)\b", re.I
)
ROLE_FIELDS = {
    "move": ("delta", "explained", "points", "share"),
    "weight": ("bucket_weight_pct",),
    "distance": ("gap",),
}
# Ratios and weights are never a general number: a weight needs weight words around it.
_NOT_GENERAL = ("bucket_weight_pct", "coverage")

DRAFT_SYSTEM = """You write one short class story for a parent's grade dashboard.
You get FACTS as JSON. Every fact has an id. Use only those facts.

Write 1 to 4 sentences, in this order, skipping any part with no fact:
1. The letter and the distance to the next letter (next_letter).
2. What moved the grade: the item, its score, its category's weight and the points it cost (grade_move and its causes).
3. The what-if: the score needed on the next item of that size to reach the next letter (what_if).
4. A pattern, if there is one (posting_day).

Rules:
- Every sentence cites the ids it uses. A cause cites its own cause id.
- Any "because", "mainly", "due to" or "driven by" wording cites the grade_move or cause it is about.
- Name causes in the order they are listed, largest share first. "Mainly" is only for the first cause.
- If a grade_move has causes_withheld, state the move (from, to, delta) but give no cause for it.
- Use only numbers that appear in the cited facts, written as digits. Round to one decimal at most.
- Letters are whole letters only (A, B, C, D, F). Never write a plus or minus grade.
- Don't mention missing, late, past-due, turned-in or awaiting work. The dashboard lists that separately.
- Never write a relative day: no "today", "tomorrow", "this Thursday" or "next Friday".
- If a what_if fact has next_title and next_due_label, name that item with its date: "the Ch. 5 Test, due Oct 30". Otherwise say "the next test" or "the next quiz". No other dates.
- A weekday appears only in a posting-day pattern ("usually posts Fridays"), citing that posting_day fact.
- Never call a 0% a grade.
- If nothing here is worth saying, return {"sentences": []}.

MADE-UP EXAMPLE, to show the shape only. These are not this student's facts; never copy its numbers, titles, dates or class:
"Math: B, 2.1 pts from an A. Down mainly from a 62% on the Ch.4 Quiz (tests are 60% of the grade). An 88% on the Ch. 5 Test, due Oct 30, gets the A back. Scores usually post Fridays."

Reply with JSON only: {"sentences": [{"text": "...", "cites": ["fact id", ...]}]}"""

VOTE_SYSTEM = """You check sentences against FACTS (JSON, each with an id).
A sentence is backed only if every claim in it follows from the facts it cites.
Reply with JSON only: {"backed": [true or false, one per sentence, in order]}"""

HEADLINE_SYSTEM = """You write the one-line headline of a parent's grade briefing from the class STORIES (JSON).
Pick the single most useful insight. One sentence, under 120 characters.
Use only numbers, letters and classes in the stories, and cite the fact ids of the story you use.
Don't mention missing, late, past-due, turned-in or awaiting work. No dates, weekdays or relative days ("tomorrow", "next Friday").
Reply with JSON only: {"text": "...", "cites": ["fact id", ...]}"""


class Model(Protocol):
    def complete(self, system: str, user: str) -> str: ...


# ── Facts ────────────────────────────────────────────────────────────────────


def merged_facts(insight_facts, log_items=None):
    """insight_facts plus posting-day patterns from the scored log, by id."""
    facts = list((insight_facts or {}).get("facts") or [])
    as_of = (insight_facts or {}).get("as_of")
    if log_items and as_of:
        have = {f.get("id") for f in facts}
        facts += [p for p in insights.find_patterns(log_items, date.fromisoformat(as_of)) if p["id"] not in have]
    return facts


def fact_index(facts):
    """id -> fact. Each cause gets an entry too, carrying its move's class and kind."""
    index = {}
    for fact in facts:
        index[fact["id"]] = fact
        for cause in fact.get("causes") or []:
            index[cause["id"]] = {**cause, "kind": "grade_move", "class": fact.get("class"), "move": fact["id"]}
    return index


def class_names(facts):
    seen = []
    for fact in facts:
        if fact.get("class") and fact["class"] not in seen:
            seen.append(fact["class"])
    return seen


def facts_for(facts, name):
    return [f for f in facts if f.get("class") == name]


def cause_allowed(move):
    """True when the move's causes explain it: the engine's coverage (explained / delta) is within 20% of 1.

    A coverage in [0.8, 1.2] means explained has delta's sign and is within 20% of it. Missed causes
    leave coverage short, negative or None, so then the writer may state the move but not a cause.
    A move whose prior score map was trimmed (coverage_trimmed) never allows a cause.
    """
    if move.get("coverage_trimmed"):
        # The prior score map was trimmed to fit, so the causes can't be known.
        return False
    coverage = move.get("coverage")
    if isinstance(coverage, bool) or not isinstance(coverage, (int, float)):
        return False
    return 1 - CAUSE_TOLERANCE - 1e-9 <= coverage <= 1 + CAUSE_TOLERANCE + 1e-9


def ranked_causes(move):
    """A move's causes, largest share of the move first (points when there's no share)."""
    def size(cause):
        value = cause.get("share") if cause.get("share") is not None else cause.get("points")
        return -abs(value or 0.0)

    return sorted(move.get("causes") or [], key=lambda c: (size(c), c.get("id", "")))


def model_facts(class_facts):
    """What the model sees: a move whose causes don't explain it is sent without them."""
    out = []
    for fact in class_facts:
        if fact.get("kind") == "grade_move" and fact.get("coverage_trimmed"):
            fact = {**fact, "causes": [], "causes_withheld": "the earlier scores were trimmed, so the cause is unknown"}
        elif fact.get("kind") == "grade_move" and fact.get("causes"):
            if cause_allowed(fact):
                fact = {**fact, "causes": ranked_causes(fact)}
            else:
                fact = {**fact, "causes": [], "causes_withheld": "they don't explain the move"}
        out.append(fact)
    return out


def _parse_due(value):
    raw = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw[:10] if fmt == "%Y-%m-%d" else raw, fmt).date()
        except ValueError:
            continue
    return None


def due_label(iso):
    """Oct 30 from 2026-10-30."""
    day = date.fromisoformat(iso)
    return f"{SHORT_MONTHS[day.month - 1]} {day.day}"


def upcoming_items(classes, as_of):
    """Unscored work due on or after as_of, by class and grade bucket, earliest first.

    classes are the per-class inputs the scrape builds for the facts engine (name, counted_insight,
    assignments). An item lands in a bucket the same way the engine files a scored one.
    """
    today = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    items = []
    for cls in classes or []:
        counted = cls.get("counted_insight") or {}
        categories = [c for c in counted.get("categories") or [] if c.get("name")]
        summative = (counted.get("layout") or "percent_of_grade") == "summative_formative"
        for a in cls.get("assignments") or []:
            if a.get("points_earned") is not None or not a.get("points_possible"):
                continue
            due = _parse_due(a.get("due_date") or a.get("due"))
            title = (a.get("description") or a.get("title") or "").strip()
            cat = insights._category_bucket(categories, a.get("category"))
            if due is None or due < today or not title or cat is None:
                continue
            bucket = cat.get("kind") if summative else cat["name"]
            if not bucket:
                continue
            items.append({"class": cls.get("name"), "bucket": bucket, "title": title,
                          "due": due.isoformat(), "points": float(a["points_possible"])})
    items.sort(key=lambda i: (i["due"], i["title"]))
    return items


def attach_next_items(facts, upcoming):
    """Copy of facts where each what-if names the real next item it describes, if there is one.

    A what-if is about the next item of one size in one bucket, so it takes the earliest upcoming
    item with that class, bucket and points possible. Any other what-if stays "the next test".
    """
    out = []
    for fact in facts or []:
        if fact.get("kind") == "what_if":
            match = next((i for i in upcoming or [] if i["class"] == fact.get("class")
                          and i["bucket"] == fact.get("bucket") and i["points"] == fact.get("points")), None)
            if match:
                fact = {**fact, "next_title": match["title"], "next_due": match["due"],
                        "next_due_label": due_label(match["due"])}
        out.append(fact)
    return out


def worth_saying(class_facts):
    """No filler: a story needs an explained move, a reachable what-if or a pattern."""
    for fact in class_facts:
        kind = fact.get("kind")
        if kind == "grade_move" and fact.get("causes") and cause_allowed(fact):
            return True
        if kind == "what_if" and fact.get("reachable"):
            return True
        if kind == "posting_day":
            return True
    return False


# ── Code checks ──────────────────────────────────────────────────────────────


def _number_role(text, start, end):
    """What a number in a sentence is: a move, a weight, a distance to a letter, or a general value."""
    before, after = text[max(0, start - 30):start], text[end:end + 40]
    if _MOVE_BEFORE_RE.search(before):
        return "move"
    if _WEIGHT_AFTER_RE.match(after) or _WEIGHT_BEFORE_RE.search(before):
        return "weight"
    if _DISTANCE_AFTER_RE.match(after):
        return "distance"
    return "general"


def _add(values, value):
    values.add(float(value))
    values.add(abs(float(value)))


def _numbers_of(fact, role="general"):
    """The numbers a fact backs for one kind of claim. A move's size never backs a weight, and so on."""
    values = set()
    if role in ROLE_FIELDS:
        for key in ROLE_FIELDS[role]:
            value = fact.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                _add(values, value)
        return values
    for key, value in fact.items():
        if key in _NOT_GENERAL or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        _add(values, value)
    if fact.get("kind") == "posting_day" and isinstance(fact.get("share"), (int, float)):
        values.add(round(fact["share"] * 100, 1))
    for earned, possible in (("earned", "possible"), ("prior_earned", "prior_possible")):
        if fact.get(possible):
            values.add(round(fact[earned] / fact[possible] * 100, 1))
    for key in ("from_pct", "to_pct", "pct"):
        if isinstance(fact.get(key), (int, float)):
            values.add(round(fact[key], 1))
            # Aeries cuts off decimals rather than rounding: shown % = floor(rebuilt x 10) / 10.
            values.add(math.floor(fact[key] * 10) / 10)
    return values


def _letters_of(fact):
    letters = {fact.get("letter"), fact.get("next_letter")}
    for key in ("from_pct", "to_pct", "pct", "target", "threshold"):
        if isinstance(fact.get(key), (int, float)):
            letters.add(insights.letter_for(fact[key]))
    return {x for x in letters if x}


def _number_ok(text_number, allowed):
    n = float(text_number)
    return any(abs(n - v) < 0.051 or (n == int(n) and round(v) == n) for v in allowed)


def _words_value(phrase):
    """95 from "ninety-five", 2.1 from "two point one"."""
    whole, _, tail = phrase.lower().partition(" point ")
    if "hundred" in whole:
        total = 100
    else:
        total = sum(_WORD_VALUE[w] for w in re.split(r"[- ]", whole) if w)
    if tail:
        digits = "".join(str(_WORD_VALUE[w]) for w in tail.split() if _WORD_VALUE.get(w, 10) < 10)
        return f"{total}.{digits}" if digits else str(total)
    return str(total)


def spelled_to_digits(text):
    """Spelled-out numbers as digits, so the number check sees them. A bare "one" is left alone
    ("no one", "one of"); "one point" or "one percent" is a number."""
    def swap(match):
        unit = match.group("unit") or ""
        phrase = match.group(0)[: len(match.group(0)) - len(unit)]
        if phrase.lower() == "one" and not unit:
            return match.group(0)
        return _words_value(phrase) + unit

    return _SPELLED_RE.sub(swap, text)


def _strip_names(text, cited):
    """Remove class names, item titles and bucket names, whose digits aren't claims."""
    names = set()
    for fact in cited:
        for key in ("class", "title", "category", "bucket", "next_title"):
            if fact.get(key):
                names.add(str(fact[key]))
    for name in sorted(names, key=len, reverse=True):
        text = re.sub(re.escape(name), " ", text, flags=re.I)
    return text


def _score_pairs(cited):
    pairs = set()
    for fact in cited:
        for earned, possible in (("earned", "possible"), ("prior_earned", "prior_possible")):
            if isinstance(fact.get(possible), (int, float)) and isinstance(fact.get(earned), (int, float)):
                pairs.add((float(fact[earned]), float(fact[possible])))
    return pairs


def _dates_in(text, cited):
    """[(start, end, (month, day) or None)] for each date-like span. None marks a score like 31/50."""
    spans = []
    for match in _MONTH_DATE_RE.finditer(text):
        spans.append((match.start(), match.end(), (MONTHS.index(match.group(1).lower()[:3]) + 1, int(match.group(2)))))
    scores = _score_pairs(cited)
    for match in _SLASH_RE.finditer(text):
        n, m = float(match.group(1)), float(match.group(2))
        if (n, m) in scores:
            spans.append((match.start(), match.end(), None))
        elif n == int(n) and m == int(m) and 1 <= n <= 12 and 1 <= m <= 31:
            spans.append((match.start(), match.end(), (int(n), int(m))))
        else:
            spans.append((match.start(), match.end(), "score"))
    return sorted(spans)


def _due_days(cited):
    days = set()
    for fact in cited:
        if fact.get("next_due"):
            day = date.fromisoformat(fact["next_due"])
            days.add((day.month, day.day))
    return days


def _mask_allowed_dates(text, cited):
    """(text with each backed due date blanked, the dates no cited item backs, the scores N/M).

    A date is backed only when it is a cited what-if's next item due date. "due Oct 30" then is the
    item's name, not a to-do entry, so its "due" goes too.
    """
    allowed = _due_days(cited)
    unbacked, scores = [], []
    out, last = [], 0
    for start, end, value in _dates_in(text, cited):
        if start < last:
            continue
        if value is None or value == "score":
            scores.append((text[start:end], value))
            out.append(text[last:start] + " ")
        elif value in allowed:
            head = text[last:start]
            head = re.sub(r"\bdue,?\s*(?:on\s+)?$", "", head, flags=re.I)
            out.append(head + " ")
        else:
            unbacked.append(text[start:end])
            out.append(text[last:start] + " ")
        last = end
    out.append(text[last:])
    return "".join(out), unbacked, scores


def _cited_facts(cites, index):
    return [index[c] for c in cites or [] if c in index]


def check_sentence(text, cites, index, name=None):
    """None if the sentence is backed by the facts it cites, else the reason it's dropped."""
    text = (text or "").strip()
    if not text:
        return "empty"
    if not cites:
        return "cites nothing"
    cited = []
    for fid in cites:
        fact = index.get(fid)
        if fact is None:
            return f"unknown fact {fid}"
        if name is not None and fact.get("class") != name:
            return f"fact {fid} is another class"
        cited.append(fact)
    text = spelled_to_digits(text)
    masked, unbacked_dates, scores = _mask_allowed_dates(text, cited)
    if unbacked_dates:
        return "names a date"
    if _RESTATE_RE.search(masked):
        return "restates the to-do list"
    if _ZERO_GRADE_RE.search(masked) or re.search(r"(?<![\d.])0(?:\.0+)? percent\b", masked, re.I):
        return "0% as a grade"
    relative = _RELATIVE_RE.search(masked)
    if relative:
        return f"names a relative day ({relative.group(0)})"
    pattern_days = {f.get("weekday", "").lower() for f in cited if f.get("kind") == "posting_day"}
    for match in _WEEKDAY_RE.finditer(masked):
        day = next((d for d in WEEKDAY_NAMES if d.startswith(match.group(1).lower()[:3])), None)
        if day not in pattern_days:
            return f"names a weekday ({match.group(0)}) no pattern backs"
    move_ids = [f["move"] if f.get("move") else f["id"] for f in cited if f.get("kind") == "grade_move"]
    moves = [index.get(m) or {} for m in dict.fromkeys(move_ids)]
    if _CAUSE_CLAIM_RE.search(masked) and not moves:
        return "states a cause without citing a grade_move or cause"
    claims_cause = any(f.get("move") for f in cited) or (moves and _CAUSE_CLAIM_RE.search(masked))
    if claims_cause and not all(cause_allowed(m) for m in moves):
        return "states a cause the move's causes don't explain"
    for move in moves:
        said = [f["id"] for f in cited if f.get("move") == move.get("id")]
        if said and said != [c["id"] for c in ranked_causes(move)][: len(said)]:
            return "names causes out of share order"
    pairs = _score_pairs(cited)
    for raw, kind in scores:
        if kind == "score":
            n, m = (float(x) for x in re.split(r" ?/ ?", raw))
            if (n, m) not in pairs:
                return f"score {raw} isn't in the cited facts"
    plus_minus = _PLUS_MINUS_RE.search(masked)
    if plus_minus:
        return f"letter {plus_minus.group(0).strip()} isn't on the whole-letter scale"
    bare = _strip_names(masked, cited)
    for match in _NUMBER_RE.finditer(bare):
        role = _number_role(bare, match.start(), match.end())
        allowed = set().union(*(_numbers_of(f, role) for f in cited))
        if not _number_ok(match.group(0), allowed):
            suffix = f" as a {role}" if role in ("move", "weight") else ""
            return f"number {match.group(0)} isn't in the cited facts{suffix}"
    letters = set().union(*(_letters_of(f) for f in cited))
    for match in _LETTER_RE.finditer(bare):
        before = bare[: match.start()].rstrip()
        after = bare[match.end():]
        article = match.group(1) == "A" and (not before or before[-1] in ".!?") and re.match(r" [a-z]", after)
        if not article and match.group(1) not in letters:
            return f"letter {match.group(1)} isn't in the cited facts"
    return None


def eval_story(sentences, index):
    """None if the story passes the acceptance bar, else why it fails.

    Fails a story that cites no grade_move, what_if or pattern fact (a bare next_letter is
    not an insight), that restates past-due or missing work, or that's out of order.
    """
    if not sentences:
        return "no story"
    kinds = []
    for sentence in sentences:
        masked, _, _ = _mask_allowed_dates(sentence.get("text") or "", _cited_facts(sentence.get("cites"), index))
        if _RESTATE_RE.search(masked):
            return "restates past-due or missing work"
        ranks = [ORDER.get((index.get(fid) or {}).get("kind"), 9) for fid in sentence.get("cites") or []]
        kinds.extend((index.get(fid) or {}).get("kind") for fid in sentence.get("cites") or [])
        sentence["_rank"] = min(ranks) if ranks else 9
    if not any(kind in INSIGHT_KINDS for kind in kinds):
        return "cites no grade_move, what_if or pattern fact"
    order = [s.pop("_rank") for s in sentences]
    if order != sorted(order):
        return "out of order (letter, cause, what-if, pattern)"
    return None


# ── Model steps ──────────────────────────────────────────────────────────────


def _json(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _sentences(raw):
    parsed = _json(raw) or {}
    out = []
    for item in parsed.get("sentences") or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            cites = [c for c in item.get("cites") or [] if isinstance(c, str)]
            out.append({"text": item["text"].strip(), "cites": cites})
    return out


def vote(model, sentences, class_facts, votes=VOTES):
    """Keep the sentences a majority of votes call backed."""
    if not sentences:
        return []
    user = json.dumps({"facts": model_facts(class_facts), "sentences": [s["text"] for s in sentences]}, indent=1)
    tally = [0] * len(sentences)
    for _ in range(votes):
        backed = (_json(model.complete(VOTE_SYSTEM, user)) or {}).get("backed")
        if not isinstance(backed, list) or len(backed) != len(sentences):
            continue
        for i, ok in enumerate(backed):
            tally[i] += ok is True
    return [s for s, n in zip(sentences, tally) if n * 2 > votes]


def write_class(model, facts, name, votes=VOTES):
    """{"class", "story", "cites", "dropped"} or None when there's nothing worth saying."""
    class_facts = facts_for(facts, name)
    if not worth_saying(class_facts):
        return None
    index = fact_index(class_facts)
    user = json.dumps({"class": name, "scale": insights.SCALE_LABEL, "facts": model_facts(class_facts)}, indent=1)
    drafted = _sentences(model.complete(DRAFT_SYSTEM, user))
    kept, dropped = [], []
    for sentence in drafted:
        reason = check_sentence(sentence["text"], sentence["cites"], index, name)
        (dropped.append({**sentence, "reason": reason}) if reason else kept.append(sentence))
    voted = vote(model, kept, class_facts, votes)
    dropped += [{**s, "reason": "vote"} for s in kept if s not in voted]
    if eval_story([dict(s) for s in voted], index):
        return None
    return {
        "class": name,
        "story": " ".join(s["text"] for s in voted),
        "cites": [c for s in voted for c in s["cites"]],
        "dropped": [d["reason"] for d in dropped],
    }


def write_headline(model, facts, stories):
    """One cited line for the Briefing masthead, or None."""
    if not stories:
        return None
    index = fact_index(facts)
    user = json.dumps([{"class": s["class"], "story": s["story"], "cites": s["cites"]} for s in stories], indent=1)
    parsed = _json(model.complete(HEADLINE_SYSTEM, user)) or {}
    text = (parsed.get("text") or "").strip() if isinstance(parsed.get("text"), str) else ""
    cites = [c for c in parsed.get("cites") or [] if isinstance(c, str)]
    used = {c for s in stories for c in s["cites"]}
    if not text or not set(cites) <= used or check_sentence(text, cites, index):
        return None
    if eval_story([{"text": text, "cites": cites}], index):
        return None
    return {"text": text, "cites": cites}


def write_up(model, insight_facts, log_items=None, votes=VOTES, upcoming=None):
    """The whole write-up for one student: {"as_of", "headline", "stories"}.

    upcoming (from upcoming_items) lets a what-if name the real next item and its due date.
    """
    facts = attach_next_items(merged_facts(insight_facts, log_items), upcoming)
    stories = [s for s in (write_class(model, facts, name, votes) for name in class_names(facts)) if s]
    return {
        "as_of": (insight_facts or {}).get("as_of"),
        "headline": write_headline(model, facts, stories),
        "stories": stories,
    }


# ── Takeover and publishing ──────────────────────────────────────────────────


def apply_write_up(ai_summary, local):
    """The local write-up takes the headline and every class's story slot.

    A class with no local story gets an empty story rather than the model's. The headline
    is replaced only when the write-up has one.
    """
    summary = dict(ai_summary or {})
    stories = (local or {}).get("stories") or []
    if (local or {}).get("headline"):
        summary["headline"] = local["headline"]["text"]
    rows = summary.get("classes") or []
    picks = match_stories(stories, [cls.get("class_name") for cls in rows])
    summary["classes"] = [{**cls, "story": pick["story"] if pick else ""} for cls, pick in zip(rows, picks)]
    return summary


def normalize_name(text):
    """index.html normalizeName: lowercase, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def match_stories(stories, class_names):
    """The story for each class name, or None. Names compare normalized (index.html normalizeName).

    An exact match wins. Otherwise a class takes a story by containment (either name inside the
    other) only when exactly one story not already taken by an exact match qualifies, so "Math"
    and "AP Math Analysis" each keep their own story and an ambiguous name gets none.
    """
    keyed = [(normalize_name(s.get("class")), s) for s in stories or []]
    keyed = [(name, s) for name, s in keyed if name]
    wants = [normalize_name(n) for n in class_names]
    picks = [next((s for name, s in keyed if want and name == want), None) for want in wants]
    exact = {id(p) for p in picks if p is not None}
    for i, want in enumerate(wants):
        if picks[i] is not None or not want:
            continue
        hits = [s for name, s in keyed if id(s) not in exact and (want in name or name in want)]
        if len(hits) == 1:
            picks[i] = hits[0]
    return picks


def match_story(stories, class_name):
    """The story for one class name (see match_stories)."""
    return match_stories(stories, [class_name])[0]


def publish_enabled(env=None):
    return (env if env is not None else os.environ).get(PUBLISH_FLAG) == "1"


def publish_write_up(local, put, env=None):
    """Hand the write-up to `put` only when GRADE_INSIGHTS_PUBLISH=1. Off until PR5.

    PR5's `put` must authenticate with the AERIES_INSIGHTS_SECRET bearer, never the PIN session.
    """
    if not publish_enabled(env):
        return False
    put(local)
    return True


# ── Nightly steps for the mini runner ────────────────────────────────────────


class WriterWork:
    """NightWork for the runner: one step per student write-up, then a publish step.

    load() returns [{"insight_facts": ..., "log_items": [...], "upcoming": [...]}] in a stable order;
    "upcoming" (from upcoming_items) is optional. Results are
    saved under store/<night>/ by step, so a repeated step does nothing.
    """

    def __init__(self, load, model, store, put=None, env=None, votes=VOTES):
        self.load, self.model, self.store = load, model, Path(store)
        self.put, self.env, self.votes = put, env, votes

    def plan(self, night):
        return [f"student:{i}" for i in range(len(self.load()))] + ["publish"]

    def _path(self, night, step):
        return self.store / night / (step.replace(":", "-") + ".json")

    def run_step(self, night, step):
        path = self._path(night, step)
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if step == "publish":
            ups = sorted(path.parent.glob("student-*.json"), key=lambda p: int(p.stem.split("-")[1]))
            local = [json.loads(p.read_text()) for p in ups]
            published = publish_write_up(local, self.put, self.env) if self.put else False
            result = {"published": published}
        else:
            student = self.load()[int(step.split(":")[1])]
            result = write_up(self.model, student.get("insight_facts"), student.get("log_items"), self.votes,
                              student.get("upcoming"))
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=1))
        tmp.replace(path)
