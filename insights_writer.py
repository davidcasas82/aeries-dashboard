"""The local write-up: Briefing headline and class stories built from insight_facts.

The writer reads only a student's insight_facts (from the scrape) and the scored-items
log (for posting-day patterns). It never sees the analytics dump, so it can't restate
the past-due list, which stays a plain code list on the page.

Each class runs as a pipeline:
  1. No filler: a class with no grade move, no reachable what-if and no pattern gets no story
     and no model call.
  2. The model drafts sentences, and each sentence cites fact ids.
  3. check_sentence (code) drops any sentence whose numbers, letters, weekdays or wording
     aren't backed by the facts it cites.
  4. A vote pass asks the model, several times, whether each surviving sentence is backed;
     a sentence needs a majority.
  5. eval_story (code) rejects the story unless it cites a grade_move, what_if or pattern
     fact, keeps the insight order, and never restates past-due or missing work.
"""

import json
import math
import os
import re
from datetime import date
from pathlib import Path
from typing import Protocol

import insights

INSIGHT_KINDS = ("grade_move", "what_if", "posting_day")
# Story order: letter and distance, then the cause, then the what-if, then a pattern.
ORDER = {"next_letter": 0, "scale_break": 0, "grade_move": 1, "what_if": 2, "posting_day": 3}
VOTES = 3
# A move's causes may be stated only when they explain it: same sign, within 20% of delta.
CAUSE_TOLERANCE = 0.20
_CAUSE_CLAIM_RE = re.compile(
    r"\b(mainly|mostly|largely|because|due to|thanks to|driven by|pulled|dragged|cost|after an? \d|from an? \d)",
    re.I,
)
PUBLISH_FLAG = "GRADE_INSIGHTS_PUBLISH"

WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEKDAY_RE = re.compile(r"\b(mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?s?\b", re.I)
_DATE_RE = re.compile(
    r"\b\d{1,2}/\d{1,2}\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2}\b"
    r"|\b(today|tomorrow|tonight|yesterday)\b",
    re.I,
)
# Restating the to-do list is a FAIL: that list is code, not story.
_RESTATE_RE = re.compile(
    r"\b(missing|past[- ]due|overdue|late|not (?:been )?turned in|turned in|unsubmitted|"
    r"incomplete|awaiting|pending|not (?:yet )?graded|due(?! to\b))\b",
    re.I,
)
_ZERO_GRADE_RE = re.compile(r"(?<![\d.])0(?:\.0+)?\s?%")
_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
_LETTER_RE = re.compile(r"(?<![\w'’])([A-DF])(?![\w'’+-])")

DRAFT_SYSTEM = """You write one short class story for a parent's grade dashboard.
You get FACTS as JSON. Every fact has an id. Use only those facts.

Write 1 to 4 sentences, in this order, skipping any part with no fact:
1. The letter and the distance to the next letter (next_letter).
2. What moved the grade: the item, its score, its category's weight and the points it cost (grade_move and its causes).
3. The what-if: the score needed on the next item of that size to reach the next letter (what_if).
4. A pattern, if there is one (posting_day).

Rules:
- Every sentence cites the ids it uses. A cause cites its own cause id.
- If a grade_move has causes_withheld, state the move (from, to, delta) but give no cause for it.
- Use only numbers that appear in the cited facts. Round to one decimal at most.
- Don't mention missing, late, past-due, turned-in or awaiting work, or anything due. The dashboard lists that separately.
- No dates or weekdays, except the weekday in a cited posting_day fact. Call the next item "the next test" or "the next quiz", not by date.
- Never call a 0% a grade.
- If nothing here is worth saying, return {"sentences": []}.

MADE-UP EXAMPLE, to show the shape only. These are not this student's facts; never copy its numbers or class:
"Math: B, 2.1 pts from an A. Down mainly from a 62% on the Ch.4 quiz (tests are 60% of the grade). An 88% on next Friday's test gets the A back. Third straight week of slipping, all from quizzes."

Reply with JSON only: {"sentences": [{"text": "...", "cites": ["fact id", ...]}]}"""

VOTE_SYSTEM = """You check sentences against FACTS (JSON, each with an id).
A sentence is backed only if every claim in it follows from the facts it cites.
Reply with JSON only: {"backed": [true or false, one per sentence, in order]}"""

HEADLINE_SYSTEM = """You write the one-line headline of a parent's grade briefing from the class STORIES (JSON).
Pick the single most useful insight. One sentence, under 120 characters.
Use only numbers, letters and classes in the stories, and cite the fact ids of the story you use.
Don't mention missing, late, past-due, turned-in or awaiting work, or anything due. No dates or weekdays.
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
    """True when the move's causes explain it: explained has delta's sign and is within 20% of it.

    Missed causes leave explained short or even the wrong sign, so then the writer may state
    the move but not a cause.
    """
    explained, delta = move.get("explained"), move.get("delta")
    if explained is None or not delta or explained * delta <= 0:
        return False
    return abs(explained - delta) <= CAUSE_TOLERANCE * abs(delta)


def model_facts(class_facts):
    """What the model sees: a move whose causes don't explain it is sent without them."""
    out = []
    for fact in class_facts:
        if fact.get("kind") == "grade_move" and fact.get("causes") and not cause_allowed(fact):
            fact = {**fact, "causes": [], "causes_withheld": "they don't explain the move"}
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


def _numbers_of(fact):
    values = set()
    for key, value in fact.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        values.add(float(value))
        values.add(abs(float(value)))
        if key == "share":
            values.add(round(value * 100, 1))
    if fact.get("possible"):
        values.add(round(fact["earned"] / fact["possible"] * 100, 1))
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


def _strip_names(text, cited):
    """Remove class names, item titles and bucket names, whose digits aren't claims."""
    names = set()
    for fact in cited:
        for key in ("class", "title", "category", "bucket"):
            if fact.get(key):
                names.add(str(fact[key]))
    for name in sorted(names, key=len, reverse=True):
        text = re.sub(re.escape(name), " ", text, flags=re.I)
    return text


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
    if _RESTATE_RE.search(text):
        return "restates the to-do list"
    if _ZERO_GRADE_RE.search(text):
        return "0% as a grade"
    if _DATE_RE.search(text):
        return "names a date"
    pattern_days = {f.get("weekday", "").lower() for f in cited if f.get("kind") == "posting_day"}
    for match in _WEEKDAY_RE.finditer(text):
        day = next((d for d in WEEKDAY_NAMES if d.startswith(match.group(1).lower()[:3])), None)
        if day not in pattern_days:
            return f"names a weekday ({match.group(0)}) no pattern backs"
    moves = [index.get(f["move"]) or {} if f.get("move") else f for f in cited if f.get("kind") == "grade_move"]
    claims_cause = any(f.get("move") for f in cited) or (moves and _CAUSE_CLAIM_RE.search(text))
    if claims_cause and not all(cause_allowed(m) for m in moves):
        return "states a cause the move's causes don't explain"
    bare = _strip_names(text, cited)
    allowed = set().union(*(_numbers_of(f) for f in cited))
    for number in _NUMBER_RE.findall(bare):
        if not _number_ok(number, allowed):
            return f"number {number} isn't in the cited facts"
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
        if _RESTATE_RE.search(sentence.get("text") or ""):
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


def write_up(model, insight_facts, log_items=None, votes=VOTES):
    """The whole write-up for one student: {"as_of", "headline", "stories"}."""
    facts = merged_facts(insight_facts, log_items)
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
    stories = {s["class"]: s["story"] for s in (local or {}).get("stories") or []}
    if (local or {}).get("headline"):
        summary["headline"] = local["headline"]["text"]
    classes = []
    for cls in summary.get("classes") or []:
        classes.append({**cls, "story": stories.get(cls.get("class_name"), "")})
    summary["classes"] = classes
    return summary


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

    load() returns [{"insight_facts": ..., "log_items": [...]}] in a stable order. Results are
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
            result = write_up(self.model, student.get("insight_facts"), student.get("log_items"), self.votes)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=1))
        tmp.replace(path)
