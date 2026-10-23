"""The local write-up: class stories built from insight_facts.

The writer reads only a student's insight_facts (from the scrape) and the scored-items
log (for posting-day patterns). It never sees the analytics dump, so it can't restate
the past-due list, which stays a plain code list on the page.

Every class with a letter to open on gets a story, and code writes every sentence of it:
  1. The opening (say_opening): the letter with the move or the gap.
  2. code_lines adds the biggest drag on a class with no recent move, the next named item's move-up
     step and keep sentence, and, for buckets with nothing posted, the same at the size of past items,
     saying so.
  3. check_sentence holds each sentence to the facts it cites and to its exact form (form_reason); a
     sentence that fails isn't written.
  4. eval_story checks the story opens on the letter, keeps the story order and never restates
     past-due or missing work.
Then story_tags (code, no model) marks the student's easiest win and biggest risk, from what
the published stories say.
"""

import json
import math
import os
import re
from datetime import date, datetime
from pathlib import Path

import insights

# Story order: the letter, then why it moved (or the biggest drag), then the pattern, then the next steps.
ORDER = {"next_letter": 0, "current_letter": 0, "scale_break": 0, "grade_move": 1, "drag": 1, "posting_day": 2,
         "what_if": 3, "keep_letter": 3}
# A move's causes may be stated only when they explain it: same sign, within 20% of delta.
# The engine's coverage field (explained / delta) carries both: 0.8 <= coverage <= 1.2.
CAUSE_TOLERANCE = 0.20
_CAUSE_CLAIM_RE = re.compile(
    r"\b((?:mainly|mostly|largely|primarily|chiefly)\s+(?:\w+\s+)?(?:from|because|due|thanks|owing|driven|"
    r"caused|after|by)|because|due to|owing to|thanks to|driven by|caused by|"
    r"result of|as a result|on account of|pulled|dragged|hurt|lifted|boosted|help(?:s|ed|ing)?|cost|came from|"
    r"comes from|damage|"
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
# "C--one more test" is a dash, not a C-.
# "C–one more test" with an en dash is a dash too.
_PLUS_MINUS_RE = re.compile(r"(?<![\w'’])([A-DF])(?:\+|[−–](?![-−–\w])|-(?![A-Za-z0-9-])| ?-?(?:plus|minus)\b)", re.I)

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
# Every plain "up" and "down" is a direction word, idioms too: "up to date", "down to the wire" and
# "scores show up Thursdays" need a move of that sign like "up to a C" does. Fail closed: an exemption for
# idioms can't tell "down to the wire" from "down to the D range", "up to a solid C" or "down to failing".
_UP_WORD = r"(?:upwards?|ups?)"
_DOWN_WORD = r"(?:downwards?|downs?)"
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
# A needed score: "an 85 or better", "84.2% on the next test", "needs an 85".
_NEEDED_AFTER_RE = re.compile(
    r"^\s*(?:%|percent)?\s*(?:or better|or higher|or above|or more|\+|on\s+(?:(?:the|that|this)\s+)?(?:next|upcoming)\b)", re.I
)
_NEEDED_BEFORE_RE = re.compile(
    r"\b(?:needs?|needed|needing|requires?|at least|score of|gets? an?|getting an?|earns? an?|earning an?)\s+$", re.I
)
# A score on an item: "a 62% on the Ch.4 Quiz". It must be a percent (or out of 100), never a bare
# points-earned like "a 4.0 on the Unit 2 Packet", which reads as 4%.
_SCORE_BEFORE_RE = re.compile(r"\b(?:an?|scored|scoring|got|getting|earned|earning)\s+$", re.I)
_SCORE_AFTER_RE = re.compile(r"^\s*(?:%|percent)?\s+(?:on|in|for)\b", re.I)
# Spoken fractions for small gaps: "half a point", "a quarter point".
_FRACTION_WORDS = (
    (re.compile(r"\ba point and a half\b", re.I), "1.5 points"),
    (re.compile(r"\bhalf (?:a|of a|a single) (point|percent|percentage point)\b", re.I), r"0.5 \1"),
    (re.compile(r"\b(?:a|one) half(?:[- ]a)? (point|percent|percentage point)s?\b", re.I), r"0.5 \1"),
    (re.compile(r"\b(?:a )?quarter(?: of a| a)? (point|percent)\b", re.I), r"0.25 \1"),
    (re.compile(r"\b(?:a )?tenth of a (point|percent)\b", re.I), r"0.1 \1"),
    (re.compile(r"\b(?:a|one) (point|percent|percentage point)(?! (?:five|\d))\b", re.I), r"1 \1"),
)
_QUARTERS_RE = re.compile(r"\b(one|two|three|1|2|3) quarters? of (?:a|one) (point|percent)\b", re.I)
# "one and three quarters" -> 1.75, "two and a quarter" -> 2.25.
_AND_QUARTERS_RE = re.compile(rf"\b(\d+(?:\.\d+)?|{_W}(?: {_W})?) and (a|one|three) quarters?\b", re.I)
_AND_A_HALF_RE = re.compile(rf"\b(\d+(?:\.\d+)?|{_W}(?: {_W})?) and a half\b", re.I)
# Hyphens inside a spoken number ("one-and-a-half", "a half-point", "twenty-one") read as spaces.
_NUMBER_PARTS = set(_WORD_VALUE) | {"a", "and", "half", "halves", "quarter", "quarters", "third", "thirds",
                                    "tenth", "tenths", "point", "points", "percent"}
_HYPHEN_CHAIN_RE = re.compile(r"\b[a-z]+(?:-[a-z]+)+\b", re.I)


def _unhyphen_numbers(text):
    def swap(match):
        parts = match.group(0).split("-")
        if any(p.lower() in _WORD_VALUE or p.lower() in ("half", "quarter", "quarters") for p in parts) and \
                all(p.lower() in _NUMBER_PARTS for p in parts):
            return " ".join(parts)
        return match.group(0)
    return _HYPHEN_CHAIN_RE.sub(swap, text)
# Claims about the next letter up: they need a next_letter or what_if fact, and never for an A.
_NEXT_CLAIM_RE = re.compile(
    r"\b(?:next letter|(?:to )?reach(?:es|ing)? an? [A-D]\b|"
    r"(?:gets?|getting|brings?|pulls?|push(?:es)?|lifts?|raises?|moves?)\s+(?:\w+\s+){0,2}?(?:up\s+)?to an? [A-D]\b|"
    r"(?:points?|pts\.?|percent|%)\s+(?:away\s+)?(?:from|short of|shy of|below)\s+an? [A-D]\b)"
)
# "Keeps the A", "stays a B", "can't drop it below an A": only with a cited keep_letter fact.
_KEEP_CLAIM_RE = re.compile(
    r"\b(?:keeps?|keeping|holds?|holding|hangs? on to|stays?|staying|remains?)\s+(?:it\s+|the grade\s+)?"
    r"(?:at\s+|as\s+)?(?:an?\s+|the\s+)?[A-F]\b(?![\w'’+-])"
    r"|\bcan(?:'|’|no)?t\s+(?:drop|pull|cost|take|lose|bring)\b|\bcannot\s+(?:drop|pull|cost|take|lose|bring)\b")
_CANT_LOSE_RE = re.compile(r"\bcan(?:'|’|no)?t\s+(?:drop|pull|cost|take|lose|bring)\b|\bcannot\s+(?:drop|pull|cost|take|lose|bring)\b", re.I)
# The letter a keep claim is about: "keeps the A", "stays a B", "below an A".
_KEEP_LETTER_RE = re.compile(
    r"\b(?:keeps?|keeping|holds?|holding|hangs? on to|stays?|staying|remains?)\s+(?:it\s+|the grade\s+)?"
    r"(?:at\s+|as\s+)?(?:an?\s+|the\s+)?([A-F])\b(?![\w'’+-])|\bbelow\s+(?:an?\s+|the\s+)?([A-F])\b(?![\w'’+-])")
# "Back to a B", "gets the A back", "a B again": only when a move shows the class held that letter.
_BACK_RE = re.compile(
    r"\b(?:back(?: up)? to|gets? (?:it |the \w+ |that \w+ )?back|regain\w*|again|return\w* to|wins? back)\b", re.I
)
_GRADE_WORDS_RE = re.compile(
    rf"\b(?:grade|{_DOWN_WORD}|{_UP_WORD}|drop|dropped|drops|fell|falls|slipped|slips|rose|climbed|lost|gained|"
    r"cost|costs|moved|"
    r"lower|higher|pulled|dragged|hurt)\b",
    re.I,
)
_BARE_CAUSE_RE = re.compile(r"\b(?:mainly|mostly|largely|primarily|chiefly)\b", re.I)
# Wording that ranks a cause first. Only then must the named cause be the top share; a plain list of causes
# ("Down from HW 3 and the Ch.4 Quiz.") may come in any order.
_MOST_RE = re.compile(r"\b(?:most|mostly|mainly|main|largely|primarily|chiefly|biggest|largest|bulk|chief)\b", re.I)
# Wording a note never uses (from the old model prompts).
_ECHO_RE = re.compile(
    r"distance to the next letter|\bnext letter\b|\bstudents?\b|\bthe letter is\b|\bwhat[- ]if\b|"
    r"\bwhat moved\b|\bof that size\b|\bnext item of\b|\bitem of similar size\b|\bthe next item\b",
    re.I,
)
# "Moved to a C" claims the class changed letter; it needs a move that crossed into that letter.
_LETTER_MOVE_RE = re.compile(
    r"(?:\b(?P<verb>(?i:moved|went|slipped|slid|dropped|fell|rose|climbed|jumped|dipped|changed|fallen|risen"
    r"|is down|is up)) (?P<way>(?i:up|down|back) )?|(?:^|(?<=[.!?] ))(?P<lead>(?i:up) ))"
    r"to (?:an? )?(?P<letter>[A-F])\b(?!\w)")
_DOWN_VERBS = {"slipped", "slid", "dropped", "fell", "dipped", "fallen", "is down"}
_UP_VERBS = {"rose", "climbed", "jumped", "risen", "is up"}


def _letter_move_direction(match):
    """-1 for a letter move worded down ("slipped to a B"), 1 up ("Up to an A", "rose to"), 0 for neither
    ("moved to", "changed to"), None when the words say both ways."""
    verb = (match.group("verb") or "").lower()
    way = (match.group("way") or "").strip().lower()
    down = verb in _DOWN_VERBS or way == "down"
    up = verb in _UP_VERBS or way == "up" or bool(match.group("lead"))
    if down and up:
        return None
    return -1 if down else 1 if up else 0
# Words that set a cause against the move: "the Lab (9/10) helped", "while the Lab helped".
_CONTRAST_RE = re.compile(
    r"\b(?:helped|helps|helping|offset|offsets|offsetting|cushioned|softened|propped|while|but|though|although|"
    r"despite|even with|kept it from)\b", re.I)
# "The move", "the drop", "explained the change": talk of a move needs a cited grade_move.
_MOVE_REF_RE = re.compile(
    r"\b(?:the|this|that|its) (?:move|drop|dip|slide|slip|rise|climb|change|decline|gain|jump)\b|"
    r"\bexplain(?:s|ed|ing)? (?:the|it|this|that)\b", re.I)
# Sentences about what isn't there say nothing: "No major assignments identified", "No pattern data".
_NOTHING_RE = re.compile(
    r"\bno (?:major |main |clear |single |specific |big |one |particular )?"
    r"(?:assignments?|causes?|reasons?|patterns?|data|items?|scores?|changes?)\b|"
    r"\bnothing (?:explains|stands out|explained)\b|\b(?:not|none) (?:identified|cited|available|explained)\b", re.I)
# Advice the facts can't back. A next step comes only from a cited what_if ("an 85 or better on ...").
_ADVICE_RE = re.compile(
    r"\b(?:re-?do(?:ing)?|re-?take|re-?submit|make up|makeup|focus(?:es|ing)? on|work(?:ing)? on|study|studying|"
    r"review(?:ing)?|practi[cs]e|ask (?:the|a|for|about)|talk(?:ing)? to|reach out|should|ought|needs? to|try(?:ing)? to|"
    r"aim(?:ing)? (?:for|to|at)|keep up|stay on top|turn(?:ing)? (?:it |them )?in|improve[ds]?|improving|get help|"
    r"catch(?:ing)? up|bring (?:it|the grade) up)\b", re.I)
# "a"/"an" before a number or a letter, fixed by how it's spoken: "an 85", "an 11", "an A", "a 97", "a B".
# A capital "A"/"An" counts as the article only at the start of a sentence, so the letter A stays put.
_ARTICLE_RE = re.compile(
    r"(?:(?<=^)|(?<=[.!?:;\"“(] )|(?<=[.!?:;]  ))(A|An)( +)(\d+(?:\.\d+)?|[A-F](?![\w'’+-]))"
    r"|\b(a|an)( +)(\d+(?:\.\d+)?|[A-F](?![\w'’+-]))")
# Words that say which way a cause pushed the grade (form_reason drops any sentence citing a cause).
# A cause that "helped" has a positive share; one that "hurt" a negative one.
_UP_VERB_RE = re.compile(
    r"\b(?:help(?:s|ed|ing)?|lift(?:s|ed|ing)?|boost(?:s|ed|ing)?|rais(?:e|es|ed|ing)|sav(?:e|es|ed|ing)|"
    r"carr(?:y|ies|ied|ying)|rescu(?:e|es|ed|ing)|improv(?:e|es|ed|ing)|" + _UP_WORD + r")\b", re.I)
_DOWN_VERB_RE = re.compile(
    r"\b(?:hurt(?:s|ing)?|drag(?:s|ged|ging)?|cost(?:s|ing)?|pull(?:s|ed|ing)?|knock(?:s|ed|ing)?|s[aiu]nk(?:s|ing)?|"
    r"damag(?:e|es|ed|ing)|tank(?:s|ed|ing)?|cut(?:s|ting)?|dent(?:s|ed|ing)?|slash(?:es|ed|ing)?|"
    r"drop(?:s|ped|ping)?|blam(?:e|es|ed|ing)|fell|fall(?:s|ing|en)?|" + _DOWN_WORD + r")\b", re.I)
# "Pulled it up" and "knocked it up" push up; "pulled it down" pushes down.
_UP_PARTICLE_AFTER_RE = re.compile(r"^(?:\s+\w+){0,2}\s+up(?:ward)?s?\b", re.I)
# Words that say which way the grade itself moved. Each needs a cited grade_move (a cited cause cites its
# move) whose delta has that sign: "Scores dropped." citing only a next_letter or pattern drops.
_MOVED_UP_RE = re.compile(rf"\b(?:rose|risen|climbed|jumped|gained|went up|is up|{_UP_WORD})\b", re.I)
_MOVED_DOWN_RE = re.compile(rf"\b(?:fell|fallen|dropped|dipped|slipped|slid|declined|sank|sunk|lost|went down|"
                            rf"is down|{_DOWN_WORD})\b", re.I)
_NEGATED_RE = re.compile(r"\b(?:not|never|n't|didn't|doesn't|did not|does not|wasn't|won't|hardly|barely|scarcely)"
                         r"\s+(?:\w+\s+)?$", re.I)
# Adjectives on a named cause: "a low {item:...}" says it hurt, "a strong {item:...}" that it helped.
_LOW_ADJ_RE = re.compile(r"\b(?:low|lower|lowest|weak|weaker|poor|bad|rough|missed)\b", re.I)
_HIGH_ADJ_RE = re.compile(r"\b(?:strong|stronger|high|higher|highest|good|great|perfect|solid|top)\b", re.I)
# Phrases that name a direction the single words miss.
_UP_PHRASE_RE = re.compile(r"\bgave it a boost\b", re.I)
_DOWN_PHRASE_RE = re.compile(r"\bbehind the (?:drop|dip|slide|slip|fall|decline)\b", re.I)
_SAME_LETTER_MOVE_RE = re.compile(
    r"\b(?i:moved|went|changed|dropped|fell|rose|slipped|slid|climbed) (?i:from) (?i:an? )?([A-F]) (?i:to) (?i:an? )?\1\b")
ROLE_FIELDS = {
    "move": ("delta", "explained", "points", "share"),
    "weight": ("bucket_weight_pct",),
    "distance": ("gap",),
}
# Ratios and weights are never a general number: a weight needs weight words around it.
# A needed score is checked on its own rule (exact, or rounded up), never by plain rounding.
# other_share/other_causes (#54) describe causes folded away; no sentence can name or count them.
_NOT_GENERAL = ("bucket_weight_pct", "coverage", "needed_pct", "other_share", "other_causes")



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
            index[cause["id"]] = {**cause, "kind": "grade_move", "class": fact.get("class"), "move": fact["id"],
                                  "since": fact.get("since")}
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


def say_needed(needed_pct):
    """How a note says a needed score: the next whole number up, "or better". 84.2 -> "85 or better"."""
    return f"{math.ceil(round(needed_pct, 6))} or better"


def _article(word):
    return fix_articles(f"a {word}").split(" ")[0]


_LETTERS = ("A", "B", "C", "D", "F")


def shown_letters(fact):
    """(prior_shown, now_shown) for a grade_move or letter fact: the letter the dashboard showed at the last
    snapshot and the one it shows now (the Aeries mark's letter, else the percent's), as the engine computed
    them once (class_facts). prior_shown is None when there's no earlier letter. Everything about the opener
    (still or now, the letter named, a crossing) and the checks on it read only these two.

    A fact built without them (no mark known) falls back to the percent's letters: a grade_move's from_pct
    and to_pct, a letter fact's own letter, unchanged.
    """
    now = fact.get("now_shown")
    if now not in _LETTERS:
        now = fact.get("letter") if fact.get("kind") in ("next_letter", "current_letter") else (
            insights.letter_for(fact["to_pct"]) if _number(fact.get("to_pct")) else None)
    if "prior_shown" in fact:
        prior = fact["prior_shown"] if fact["prior_shown"] in _LETTERS else None
    elif fact.get("kind") == "grade_move":
        prior = insights.letter_for(fact["from_pct"]) if _number(fact.get("from_pct")) else None
    else:
        prior = now
    return prior, now


_RANK = {"A": 4, "B": 3, "C": 2, "D": 1, "F": 0}


def shown_direction(fact):
    """1 when the shown letter rose from prior_shown to now_shown, -1 when it fell, 0 when it's the same,
    None with no earlier letter. The one place a letter direction comes from (shown_letters)."""
    prior, now = shown_letters(fact)
    if prior is None or now is None:
        return None
    return (_RANK[now] > _RANK[prior]) - (_RANK[now] < _RANK[prior])


def say_opening(fact):
    """The story's first sentence, built from one next_letter, current_letter or grade_move fact, or None.

    current_letter (a class at an A): "Still an A."  next_letter: "Still a C, 0.5 points from a B."
    grade_move: "Still an A, down 1.6 since Oct 14.", or, when the shown letter changed, "Slipped to a C since
    Oct 14, down 1.6." / "Up to a B since Oct 14, up 2.1.", the verb only when the shown letter's direction
    (shown_direction) and the percent's agree; when they don't (a mark that went against the percent),
    "Now an A, down 1.4 since Oct 14." The letters are the fact's prior_shown and now_shown, nothing else:
    "Still" exactly when they are equal, otherwise "Now a C, 0.1 points from a B." / "Now an A." or the
    crossing into now_shown. The letter named is now_shown. When it isn't the percent's letter (the Aeries
    mark differs), a letter fact states no gap, since the percent's gap isn't about the letter on the page
    ("Still an A." at 89.5 marked A).
    """
    kind = fact.get("kind")
    prior, now = shown_letters(fact)
    if now is None:
        return None
    still = prior is not None and prior == now
    lead = "Still" if still else "Now"
    if kind == "current_letter" and fact.get("letter"):
        return f"{lead} {_article(now)} {now}."
    if kind == "next_letter" and fact.get("letter") and fact.get("next_letter") and _number(fact.get("gap")):
        if now != fact["letter"]:
            return f"{lead} {_article(now)} {now}."
        nxt = fact["next_letter"]
        return f"{lead} {_article(now)} {now}, {fact['gap']:g} points from {_article(nxt)} {nxt}."
    if kind == "grade_move" and all(_number(fact.get(k)) for k in ("from_pct", "to_pct", "delta")):
        way = "down" if fact["delta"] < 0 else "up"
        since = f" since {fact['since_label']}" if fact.get("since_label") else ""
        if still:
            return f"Still {_article(now)} {now}, {way} {abs(fact['delta']):g}{since}."
        sign = (fact["delta"] > 0) - (fact["delta"] < 0)
        if sign and shown_direction(fact) == sign:
            verb = "Slipped" if sign < 0 else "Up"
            return f"{verb} to {_article(now)} {now}{since}, {way} {abs(fact['delta']):g}."
        return f"Now {_article(now)} {now}, {way} {abs(fact['delta']):g}{since}."
    return None


def _say_fields(fact):
    """The fact with the wording code says it in: a what_if's say_needed and say_step, a grade_move's
    since_label (which say_opening uses)."""
    if fact.get("kind") == "what_if" and isinstance(fact.get("needed_pct"), (int, float)) and fact.get("reachable"):
        fact = {**fact, "say_needed": say_needed(fact["needed_pct"])}
        if isinstance(fact.get("target"), (int, float)):
            to = insights.letter_for(fact["target"])
            fact["say_step"] = fix_articles(f"A {fact['say_needed']} on the {{next:{fact['id']}}} gets it to a {to}.")
    if fact.get("kind") == "grade_move" and fact.get("since"):
        try:
            fact = {**fact, "since_label": due_label(fact["since"])}
        except ValueError:
            pass
    return fact


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
    item with that class, bucket and points possible. Any other what-if stays "the next test". One whose
    class and bucket have a posted item of another size gets bucket_posted: the posted item wins, so no
    assumed-size line is said about that bucket (assumed_picks).
    """
    out = []
    for fact in facts or []:
        if fact.get("kind") in ("what_if", "keep_letter"):
            match = next((i for i in upcoming or [] if i["class"] == fact.get("class")
                          and i["bucket"] == fact.get("bucket") and i["points"] == fact.get("points")), None)
            if match:
                fact = {**fact, "next_title": match["title"], "next_due": match["due"],
                        "next_due_label": due_label(match["due"])}
            elif any(i["class"] == fact.get("class") and i["bucket"] == fact.get("bucket") for i in upcoming or []):
                fact = {**fact, "bucket_posted": True}
        out.append(fact)
    return out


def is_keep(fact):
    """The engine's keep-the-letter fact (kind keep_letter), never a next-letter what-if."""
    return fact.get("kind") == "keep_letter"


def pick_keep(class_facts):
    """The class's keep_letter fact when code can say it, in any direction (rise, flat or drop), else None.

    It is said only about a named next item at its posted points (_named): a needed score of at most 100,
    or #55's can't-drop form (_named_cannot_lose). At a typical size, with no item named, it isn't said
    here (assumed_picks says it). With several named keep facts (keep_fact's and a typical keep an
    upcoming item matches), the heaviest bucket wins, then the soonest due date, then the id, so the same
    facts in any order pick the same one.
    """
    def sayable(f):
        if not is_keep(f) or f.get("move") or not _named(f):
            return False
        if _number(f.get("needed_pct")):
            return f["needed_pct"] <= 100
        return _named_cannot_lose(f)
    weight = {f.get("bucket"): -(_weight(f, class_facts) or 0.0) for f in class_facts}
    return min((f for f in class_facts if sayable(f)),
               key=lambda f: (weight.get(f.get("bucket"), 0.0), str(f.get("next_due") or ""), f["id"]), default=None)


def move_up_step(class_facts):
    """The class's what_if for its next named item when it reaches the next letter, else None.

    Of the what_if facts attach_next_items named at their posted points, the soonest (due date, then title,
    then id), in any fact order. Reachable means a needed score of at most 100; the step's target must be
    the class's next letter. A later item never stands in for a soonest one that can't get there.
    """
    named = [f for f in class_facts if f.get("kind") == "what_if" and _named(f)]
    step = min(named, key=lambda f: (str(f.get("next_due") or ""), str(f["next_title"]), f["id"]), default=None)
    letter = next((f for f in class_facts if f.get("kind") == "next_letter"), None)
    if step is None or letter is None or not step.get("reachable") or not _number(step.get("needed_pct")) \
            or step["needed_pct"] > 100 or not _number(step.get("target")) \
            or insights.letter_for(step["target"]) != letter.get("next_letter"):
        return None
    return step


def _named_cannot_lose(fact):
    """A can't-drop keep fact is only true about a named upcoming item at its posted points: the engine works
    it out at fact["points"], and attach_next_items names it only when an upcoming item in that class and
    bucket has exactly those points. Without that name (a typical size) it can be false, so it isn't one."""
    return bool(fact.get("cannot_lose")) and not isinstance(fact.get("needed_pct"), (int, float)) \
        and bool(fact.get("next_title")) and isinstance(fact.get("points"), (int, float)) and fact["points"] > 0


def say_keep(fact):
    """How a note says a keep_letter fact, in slot form: "A 62 or better on the {next:<id>} keeps the A." or,
    when the named next item can't cost the letter, "The {next:<id>} can't drop it below an A." (only with a
    named upcoming item at its posted points, see _named_cannot_lose). Code fills the slot with the next
    item's title or "next <bucket>" after the checks. Only code says it (code_lines), and only for
    pick_keep's fact: a named item at its posted points."""
    letter = fact.get("letter")
    if not letter or not fact.get("id"):
        return None
    where = f"the {{next:{fact['id']}}}"
    if fact.get("cannot_lose"):
        if not _named_cannot_lose(fact):
            return None
        return f"The {{next:{fact['id']}}} can't drop it below {_article(letter)} {letter}."
    if isinstance(fact.get("needed_pct"), (int, float)):
        return fix_articles(f"A {say_needed(fact['needed_pct'])} on {where} keeps the {letter}.")
    return None


# ── Assumed next item and biggest drag (code, no model) ─────────────────────
# With nothing posted in a bucket, code still says what the next item there would take, at the size of the
# bucket's past items (the engine's median points possible): "If the next test is worth about 50 points like
# the last ones, a 38 or better keeps the B and a 46 gets it to an A." Points, not percents, because the
# sentence names the item's size. Never a date, never "scheduled", never a can't-drop claim.
ASSUMED_MAX_BUCKETS = 2
# An assumed keep needing less than this ("a 1 or better keeps the B" on a 10-point homework) says nothing
# useful, so it isn't said and its bucket doesn't take one of the two places.
ASSUMED_KEEP_MIN_PCT = 50.0
_SCHEDULED_RE = re.compile(r"\b(?:schedul\w*|planned|coming up|upcoming|on the calendar|due(?! to\b))\b", re.I)
_STILL_RE = re.compile(r"\b(?:still|remains?|stays?)\b", re.I)
_ASSUMED_RE = re.compile(r"^If the \{next:([^{}\s]+)\} is worth about \d+(?:\.\d+)? points like the last ones, ")
_DRAG_FORM = "The {{item:{id}}} is what's pulling it down most."
_DRAG_RE = re.compile(r"^The \{item:([^{}\s]+)\} is what's pulling it down most\.$")


def needed_points(fact):
    """The whole points needed on an item of fact["points"] for its needed_pct, rounded up, or None.

    A needed 76.0% on a 50-point item is 38; 76.1% is 39. None past the item's own points."""
    if not _number(fact.get("needed_pct")) or not _number(fact.get("points")) or fact["points"] <= 0:
        return None
    n = math.ceil(round(fact["needed_pct"] * fact["points"] / 100.0, 6))
    return n if n <= fact["points"] else None


def _assumable(fact):
    """An unnamed fact about the next item at its typical size, in a bucket with nothing posted and past
    items of about one size (no sizes_vary)."""
    return not _named(fact) and not fact.get("bucket_posted") and not fact.get("sizes_vary") \
        and needed_points(fact) is not None


def _class_next_letter(class_facts):
    return next((f.get("next_letter") for f in class_facts if f.get("kind") == "next_letter"), None)


def _weight(fact, class_facts):
    """A bucket's live weight from any fact of the class that carries it (what_if facts do), else None."""
    if _number(fact.get("bucket_weight_pct")):
        return fact["bucket_weight_pct"]
    same = [f["bucket_weight_pct"] for f in class_facts if f.get("bucket") == fact.get("bucket")
            and _number(f.get("bucket_weight_pct"))]
    return max(same, default=None)


def _named_buckets(class_facts):
    """Buckets the named code lines speak for (move_up_step, pick_keep)."""
    return {f.get("bucket") for f in (move_up_step(class_facts), pick_keep(class_facts)) if f}


def assumed_picks(class_facts):
    """[(keep or None, step or None)] for the class's assumed next items, heaviest bucket first (then bucket
    name, then id).

    Per bucket, at its typical size: the keep_letter fact with a needed score and the what_if that reaches
    the class's next letter, each only when _assumable; a keep only when it needs ASSUMED_KEEP_MIN_PCT or
    more. A bucket the named lines already cover is skipped,
    and the class says at most ASSUMED_MAX_BUCKETS buckets in all, named ones counted first."""
    nxt = _class_next_letter(class_facts)
    named = _named_buckets(class_facts)
    pairs = {}
    for f in class_facts:
        if f.get("bucket") in named or f.get("move") or not _assumable(f):
            continue
        key = (f.get("bucket"), f.get("points"))
        if is_keep(f) and not f.get("cannot_lose") and f.get("letter") and f["needed_pct"] >= ASSUMED_KEEP_MIN_PCT:
            slot = 0
        elif f.get("kind") == "what_if" and f.get("reachable") and _number(f.get("target")) and nxt \
                and insights.letter_for(f["target"]) == nxt:
            slot = 1
        else:
            continue
        pair = pairs.setdefault(key, [None, None])
        if pair[slot] is None or str(f.get("id")) < str(pair[slot].get("id")):
            pair[slot] = f
    # One size per bucket: the engine has one typical size, so two would mean a mixed-up fact list.
    by_bucket = {}
    for (bucket, _points), pair in pairs.items():
        by_bucket.setdefault(bucket, []).append(pair)
    # Heaviest first (the engine's live bucket weight, which keep facts carry too), then the bucket name,
    # then the id: a total order, so the same facts in any order give the same lines.

    def order(pr):
        lead = pr[0] or pr[1]
        weight = _weight(pr[1] or lead, class_facts)
        return (weight is None, -(weight or 0.0), str(lead.get("bucket")), str(lead.get("id")))
    ordered = sorted((pairs_[0] for pairs_ in by_bucket.values() if len(pairs_) == 1), key=order)
    return [tuple(pr) for pr in ordered[:max(0, ASSUMED_MAX_BUCKETS - len(named))]]


def _points_said(n, fact, or_better):
    """ "a 38 or better", or "a perfect 50" when only full points will do."""
    if n >= fact["points"]:
        return f"a perfect {n}"
    return f"a {n} or better" if or_better else f"a {n}"


def say_assumed(keep, step):
    """The assumed-item sentence in slot form, or None:
    "If the {next:<id>} is worth about 50 points like the last ones, a 38 or better keeps the B and a 46 gets
    it to an A." Either half alone too ("..., a 46 or better gets it to an A."). Both halves are about one
    bucket at one size. When only full points will do, "a perfect 50 gets it to an A"."""
    lead = keep or step
    if lead is None or (keep and step and (keep.get("points"), keep.get("class"), keep.get("bucket"))
                        != (step.get("points"), step.get("class"), step.get("bucket"))):
        return None
    parts = []
    if keep:
        kn = needed_points(keep)
        if kn is None or not keep.get("letter"):
            return None
        parts.append(f"{_points_said(kn, keep, True)} keeps the {keep['letter']}")
    if step:
        sn = needed_points(step)
        if sn is None or not _number(step.get("target")):
            return None
        to = insights.letter_for(step["target"])
        parts.append(f"{_points_said(sn, step, not keep)} gets it to a {to}")
    head = f"If the {{next:{lead['id']}}} is worth about {lead['points']:g} points like the last ones, "
    return fix_articles(head + " and ".join(parts) + ".")


def _assumed_forms(cited):
    keeps = [f for f in cited if is_keep(f)]
    steps = [f for f in cited if f.get("kind") == "what_if"]
    if len(keeps) > 1 or len(steps) > 1 or len(keeps) + len(steps) != len(cited):
        return set()
    keep, step = (keeps or [None])[0], (steps or [None])[0]
    if not all(_assumable(f) for f in cited):
        return set()
    say = say_assumed(keep, step)
    return {say} if say else set()


def drag_fact(class_facts):
    """The class's drag fact when code says it: only on a class with no recent move, and on an A only within
    DRAG_A_MARGIN points of the B line (the engine leaves it off past that too)."""
    if any(f.get("kind") == "grade_move" for f in class_facts):
        return None
    at_a = next((f for f in class_facts if f.get("kind") == "current_letter" and _number(f.get("pct"))), None)
    if at_a and at_a["pct"] - dict(insights.SCALE)["A"] >= insights.DRAG_A_MARGIN:
        return None
    return next((f for f in class_facts if f.get("kind") == "drag" and f.get("title") and _has_score(f)), None)


def say_drag(fact):
    """ "The {item:<drag id>} is what's pulling it down most." Code fills "Unit 2 Test (31/50)"."""
    return _DRAG_FORM.format(id=fact["id"]) if fact and fact.get("id") else None


def opening_fact(class_facts):
    """The fact the story opens on: the latest grade_move, else next_letter, else current_letter (an A)."""
    for kind in ("grade_move", "next_letter", "current_letter"):
        fact = next((f for f in class_facts if f.get("kind") == kind and not f.get("move") and say_opening(_say_fields(f))),
                    None)
        if fact:
            return fact
    return None


# ── Code checks ──────────────────────────────────────────────────────────────


def _number_role(text, start, end):
    """What a number in a sentence is: a move, a weight, a distance to a letter, or a general value."""
    before, after = text[max(0, start - 30):start], text[end:end + 40]
    if _MOVE_BEFORE_RE.search(before):
        return "move"
    if _WEIGHT_AFTER_RE.match(after) or _WEIGHT_BEFORE_RE.search(before):
        return "weight"
    if _NEEDED_AFTER_RE.match(after) or _NEEDED_BEFORE_RE.search(before):
        return "needed"
    if _SCORE_BEFORE_RE.search(before) and _SCORE_AFTER_RE.match(after):
        return "score"
    if _DISTANCE_AFTER_RE.match(after):
        return "distance"
    return "general"


def _add(values, value):
    values.add(float(value))
    values.add(abs(float(value)))


def _numbers_of(fact, role="general"):
    """The numbers a fact backs for one kind of claim. A move's size never backs a weight, and so on."""
    values = set()
    if role == "score":
        for earned, possible in (("earned", "possible"), ("prior_earned", "prior_possible")):
            if isinstance(fact.get(possible), (int, float)) and fact[possible]:
                values.add(round(fact[earned] / fact[possible] * 100, 1))
                if fact[possible] == 100:
                    values.add(float(fact[earned]))
        return values
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
    if fact.get("kind") in ("next_letter", "current_letter", "grade_move"):
        letters.update(shown_letters(fact))
    for key in ("from_pct", "to_pct", "pct", "target", "threshold"):
        if isinstance(fact.get(key), (int, float)):
            letters.add(insights.letter_for(fact[key]))
    return {x for x in letters if x}


def _needed_ok(text_number, cited):
    """A needed score is the exact value to one decimal or a round-up (84.2 -> 85). Never rounded down."""
    n = float(text_number)
    for fact in cited:
        v = fact.get("needed_pct")
        if fact.get("kind") in ("what_if", "keep_letter") and isinstance(v, (int, float)):
            if abs(n - v) < 0.051 or (v <= n < v + 1 and n <= 100):
                return True
    return False


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

    # "one and a half", "two and a half", "3 and a half" first, so "one" isn't left bare.
    def half(match):
        whole = match.group(1)
        value = float(whole) if whole[0].isdigit() else float(_words_value(whole))
        return f"{value + 0.5:g}"

    text = _unhyphen_numbers(text)
    text = _QUARTERS_RE.sub(lambda m: f"{({'one': 1, 'two': 2, 'three': 3}.get(m.group(1).lower()) or int(m.group(1))) * 0.25:g} "
                                      f"{m.group(2)}", text)
    def quarters(match):
        whole = match.group(1)
        value = float(whole) if whole[0].isdigit() else float(_words_value(whole))
        return f"{value + (0.75 if match.group(2).lower() == 'three' else 0.25):g}"

    text = _AND_QUARTERS_RE.sub(quarters, text)
    text = _AND_A_HALF_RE.sub(half, text)
    text = _SPELLED_RE.sub(swap, text)
    for pattern, value in _FRACTION_WORDS:
        text = pattern.sub(value, text)
    return text


def _strip_names(text, cited):
    """Remove class names, item titles and bucket names, whose digits aren't claims."""
    names = set()
    for fact in cited:
        for key in ("class", "title", "category", "bucket", "next_title"):
            if fact.get(key):
                names.add(str(fact[key]))
                # "Unit 2 Warm Ups (wk9-10)" is often written without its parenthetical.
                short = re.sub(r"\s*\([^)]*\)\s*$", "", str(fact[key])).strip()
                if short and short != str(fact[key]):
                    names.add(short)
    nexts = {str(f["next_title"]) for f in cited if f.get("next_title")}
    for name in sorted(names, key=len, reverse=True):
        # A named next item reads like "the next test" to the needed-score rule.
        text = re.sub(re.escape(name), " next item " if name in nexts else " ", text, flags=re.I)
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
        in_parens = text[: match.start()].endswith("(") and text[match.end():].startswith(")")
        # "down 2.5 (10/14)": a pair that is a cited since/due date reads as that date.
        if in_parens and n == int(n) and m == int(m) and (int(n), int(m)) in _due_days(cited) and (n, m) not in scores:
            spans.append((match.start(), match.end(), (int(n), int(m))))
            continue
        if (n, m) in scores:
            spans.append((match.start(), match.end(), None))
        elif in_parens:
            # "Glaze Test (11/20)" is a score in parentheses, never a date; the score check judges it.
            spans.append((match.start(), match.end(), "score"))
        elif n == int(n) and m == int(m) and 1 <= n <= 12 and 1 <= m <= 31:
            spans.append((match.start(), match.end(), (int(n), int(m))))
        else:
            spans.append((match.start(), match.end(), "score"))
    return sorted(spans)


def _due_days(cited):
    """Dates a sentence may name: a cited what-if's next item due date, or a cited move's since date."""
    days = set()
    for fact in cited:
        for key in ("next_due", "since"):
            try:
                day = date.fromisoformat(str(fact.get(key) or ""))
            except ValueError:
                continue
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


def check_sentence(text, cites, index, name=None, classes=None):
    """None if the sentence is backed by the facts it cites, else the reason it's dropped.

    classes is the student's class names (write_class passes them); the index's own classes are added.
    A sentence that cites a fact must be one of the fixed forms for the kinds it cites. A sentence that
    cites nothing drops.
    """
    text = (text or "").strip()
    if not text:
        return "empty"
    if not cites:
        return UNCITED_REASON
    cited = []
    for fid in cites:
        fact = index.get(fid)
        if fact is None:
            return f"unknown fact {fid}"
        if name is not None and fact.get("class") != name:
            return f"fact {fid} is another class"
        cited.append(fact)
    reason = slot_reason(text, cites, index) or leak_reason(text, index)
    if reason:
        return reason
    # The claim checks read each slot as a neutral token plus its score, so a title's own dates,
    # letters or "B+" never trip them; the text outside the slots is checked as usual.
    neutral = neutral_slots(text, index)
    echo = _ECHO_RE.search(neutral) or _SAME_LETTER_MOVE_RE.search(neutral)
    if echo:
        return f"echoes the prompt ({echo.group(0)})"
    reason = _claims_reason(neutral, cited, index, slotted=text)
    if reason:
        return reason
    # Last, so the claim checks keep their own reasons: any school-work noun must sit in a slot, and a
    # class is named only by its own name or an unambiguous short form of it.
    all_classes = list(dict.fromkeys([*(classes or ()), *(f.get("class") for f in index.values() if f.get("class"))]))
    reason = names_reason(text, cited, all_classes)
    if reason:
        return reason
    # Fail closed: a sentence that cites a fact is one of the fixed forms for the kinds it cites.
    reason = form_reason(text, cites, index)
    if reason:
        return reason
    # Whatever the fill produces must not carry a raw id or a brace either.
    return leak_reason(fill_slots(text, index), index, filled=True)


# ── Slots ─────────────────────────────────────────────────────────────────────
# Code writes items, buckets and classes only as slots, which it fills after the checks:
#   {item:<cause id>}     the cause's title, plus "(earned/possible)" unless the sentence has a "(...)" after it
#   {next:<what_if id>}   the named next item's title, else "next <bucket>" ("next test", "next assessment",
#                         "next coursework item")
#   {bucket:<fact id>}    the bucket or category name of a cause, what_if or keep fact ("Homework")
#   {class:<fact id>}     the class name
# Outside a slot, a sentence may not use a school-work noun at all: no name guessing.
SLOT_RE = re.compile(r"\{(item|next|class|bucket):([^{}\s]+)\}")
SLOT_KINDS = {"next": ("what_if", "keep_letter")}
# What the claim checks see in place of each slot.
NEUTRAL = {"item": "Zitem", "next": "next Znext", "class": "Zclass", "bucket": "Zbucket"}
_WORK_NOUN_RE = re.compile(
    r"\b(?:tests?|exams?|quiz(?:zes)?|labs?|projects?|packets?|assignments?|homework|warm[- ]?ups?|essays?|"
    r"reports?|summatives?|formatives?|assessments?|coursework|classwork|worksheets?|finals?|midterms?|papers?|"
    r"presentations?|notebooks?|practicals?|retakes?|sketch(?:es)?|drawings?|portfolios?|journals?|posters?|"
    r"units?|chapters?|ch\.)(?![\w])", re.I)
# A capitalized school subject that isn't one of the student's classes ("Physics scores dragged it." in a
# Geo note). Title case only, so "the low band", "state of the art" or "posting history" never trip it.
# The CTO's ruling: this fixed list runs on top of the student's own class names, never instead of them.
_SUBJECT_RE = re.compile(
    r"\b(?:Math(?:s|ematics)?|Algebra|Geometry|Calculus|Statistics|Physics|Chemistry|Chem|Biology|Bio|Science|"
    r"History|APUSH|English|Spanish|French|German|Latin|Chinese|Art|Ceramics|Music|Band|Choir|Orchestra|Drama|"
    r"Theater|Health|Economics|Government|Civics|Engineering|Marketing|Business|Psychology|Geography|Yearbook)\b|"
    r"(?<!\w)P\.E\.(?!\w)")
_SUBJECT_IDIOMS = re.compile(r"\bplain English\b")
# Phrases that may use a school-work noun outside a slot. Kept empty on purpose: "the next test" goes
# through {next:<id>} or {bucket:<id>}. Add only with a test.
NOUN_WHITELIST = ()
# A raw id ("pattern:posting_day:geo"), a bracketed or bare slot word, a fullwidth or stray brace.
_ID_LIKE_RE = re.compile(r"\b[a-z_]+:[\w:.-]+")
# In a fill: a slot word, a colon, then an id-like token (word, colon, more), as a slot-shaped title leaves it.
_FILL_SLOT_ID_RE = re.compile(r"\b(?:item|next|class|bucket|pattern)\s*[:：]\s*[a-z_]+\s*[:：]\s*[\w.-]", re.I)
_SLOT_DEBRIS_RE = re.compile(r"[\[(<]\s*(?:item|next|class|bucket|pattern)\s*:|\b(?:item|next|class|bucket|pattern)\s*:|[{}｛｝]",
                             re.I)
# Short forms of class-name words ("Geometry H" -> "Geo").
_CLASS_ABBREV = {"geometry": "geo", "calculus": "calc", "chemistry": "chem", "biology": "bio", "statistics": "stats",
                 "mathematics": "math", "algebra": "alg", "physics": "phys", "history": "hist", "spanish": "span",
                 "economics": "econ", "government": "gov", "psychology": "psych", "engineering": "eng",
                 "english": "eng", "marketing": "mktg"}
_CLASS_SKIP = {"ap", "ib", "hon", "honors", "adv", "advanced", "int", "intro", "course", "and", "of", "the", "for",
               "ll", "lll", "ii", "iii", "iv", "cp", "ab", "bc", "sl", "hl"}


def _clean(value):
    """A title or name as one plain line: no braces, no newlines or runs of whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[{}｛｝]", "", str(value or ""))).strip()


def _short_title(title):
    title = _clean(title)
    return re.sub(r"\s*\([^)]*\)\s*$", "", title).strip() or title


def _bucket_of(fact):
    return fact.get("bucket") or fact.get("category")


# Bucket words a "next ..." fill can stand on alone ("the next assessment"). Mass nouns like "Homework" get
# "assignment" ("the next homework assignment"), "Participation" gets "score", and any other word that isn't
# a plural gets "item". A bucket of two kinds reads as either one ("Labs & Projects" -> "lab or project").
_COUNT_BUCKETS = {"assessment", "test", "quiz", "lab", "project", "exam", "essay", "assignment", "summative",
                  "formative", "task", "final", "midterm", "activity", "worksheet", "presentation", "paper", "report"}
_MASS_BUCKETS = {"coursework", "homework", "classwork", "participation", "practice", "fluency", "work", "progress"}
_MASS_TAIL = {"participation": "score", "progress": "score"}


def _singular(word):
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("zzes"):
        return word[:-3]
    if re.search(r"(?:ch|sh|x|ss)es$", word):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _bucket_noun(bucket):
    """ "Assessments" or "Assessment" -> "assessment"; "Quizzes" -> "quiz"; "Labs & Activities" ->
    "lab or activity"; a mass noun like "Homework" or "Math Fluency" -> "homework assignment",
    "math fluency assignment"; "Participation" -> "participation score"."""
    words = re.sub(r"\s*([/&])\s*", r" \1 ", str(bucket or "test")).strip().lower().split() or ["test"]
    if words[-1] in _MASS_BUCKETS:
        tail = _MASS_TAIL.get(words[-1], "assignment")
        return " ".join("or" if w in ("&", "and", "/") else w for w in words) + f" {tail}"
    singular = ["or" if w in ("&", "and", "/") else _singular(w) for w in words]
    noun = " ".join(singular)
    if singular[-1] in _COUNT_BUCKETS or singular[-1] != words[-1]:
        return noun
    return f"{noun} item"


def _item_noun(name):
    """One next item of a category, singular: "Projects and Presentations" -> "project or presentation",
    "Learning Targets" -> "learning target", "Homework" -> "homework assignment". When a name can't be made
    singular safely ("Speaking and Listening", "Reading, Writing"), "item in Speaking and Listening"."""
    words = re.sub(r"\s*([/&])\s*", r" \1 ", str(name or "")).strip().split()
    if not words or not all(re.fullmatch(r"[A-Za-z][A-Za-z'-]*|[/&]", w) for w in words):
        return f"item in {name}" if words else "test"
    parts, part = [], []
    for w in words + ["and"]:
        if w.lower() in ("&", "and", "/"):
            parts.append(part)
            part = []
        else:
            part.append(w.lower())
    if any(not p for p in parts):
        return f"item in {name}"
    if parts[-1][-1] in _MASS_BUCKETS:
        return _bucket_noun(name)
    for p in parts:
        last = _singular(p[-1])
        if last == p[-1] and last not in _COUNT_BUCKETS:
            return f"item in {name}"
    return _bucket_noun(name)


def slot_reason(text, cites, index):
    """None if every slot names one of the sentence's cited facts, of the right kind, else why not."""
    found = SLOT_RE.findall(text)
    for kind, fid in found:
        if fid not in (cites or []):
            return f"slot {{{kind}:{fid}}} names a fact the sentence doesn't cite"
        fact = index.get(fid) or {}
        if kind == "item" and not fact.get("title"):
            return f"item slot {fid} has no title"
        if kind == "class" and not fact.get("class"):
            return f"class slot {fid} has no class"
        if kind == "bucket" and not _bucket_of(fact):
            return f"bucket slot {fid} has no bucket"
        if kind in SLOT_KINDS and fact.get("kind") not in SLOT_KINDS[kind]:
            return f"{kind} slot {fid} isn't a {' or '.join(SLOT_KINDS[kind])} fact"
    if len(set(found)) < len(found):
        return "repeats a slot"
    if re.search(r"\}\s*\{", text):
        return "adjacent slots"
    if re.search(r"[{}]", SLOT_RE.sub(" ", text)):
        return "malformed slot"
    return None


def _current_letters(index, cited):
    """The letter each cited class holds now, from its next_letter or current_letter fact or its latest move."""
    classes = {f.get("class") for f in cited}
    out = set()
    for fact in index.values():
        if fact.get("class") not in classes or fact.get("move"):
            continue
        if fact.get("kind") in ("next_letter", "current_letter", "grade_move"):
            out.add(shown_letters(fact)[1])
    return out


def leak_reason(text, index, filled=False):
    """Why the text still carries a raw fact id or slot debris once the real slots are out, or None.

    Times ("3:05") and scores ("11/20") aren't ids. On filled text only real ids, braces and a slot word
    followed by an id-like token ("item:cause:g:h") count, so "Next: Chapter 5" or "Ratio a:b" is fine.
    """
    bare = text if filled else SLOT_RE.sub(" ", text)
    for fid in sorted(index, key=len, reverse=True):
        if ":" in fid and fid in bare:
            return "leaks a raw fact id"
    if filled:
        # The id and slot-debris filters ran on the slot form (below). A fill only adds names from
        # the facts, so here only what a slot-shaped title could smuggle in counts: a brace, a real fact id,
        # or a slot word followed by an id-like token ("{item:cause:g:h}" fills as "item:cause:g:h").
        # "Next: Chapter 5", "Class: Notes Check" and "Ratio a:b Practice" are titles, not slots.
        if re.search(r"[{}｛｝]", bare):
            return "leaves slot debris"
        return "leaves slot debris" if _FILL_SLOT_ID_RE.search(bare) else None
    if _ID_LIKE_RE.search(bare):
        return "leaks a raw fact id"
    if _SLOT_DEBRIS_RE.search(bare):
        return "leaves slot debris"
    return None


def neutral_slots(text, index):
    """The sentence with each slot as a neutral token; an item slot keeps its score, as the fill would."""
    def swap(match):
        kind, fid = match.group(1), match.group(2)
        fact = index.get(fid) or {}
        token = NEUTRAL[kind]
        if kind == "item" and _has_score(fact) and not re.match(r"\s*\(", text[match.end():]):
            return f"{token} ({fact['earned']:g}/{fact['possible']:g})"
        return token
    return SLOT_RE.sub(swap, text)


def _has_score(fact):
    return isinstance(fact.get("earned"), (int, float)) and isinstance(fact.get("possible"), (int, float))


def _class_forms(name):
    """[(form, is_full)] for a class name: the full name, and short forms of its words ("Geometry H" -> Geometry, Geo)."""
    full = _clean(name)
    forms = [(full.lower(), True)]
    words = set(re.findall(r"[^\W\d_]+", full.lower()))
    # Short forms the fixed subject list knows, so a student's own "Physical Education" may be "P.E.".
    if {"physical", "education"} <= words:
        forms.append(("p.e.", False))
    if "history" in words and ({"us", "u"} & words or {"united", "states"} <= words):
        forms.append(("apush", False))
    for word in re.findall(r"[^\W\d_]+", full.lower()):
        if len(word) < 3 or word in _CLASS_SKIP or _WORK_NOUN_RE.fullmatch(word):
            continue
        forms.append((word, False))
        if word in _CLASS_ABBREV:
            forms.append((_CLASS_ABBREV[word], False))
    return forms


# Words after which a lowercase class name is a name, not a common word: "mostly from physics".
_LEAD_INS = "(?:%s)" % "|".join(rf"(?<=\b(?i:{w}) )" for w in (
    "from", "on", "in", "by", "for", "than", "and", "or", "with", "plus", "but", "like", "after", "to", "mostly",
    "mainly", "largely", "chiefly", "primarily"))


def names_reason(text, cited=(), classes=()):
    """Why the sentence names school work or a class outside a slot, or None.

    The cited class's own full name, or a short form that maps to it alone, may be written out; another of
    the student's classes, an ambiguous short form, a capitalized subject that isn't the student's own, or
    any school-work noun drops the sentence. Quotes are ignored.
    """
    bare = SLOT_RE.sub(" ", text)
    bare = _SUBJECT_IDIOMS.sub(" ", re.sub(r"[\"“”‘’'`]", "", bare))
    for phrase in NOUN_WHITELIST:
        bare = re.sub(re.escape(phrase), " ", bare, flags=re.I)
    own = {f.get("class") for f in cited if f.get("class")}
    owners = {}
    for cls in classes or ():
        for form, is_full in _class_forms(cls):
            owners.setdefault((form, is_full), set()).add(cls)
    student_words = {form for form, _ in owners}
    for (form, is_full), who in sorted(owners.items(), key=lambda kv: (-len(kv[0][0]), not kv[0][1])):
        # A full name of more than one word matches in any case; a one-word name or short form capitalized
        # ("Geo", "GEO", "Band"), or lowercase only at a sentence start, before scores, grade or class
        # ("physics scores dragged it."), or after a word like "from" or "mostly" ("mostly from physics."),
        # never "the art of", "the low band" or "its posting history".
        word = re.escape(form)
        pattern = (re.compile(rf"(?<!\w){word}(?!\w)", re.I) if is_full and not re.fullmatch(r"[^\W\d_]+", form) else
                   re.compile(rf"(?<!\w)(?:{re.escape(form.capitalize())}|{re.escape(form.upper())})(?!\w)|"
                              rf"(?:^\s*|(?<=[.!?;:]\s)|(?<=[.!?;:]\s\s)){word}(?!\w)|"
                              rf"(?<!\w){word}(?=\s+(?:scores?|grades?|class(?:es)?)\b)|"
                              rf"{_LEAD_INS}{word}(?!\w)"))
        for match in list(pattern.finditer(bare)):
            if who <= own and (is_full or len(who) == 1):
                continue
            if who & own:
                return f"names a class by an ambiguous short form ({match.group(0)})"
            return f"names another class ({match.group(0)})"
        if who <= own and (is_full or len(who) == 1):
            bare = pattern.sub(" ", bare)
    hit = _WORK_NOUN_RE.search(bare)
    if hit:
        return f"names school work ({hit.group(0)}) outside a slot"
    for match in _SUBJECT_RE.finditer(bare):
        if match.group(0).lower() not in student_words:
            return f"names another class ({match.group(0)})"
    return None


def work_noun_outside_slot(text, cited=(), classes=()):
    """The word names_reason objects to, or None. Kept for callers and probes."""
    reason = names_reason(text, cited, classes or [f.get("class") for f in cited if f.get("class")])
    return re.search(r"\(([^)]*)\)", reason).group(1) if reason else None


def fill_slots(text, index):
    """The sentence with each slot replaced by the real name from its fact, on one line, without braces."""
    def fill(match):
        kind, fid = match.group(1), match.group(2)
        fact = index.get(fid) or {}
        if kind == "class":
            return _clean(fact.get("class"))
        if kind == "bucket":
            name = _clean(_bucket_of(fact))
            # "Homework scores dragged it." and "Mostly Labs scores." keep the name as is; after "the", "a",
            # "an", "one" or "next" it reads as a common noun: "the next homework score".
            lead = re.search(r"\b(?:the|a|an|one|next)\s*$", text[:match.start()], re.I)
            return name.lower() if lead and not name.isupper() else name
        if kind == "next":
            if fact.get("next_title"):
                return _clean(fact["next_title"])
            # The Aeries category name when the bucket has one ("Learning Targets (Quizzes)" -> "next learning
            # target"), else the bucket's.
            noun = re.sub(r"\s*\([^()]*\)\s*$", "", _clean(fact.get("category") or fact.get("bucket")))
            return f"next {_item_noun(noun or _clean(fact.get('bucket')))}"
        title = _clean(fact.get("title"))
        if _has_score(fact) and not re.match(r"\s*\(", text[match.end():]):
            return f"{_short_title(title)} ({fact['earned']:g}/{fact['possible']:g})"
        return title if not _has_score(fact) else _short_title(title)
    return SLOT_RE.sub(fill, text)


def _class_moves(index, cited):
    classes = {f.get("class") for f in cited}
    return [f for f in index.values() if f.get("kind") == "grade_move" and not f.get("move")
            and f.get("class") in classes and isinstance(f.get("from_pct"), (int, float))]


def _class_at_a(index, cited):
    classes = {f.get("class") for f in cited}
    for f in index.values():
        if f.get("class") not in classes:
            continue
        pct = f.get("pct") if f.get("kind") in ("next_letter", "current_letter") else f.get("to_pct")
        if isinstance(pct, (int, float)) and insights.letter_for(pct) == "A":
            return True
    return False


def _said_letters(bare):
    out = []
    for match in _LETTER_RE.finditer(bare):
        before = bare[: match.start()].rstrip()
        after = bare[match.end():]
        # "A score..." or "A 74 or better..." at a sentence start is the article, not the letter.
        article = match.group(1) == "A" and (not before or before[-1] in ".!?") and re.match(r" [a-z0-9]", after)
        if not article:
            out.append(match.group(1))
    return out


def _cause_share(fact):
    return fact.get("share") if fact.get("share") is not None else fact.get("points")


def _direction_reason(slotted, cited, index):
    """Why a direction word says a cause pushed the grade the other way from its share, or None.

    Up words (helped, lifted, boosted, raised, saved, carried, rescued, improved, up, "gave it a boost") need
    a positive share; down words (hurt, dragged, cost, pulled, knocked, sank, damage, tanked, cut, dented,
    slashed, dropped, blamed, fell, down, "behind the drop") a negative one. Each word is read against the
    cause named by the nearest {item:} or {bucket:} slot before it (after it when none comes before:
    "Lifted by the {item:...}."); an adjective ("a low {item:...}", "a strong {item:...}") against the slot
    right after it. With no cause slot, every cited cause must match. "Didn't", "hardly" or "barely" turn a
    word around. form_reason drops any sentence that cites a cause; this is not the main check.
    """
    causes = [f for f in cited if f.get("move")]
    if not causes:
        return None
    slots = [(m.start(), m.end(), (index.get(m.group(2)) or {}, m.group(1))) for m in SLOT_RE.finditer(slotted)
             if m.group(1) in ("item", "bucket")]
    slots = [(a, b, f) for a, b, f in slots if f[0].get("move")]
    bare = SLOT_RE.sub(lambda m: " " * len(m.group(0)), slotted)
    words = [(m.start(), 1, False) for m in _UP_PHRASE_RE.finditer(bare)]
    words += [(m.start(), -1, False) for m in _DOWN_PHRASE_RE.finditer(bare)]
    for m in _UP_VERB_RE.finditer(bare):
        words.append((m.start(), 1, False))
    for m in _DOWN_VERB_RE.finditer(bare):
        up = m.group(0).lower().startswith(("pull", "knock")) and _UP_PARTICLE_AFTER_RE.match(bare[m.end():])
        words.append((m.start(), 1 if up else -1, False))
    words += [(m.start(), -1, True) for m in _LOW_ADJ_RE.finditer(bare)]
    words += [(m.start(), 1, True) for m in _HIGH_ADJ_RE.finditer(bare)]
    for start, way, adjective in sorted(words):
        if not adjective and _NEGATED_RE.search(bare[:start]):
            way = -way
        before = [f for a, b, f in slots if b <= start]
        after = [f for a, b, f in slots if a >= start]
        everyone = [(c, "item") for c in causes]
        if adjective:
            named = [after[0]] if after else [before[-1]] if before else everyone
        else:
            named = [before[-1]] if before else [after[0]] if after else everyone
        for cause, kind in named:
            share = (_bucket_net(index, cause) if kind == "bucket" else _cause_share(cause)) or 0
            if share * way <= 0:
                return "says a cause pushed the grade the other way from its share"
    return None


def _claims_reason(text, cited, index, slotted=None):
    """Every claim check except the prompt-echo and example-copy bans.

    slotted is the sentence with its slots (check_sentence passes it), so each direction verb is read
    against the cause its slot names; without it, against every cited cause.
    """
    text = spelled_to_digits(text)
    form = (slotted or "").strip()
    # Code's fixed forms: the drag line names its item as pulling the grade down with no move, and the
    # assumed-item line's numbers are points on an item of the stated size (needed_points).
    drag_form = bool(_DRAG_RE.match(form)) and [f.get("kind") for f in cited] == ["drag"]
    assumed = bool(_ASSUMED_RE.match(form)) and bool(cited) and all(
        f.get("kind") in ("what_if", "keep_letter") for f in cited)
    assumed_numbers = {float(n) for n in (needed_points(f) for f in cited) if n is not None} if assumed else set()
    masked, unbacked_dates, scores = _mask_allowed_dates(text, cited)
    if unbacked_dates:
        return "names a date"
    if _STILL_RE.search(masked) and any(f.get("kind") in ("next_letter", "current_letter", "grade_move")
                                         and say_opening(f) and not say_opening(f).startswith("Still")
                                         for f in cited):
        return "says still across a letter change"
    # An unnamed next item is only assumed: nothing says it's on the calendar.
    if _SCHEDULED_RE.search(masked) and any(f.get("kind") in ("what_if", "keep_letter") and not _named(f)
                                             for f in cited):
        return "says an assumed item is scheduled"
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
    if _ADVICE_RE.search(_strip_names(masked, cited)) and not any(f.get("kind") in ("what_if", "keep_letter")
                                                                     for f in cited):
        return "gives advice no what_if backs"
    if _NOTHING_RE.search(_strip_names(masked, cited)):
        return "says what isn't there"
    move_ids = [f["move"] if f.get("move") else f["id"] for f in cited if f.get("kind") == "grade_move"]
    moves = [index.get(m) or {} for m in dict.fromkeys(move_ids)]
    # A bare "mainly"/"mostly" is cause wording, except in a posting pattern ("scores mostly post Fridays").
    pattern_only = any(f.get("kind") == "posting_day" for f in cited) and not _GRADE_WORDS_RE.search(masked)
    cause_words = (_CAUSE_CLAIM_RE.search(masked) or (_BARE_CAUSE_RE.search(masked) and not pattern_only)) \
        and not drag_form
    if cause_words and not moves:
        return "states a cause without citing a grade_move or cause"
    if _MOVE_REF_RE.search(masked) and not moves:
        return "talks about a move with no grade_move cited"
    # The exact contrast forms ("The {item:...} helped." on a drop, "... hurt." on a rise) are never read as the
    # reason for the move (and form_reason drops any sentence that cites a cause).
    # The exact contrast forms, and a bucket's weight (no direction at all), may name an offsetting cause.
    contrast_form = bool(form and (_FORM_CONTRAST_RE.match(form) or _FORM_WEIGHT_RE.match(form)))
    # "Mostly {bucket:...} scores." ranks buckets by net share (and form_reason drops it: it cites a cause).
    bucket_most = bool(slotted and re.match(r"^(?:Mostly (?:from )?(?:one )?\{bucket:|\{bucket:[^{}\s]+\}(?: scores?)? did most)",
                                            slotted.strip()))
    claims_cause = any(f.get("move") for f in cited) or (moves and cause_words)
    if claims_cause and not all(cause_allowed(m) for m in moves):
        return "states a cause the move's causes don't explain"
    for move in moves:
        said = [f for f in cited if f.get("move") == move.get("id")]
        if not said:
            continue
        sign = 1 if (move.get("delta") or 0) >= 0 else -1
        drivers = [c["id"] for c in ranked_causes(move) if (c.get("share") or c.get("points") or 0) * sign > 0]
        said_drivers = [f["id"] for f in said if f["id"] in drivers]
        if said_drivers and _MOST_RE.search(masked) and said_drivers[0] != drivers[0] and not bucket_most:
            return "names causes out of share order"
        # A cause that pushed the other way may be named, but never as the reason for the move.
        if not said_drivers and cause_words and not contrast_form:
            return "gives an offsetting cause as the reason for the move"
        # Named beside the drivers, a cause that pushed the other way must be set against the move.
        if len(said_drivers) < len(said) and not _CONTRAST_RE.search(masked) and not contrast_form:
            return "names an offsetting cause as if it drove the move"
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
    said = _said_letters(bare)
    keeps = [f for f in cited if is_keep(f)]
    if _KEEP_CLAIM_RE.search(bare):
        if not keeps:
            return "says the letter holds with no keep_letter fact cited"
        if _CANT_LOSE_RE.search(bare) and not any(f.get("cannot_lose") for f in keeps):
            return "says the next item can't cost the letter, but the keep_letter fact needs a score"
        if not _CANT_LOSE_RE.search(bare) and not any(isinstance(f.get("needed_pct"), (int, float)) for f in keeps):
            return "says a score keeps the letter, but the keep_letter fact has no needed score"
        held = {f.get("letter") for f in keeps}
        current = _current_letters(index, cited)
        for match in _KEEP_LETTER_RE.finditer(bare):
            letter = match.group(1) or match.group(2)
            if letter not in held or (current and letter not in current):
                return f"says it keeps {letter}, but the keep_letter fact is for the current letter {'/'.join(sorted(held))}"
    if _NEXT_CLAIM_RE.search(bare):
        # The assumed-item line may say both halves of one item: keeps the letter, and gets it to the next.
        if keeps and not (assumed and any(f.get("kind") == "what_if" for f in cited)):
            return "a keep_letter fact never backs a gets-it-to claim"
        if not any(f.get("kind") in ("next_letter", "what_if") for f in cited):
            return "claims a next letter with no next_letter or what_if fact"
        if _class_at_a(index, cited):
            return "claims a next letter for a class at an A"
    if _BACK_RE.search(bare) and said:
        held = {prior for prior, now in map(shown_letters, _class_moves(index, cited)) if prior and prior != now}
        if not set(said) & held:
            return "says back to or again with a letter no move shows the class held"
    for match in _LETTER_MOVE_RE.finditer(bare):
        # A crossing into the letter, the way the words say: "slipped to X" needs X below the shown letter it
        # left, "up to X" above it (shown_direction).
        letter, way = match.group("letter"), _letter_move_direction(match)
        crossed = [m for m in moves if shown_letters(m)[1] == letter and shown_direction(m)]
        if not crossed:
            return f"says the class moved to {letter} but no move crossed into that letter"
        if way is None or not any(way in (0, shown_direction(m)) for m in crossed):
            return f"says the class moved to {letter} the wrong way"
    for match in _NUMBER_RE.finditer(bare):
        if float(match.group(0)) in assumed_numbers:
            continue
        role = _number_role(bare, match.start(), match.end())
        if role == "needed":
            if _needed_ok(match.group(0), cited):
                continue
            if any(f.get("kind") in ("what_if", "keep_letter") and isinstance(f.get("needed_pct"), (int, float))
                   for f in cited):
                return f"number {match.group(0)} isn't the needed score or a round-up of it"
            return f"number {match.group(0)} isn't in the cited facts"
        allowed = set().union(*(_numbers_of(f, role) for f in cited))
        if not _number_ok(match.group(0), allowed):
            suffix = f" as a {role}" if role in ("move", "weight", "score") else ""
            return f"number {match.group(0)} isn't in the cited facts{suffix}"
    letters = set().union(*(_letters_of(f) for f in cited))
    for letter in said:
        if letter not in letters:
            return f"letter {letter} isn't in the cited facts"
    # Last but one, so the older checks keep their reasons: "rose", "fell", "up", "down" and the like need
    # a cited grade_move (a cited cause cites its move) whose delta has that sign.
    for pattern, sign in ((_MOVED_UP_RE, 1), (_MOVED_DOWN_RE, -1)):
        if drag_form and sign < 0:
            continue
        if pattern.search(bare) and not any((m.get("delta") or 0) * sign > 0 for m in moves):
            return "says the grade moved a way no cited move did"
    return _direction_reason(slotted if slotted is not None else text, cited, index)


# ── Cause wording read by the claim checks ───────────────────────────────────
# Code writes no cause sentence, and form_reason drops any sentence that cites a cause. _claims_reason still
# reads these shapes on every sentence it checks: the contrast forms ("The {item:<cause>} helped.") and a
# bucket's weight ("{bucket:<cause>} is 50% of the grade."). The item form may carry " score" and the
# "(n/m)" score the fill adds.
_FORM_ITEM = r"(?i:the) \{item:([^{}\s]+)\}(?: score)?(?: \(\d+(?:\.\d+)?/\d+(?:\.\d+)?\))?"
_FORM_BUCKET = r"\{bucket:([^{}\s]+)\}(?: scores?)?"
_FORM_ONE = rf"(?:{_FORM_ITEM}|{_FORM_BUCKET})"
_FORM_LIST = rf"{_FORM_ONE}(?:(?:, {_FORM_ONE})?,? and {_FORM_ONE})?"
_FORM_CONTRAST_RE = re.compile(rf"^(?P<who>{_FORM_LIST}) (?:helped|hurt)\.$")
# A bucket's weight with no direction in it: "{bucket:<cause>} is 50% of the grade."
_FORM_WEIGHT_RE = re.compile(r"^\{bucket:[^{}\s]+\}(?: scores)? (?:is|are|counts? for) \d+(?:\.\d+)?% of the grade\.$")
FORM_REASON = "cited sentence not in an allowed form"  # one reason for every fixed-form gate
# A bucket whose causes net to less than this, either way, has no direction to claim.
BUCKET_MIN_NET = 0.1


def _folded(move):
    """True when #54 folded some of the move's causes away (other_causes, other_share).

    A folded cause's bucket is gone, so no bucket's net share in the move is known: fail closed.
    """
    return (move.get("other_causes") or 0) > 0 or move.get("other_share") not in (None, 0)


def _bucket_nets(move):
    """{bucket: the sum of its causes' shares in this move}, rounded to 6 places."""
    nets = {}
    for cause in move.get("causes") or []:
        bucket = _bucket_of(cause)
        if bucket:
            nets[bucket] = nets.get(bucket, 0.0) + (_cause_share(cause) or 0.0)
    return {b: round(v, 6) for b, v in nets.items()}


def _bucket_net(index, cause):
    """The net share of the bucket a cause sits in, across its move; 0 when |net| < BUCKET_MIN_NET."""
    move = index.get(cause.get("move")) or {}
    if _folded(move):
        return 0.0
    net = _bucket_nets(move).get(_bucket_of(cause), 0.0)
    return net if abs(net) >= BUCKET_MIN_NET - 1e-9 else 0.0


def _opening_forms(move):
    opening = say_opening(_say_fields(move)) if move else None
    if not opening:
        return set()
    lower = opening[0].lower() + opening[1:]
    return {opening, fix_articles(opening), f"{{class:{move['id']}}} {lower}"}


UNCITED_REASON = "cites nothing"  # every sentence cites at least one fact


def _step(fact):
    return _say_fields(fact).get("say_step") if fact.get("kind") == "what_if" else None


def form_reason(text, cites, index):
    """None if a sentence that cites facts is one of the fixed forms for the set of kinds it cites.

      grade_move                 its say_opening ({class:} prefix allowed)
      next_letter                its say_opening ({class:} prefix allowed)
      what_if, with or without the next_letter   its say_step, word for word (not on a bucket_posted one)
      keep_letter                its say_keep, word for word
      keep_letter and/or what_if at the assumed size   say_assumed, word for word
      drag                       say_drag, word for word
      a cause, posting_day, scale_break, no_raise_math, any other kind or mix: no sentence (code writes none).
    """
    text = (text or "").strip()
    cited = _cited_facts(cites, index)
    if not cited:
        return None
    kinds = {"cause" if f.get("move") else f.get("kind") for f in cited}
    if kinds == {"drag"}:
        return None if len(cited) == 1 and text == say_drag(cited[0]) else FORM_REASON
    if _ASSUMED_RE.match(text) and kinds <= {"keep_letter", "what_if"}:
        return None if text in _assumed_forms(cited) else FORM_REASON
    if kinds == {"grade_move"}:
        ok = any(text in _opening_forms(f) for f in cited)
    elif kinds == {"next_letter"}:
        ok = any(text in _opening_forms(f) for f in cited)
    elif kinds == {"current_letter"}:
        ok = len(cited) == 1 and text == say_opening(cited[0])
    elif kinds in ({"what_if"}, {"what_if", "next_letter"}):
        # Never about "the next test" at a typical size when that bucket has a posted item: the posted one wins.
        ok = any(text == _step(f) for f in cited if f.get("kind") == "what_if" and not f.get("bucket_posted"))
    elif kinds == {"keep_letter"}:
        ok = any(text in _keep_forms(f) for f in cited)
    else:
        ok = False
    return None if ok else FORM_REASON


def _keep_forms(fact):
    """A keep_letter fact's sentence: its say_keep, word for word (the score form or the can't-drop form)."""
    say = fact.get("say_keep") or say_keep(fact)
    return {say} if say else set()


def eval_story(sentences, index):
    """None if the story passes the acceptance bar, else why it fails.

    Fails a story that restates past-due or missing work or that's out of order (letter, cause,
    pattern, next step). The first sentence must state the letter with the gap or the move, backed
    by a cited next_letter or grade_move. Code builds every story (its opening, the drag line, the
    keep and step lines) and sets its order.
    """
    reason = _story_basics(sentences, index)
    if reason:
        return reason
    if not _opens_with_letter(sentences[0], index):
        return "doesn't open with the letter and the gap or move"
    return None


def _story_basics(sentences, index):
    if not sentences:
        return "no story"
    order = []
    for sentence in sentences:
        cited = _cited_facts(sentence.get("cites"), index)
        masked, _, _ = _mask_allowed_dates(sentence.get("text") or "", cited)
        # A cited item's own title ("Late Work Reflection", "Lab due") is no restatement of the to-do list.
        masked = _without_titles(masked, cited)
        if _RESTATE_RE.search(masked):
            return "restates past-due or missing work"
        ranks = [ORDER.get((index.get(fid) or {}).get("kind"), 9) for fid in sentence.get("cites") or []]
        # A sentence sits where its latest kind goes: a what-if that also cites the move is a next step.
        order.append(max(ranks) if ranks else 9)
    if order != sorted(order):
        return "out of order (letter, cause, pattern, next step)"
    return None


def _without_titles(text, cited):
    """The text with each cited fact's own item names (title, next_title, short forms) blanked out."""
    names = {_clean(f.get(k)) for f in cited for k in ("title", "next_title") if f.get(k)}
    names |= {_short_title(n) for n in names}
    for name in sorted((n for n in names if n), key=len, reverse=True):
        text = re.sub(re.escape(name), " ", text, flags=re.I)
    return text


def _opens_with_letter(sentence, index):
    """The first sentence states a letter and a gap or move from a cited next_letter or grade_move."""
    cited = [index[fid] for fid in sentence.get("cites") or [] if fid in index]
    # A class at an A has no gap to state: its opening is exactly "Still an A." (or "Now an A.") from its current_letter.
    if [f.get("kind") for f in cited] == ["current_letter"]:
        return (sentence.get("text") or "").strip() == say_opening(cited[0])
    backers = [f for f in cited if f.get("kind") == "next_letter" or (f.get("kind") == "grade_move" and not f.get("move"))]
    if not backers:
        return False
    masked, _, _ = _mask_allowed_dates(spelled_to_digits(sentence.get("text") or ""), cited)
    bare = _strip_names(masked, cited)
    if not _said_letters(bare):
        return False
    sizes = [abs(f[k]) for f in backers for k in ("gap", "delta") if isinstance(f.get(k), (int, float))]
    said = [float(m.group(0)) for m in _NUMBER_RE.finditer(bare)]
    return any(abs(n - size) <= 0.051 for n in said for size in sizes)


def fix_articles(text):
    """ "a 85" -> "an 85", "A 11" -> "An 11", "a A" -> "an A", "an 97" -> "a 97". Only the article changes."""
    def wants_an(token):
        if not token[0].isdigit():
            return token in ("A", "E", "F")
        whole = token.split(".")[0]
        return whole.startswith("8") or whole in ("11", "18") or (len(whole) == 5 and whole[:2] in ("11", "18"))

    def swap(match):
        article, space, token = (match.group(1), match.group(2), match.group(3)) if match.group(1) else \
            (match.group(4), match.group(5), match.group(6))
        word = "an" if wants_an(token) else "a"
        return (word.capitalize() if article[0].isupper() else word) + space + token

    return _ARTICLE_RE.sub(swap, text or "")


def _unique(items):
    """Items with repeats removed, first occurrence kept, order kept."""
    seen = set()
    return [x for x in items if not (x in seen or seen.add(x))]


# family-data src/insights.js cleanInsights limits (all inclusive, lengths in UTF-16 code units like JS):
# text <= 600, cites 1..12 and each cite 1..120, class <= 120. A story over any limit, or with no cites, is dropped alone,
# so the rest of the publish still goes through.
PUBLISH_MAX_TEXT = 600
PUBLISH_MAX_CITES = 12
PUBLISH_MAX_CITE = 120
PUBLISH_MAX_CLASS = 120


# String.prototype.trim's whitespace set (not Python's strip()), as family-data's isText uses.
_JS_WHITESPACE = ("\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
                  "\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff")


def _js_len(text):
    """UTF-16 code units, as JS .length counts them."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def _fits_text(text, limit):
    """Not blank by JS trim, no lone surrogate (it can't be sent), at most `limit` UTF-16 units."""
    return (isinstance(text, str) and not any("\ud800" <= ch <= "\udfff" for ch in text)
            and text.strip(_JS_WHITESPACE) != "" and _js_len(text) <= limit)


def within_publish_limits(text, cites, class_name=None):
    """True when the Worker would accept this story line as is."""
    if not _fits_text(text, PUBLISH_MAX_TEXT):
        return False
    if class_name is not None and not _fits_text(class_name, PUBLISH_MAX_CLASS):
        return False
    cites = cites or []
    return 0 < len(cites) <= PUBLISH_MAX_CITES and all(_fits_text(c, PUBLISH_MAX_CITE) for c in cites)


def story_fits(story):
    return within_publish_limits((story or {}).get("story"), (story or {}).get("cites"), (story or {}).get("class"))


def code_lines(class_facts, name, classes=None):
    """The sentences code writes for a class after its opening, in slot form, in story order:
      drag      on a class with no recent move, its costliest past item (say_drag):
                "The {item:<drag id>} is what's pulling it down most."
      move_up   the next named item's step (say_step of move_up_step): "A 90 or better on the {next:<id>} gets
                it to an A."
      keep      its keep sentence (say_keep of pick_keep): "An 84 or better on the {next:<id>} keeps the B."
      assumed   for up to ASSUMED_MAX_BUCKETS buckets with nothing posted, heaviest first (assumed_picks):
                "If the {next:<id>} is worth about 50 points like the last ones, a 38 or better keeps the B
                and a 46 gets it to an A."
    Each passes check_sentence, or it isn't written."""
    index = fact_index(class_facts)
    step, keep, drag = move_up_step(class_facts), pick_keep(class_facts), drag_fact(class_facts)
    candidates = [("drag", [drag], say_drag(drag) if drag else None),
                  ("move_up", [step], _step(step) if step else None),
                  ("keep", [keep], say_keep(keep) if keep else None)]
    for pair in assumed_picks(class_facts):
        cited = [f for f in pair if f]
        candidates.append(("assumed", cited, say_assumed(*pair)))
    lines = []
    for role, cited, text in candidates:
        if not text or not all(cited):
            continue
        cites = [f["id"] for f in cited]
        if check_sentence(text, cites, index, name, classes) is None:
            lines.append({"text": text, "cites": cites, "code": role})
    return lines


def _ranked(sentences, index):
    """The sentences in story order (ORDER by each one's latest kind), stable."""
    def rank(s):
        return max((ORDER.get((index.get(c) or {}).get("kind"), 9) for c in s["cites"]), default=9)
    return sorted(sentences, key=rank)


def _filled(line, index):
    return {**line, "slots": line["text"], "text": fix_articles(fill_slots(line["text"], index))}


def _fit(final):
    """A story over the publish limits (600 characters, the cite count) loses the drag line first, then its
    second assumed line, so it can still publish; write_up drops a story still over them."""
    def fits(lines):
        return within_publish_limits(" ".join(s["text"] for s in lines), _unique([c for s in lines for c in s["cites"]]))
    for code in ("drag", "assumed"):
        if fits(final):
            break
        lines = [i for i, s in enumerate(final) if s.get("code") == code]
        if code == "assumed" and len(lines) < 2:
            continue
        if lines:
            final = [s for i, s in enumerate(final) if i != lines[-1]]
    return final


def _canonical_key(fact):
    if not isinstance(fact, dict):
        return ("", "", "", (1, 0.0), "", "", "", json.dumps(fact, sort_keys=True, default=str))
    weight = fact.get("bucket_weight_pct")
    return (str(fact.get("kind") or ""), str(fact.get("class") or ""), str(fact.get("bucket") or ""),
            (0, -float(weight)) if _number(weight) else (1, 0.0), str(fact.get("next_due") or fact.get("due") or ""),
            str(fact.get("id") or ""), str(fact.get("title") or fact.get("next_title") or ""),
            json.dumps(fact, sort_keys=True, default=str))


def canonical_facts(facts):
    """The facts in one total, stable order (kind, class, bucket, weight, due date, id, title, then every
    field), so every picker downstream sees the same list however the facts arrived."""
    return sorted(facts or [], key=_canonical_key)


def canonical_upcoming(upcoming):
    """Upcoming items in one total order (class, due, bucket, title, points, then every field)."""
    def key(item):
        if not isinstance(item, dict):
            return ("", "", "", "", "", json.dumps(item, sort_keys=True, default=str))
        return (str(item.get("class") or ""), str(item.get("due") or ""), str(item.get("bucket") or ""),
                str(item.get("title") or ""), str(item.get("points")), json.dumps(item, sort_keys=True, default=str))
    return sorted(upcoming or [], key=key)


def write_class(facts, name):
    """{"class", "story", "slotted", "cites", "keep_sentence"}, or None for a class with no letter to open on.

    Every class with an opening fact (opening_fact) gets a story: the opening, then code_lines, in story
    order. A story over the publish limits sheds its drag line, then its second assumed line (_fit).
    keep_sentence is the keep sentence as published (named or assumed), or None. letter and bullets are the
    note as the page shows it (note_bullets): the shown letter for the heading and the same lines as short
    bullets. The facts are put in canonical order first (canonical_facts).
    """
    facts = canonical_facts(facts)
    class_facts = facts_for(facts, name)
    opening = opening_fact(class_facts)
    if opening is None:
        return None
    index = fact_index(class_facts)
    classes = class_names(facts)
    code = code_lines(class_facts, name, classes)
    first = {"text": say_opening(_say_fields(opening)), "cites": [opening["id"]], "code": "opening"}
    if check_sentence(first["text"], first["cites"], index, name, classes) is not None:
        return None
    final = _ranked([_filled(first, index)] + [_filled(line, index) for line in code], index)
    if eval_story([{"text": s["text"], "cites": s["cites"]} for s in final], index):
        return None
    final = _fit(final)
    return {
        "class": name,
        "story": " ".join(s["text"] for s in final),
        "slotted": " ".join(s.get("slots", s["text"]) for s in final),
        "letter": shown_letters(opening)[1],
        "bullets": note_bullets(final, index),
        "cites": _unique([c for s in final for c in s["cites"]]),
        "keep_sentence": next((s["text"] for s in final if s.get("code") == "keep"
                               or (s.get("code") == "assumed" and any(is_keep(index.get(c) or {}) for c in s["cites"]))),
                              None),
    }



# ── Bullets (code, no model) ─────────────────────────────────────────────────
# The page shows each class as a heading (class name, shown letter, tag) over short bullets. Each bullet is
# built from the same facts as the line it stands for, in the same words, never by cutting up a sentence.
# The letter moved to the heading, so "Still an A." has no bullet and "Still a C, 0.5 points from a B." is
# "0.5 points from a B". Order: the distance to the next letter, the move since last time, what's pulling it
# down, then the next-item lines in story order. A line with nothing to say past the letter has no bullet.
_BULLET_RANK = {"next_letter": 0, "grade_move": 1, "drag": 2}


def say_opening_bullet(fact):
    """The opener as a bullet, or None: "0.5 points from a B" (next_letter), "Down 1.6 since Oct 14"
    (grade_move, same letter), or the crossing as say_opening says it without the period ("Slipped to a C
    since Oct 14, down 1.6", "Now an A, down 1.4 since Oct 14"). A current_letter, or a letter fact whose
    shown letter isn't the percent's, says nothing past the letter."""
    fact = _say_fields(fact)
    kind = fact.get("kind")
    prior, now = shown_letters(fact)
    if now is None:
        return None
    if kind == "next_letter" and fact.get("letter") == now and fact.get("next_letter") and _number(fact.get("gap")):
        nxt = fact["next_letter"]
        return f"{fact['gap']:g} points from {_article(nxt)} {nxt}"
    if kind == "grade_move" and all(_number(fact.get(k)) for k in ("from_pct", "to_pct", "delta")):
        if prior is not None and prior == now:
            way = "Down" if fact["delta"] < 0 else "Up"
            since = f" since {fact['since_label']}" if fact.get("since_label") else ""
            return f"{way} {abs(fact['delta']):g}{since}"
        said = say_opening(fact)
        return said[:-1] if said and said.endswith(".") else said
    return None


def _needed_said(n, fact, points):
    """ "38+", or "a perfect 50" when only full points will do (points only)."""
    return f"a perfect {n}" if points and n >= fact["points"] else f"{n}+"


def _cap(text):
    return text[:1].upper() + text[1:] if text.startswith("next ") else text


def _line_bullet(line, index):
    """(rank, slot-form bullet) for one code line of the story, or None."""
    facts = [index.get(c) or {} for c in line.get("cites") or []]
    code = line.get("code")
    if code == "opening" and facts:
        text = say_opening_bullet(facts[0])
        return (_BULLET_RANK.get(facts[0].get("kind"), 3), text) if text else None
    if code == "drag" and facts and facts[0].get("id"):
        return (2, f"Pulling it down: {{item:{facts[0]['id']}}}")
    if code == "move_up" and facts and _number(facts[0].get("needed_pct")) and _number(facts[0].get("target")):
        to = insights.letter_for(facts[0]["target"])
        return (3, f"{{next:{facts[0]['id']}}}: {_needed_said(_spoken_needed(facts[0]), facts[0], False)} gets "
                   f"{_article(to)} {to}")
    if code == "keep" and facts and facts[0].get("letter"):
        keep = facts[0]
        if keep.get("cannot_lose"):
            return (3, f"{{next:{keep['id']}}}: can't drop it below {_article(keep['letter'])} {keep['letter']}")
        if _number(keep.get("needed_pct")):
            return (3, f"{{next:{keep['id']}}}: {_spoken_needed(keep)}+ keeps the {keep['letter']}")
        return None
    if code == "assumed" and facts:
        keep = next((f for f in facts if is_keep(f)), None)
        step = next((f for f in facts if f.get("kind") == "what_if"), None)
        lead = keep or step
        parts = []
        if keep:
            parts.append(f"{_needed_said(needed_points(keep), keep, True)} keeps the {keep['letter']}")
        if step:
            to = insights.letter_for(step["target"])
            parts.append(f"{_needed_said(needed_points(step), step, True)} gets {_article(to)} {to}")
        return (3, f"{{next:{lead['id']}}} (about {lead['points']:g} pts): " + " and ".join(parts))
    return None


def note_bullets(final, index):
    """The story's lines as bullets, filled ("Pulling it down: Unit 2 Test (31/50)"), in bullet order."""
    picked = [b for b in (_line_bullet(line, index) for line in final) if b]
    picked.sort(key=lambda b: b[0])
    return [_cap(fill_slots(text, index)) for _rank, text in picked]


def note_text(story):
    """A story's text as published. The Worker keeps only class, story, cites and tags, so the note travels in
    story: the shown letter alone on the first line, then one "- " line per bullet. A story without a letter
    (written before bullets) publishes its sentences as they were."""
    letter, bullets = story.get("letter"), story.get("bullets")
    if letter not in _LETTERS or not isinstance(bullets, list):
        return story.get("story")
    return "\n".join([letter] + [f"- {b}" for b in bullets])

# ── Tags (code, no model) ────────────────────────────────────────────────────
# A class's tag says what its next item means: the easiest step up a letter, or the hardest score to keep the
# letter it has. Each is picked from what the class's story says: a named item ("Named" is an upcoming item
# attach_next_items matched at its posted points) or an assumed next item at its typical size. Across the kid's
# classes every named candidate ranks ahead of every assumed one, then the needed score decides. write_up
# works the tags out over the published stories and the facts they cite, so a tag always lands on a story
# that says it.
EASIEST_WIN, BIGGEST_RISK = "easiest_win", "biggest_risk"
STORY_TAGS = (EASIEST_WIN, BIGGEST_RISK)


def _named(fact):
    return bool(fact.get("next_title")) and _number(fact.get("points")) and fact["points"] > 0


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _spoken_needed(fact):
    """The needed score as a note says it (say_needed's whole number), so a tag never contradicts the note."""
    return math.ceil(round(fact["needed_pct"], 6))


def class_steps(class_facts):
    """The what_if facts a class's story can say as its step up, as code_lines says them: the soonest named
    one (move_up_step) when it reaches the next letter, else each assumed step (assumed_picks)."""
    step = move_up_step(class_facts)
    if step is not None:
        return [step]
    return [step for _keep, step in assumed_picks(class_facts) if step]


def class_keeps(class_facts):
    """The keep_letter facts a class's story can say with a needed score: pick_keep's named one, else each
    assumed keep (assumed_picks). Never a can't-drop fact."""
    named = pick_keep(class_facts)
    if named is not None:
        return [named] if _number(named.get("needed_pct")) else []
    return [keep for keep, _step in assumed_picks(class_facts) if keep]


def class_step(class_facts, said=None):
    """The step a class's story says that needs the lowest score, or None (named before assumed; said, when
    given, limits it to the ids the story cites)."""
    steps = [f for f in class_steps(class_facts) if said is None or f["id"] in said]
    return min(steps, key=lambda f: (_spoken_needed(f), f["id"]), default=None)


def class_keep(class_facts, said=None):
    """The keep a class's story says that needs the highest score, or None (named before assumed; said as in
    class_step)."""
    keeps = [f for f in class_keeps(class_facts) if said is None or f["id"] in said]
    return max(keeps, key=lambda f: (_spoken_needed(f), f["id"]), default=None)


def _class_pct(facts, name):
    """The class's percent from its next_letter, current_letter or grade_move fact, or None."""
    for fact in facts:
        if fact.get("class") == name and not fact.get("move"):
            if fact.get("kind") in ("next_letter", "current_letter") and _number(fact.get("pct")):
                return fact["pct"]
            if fact.get("kind") == "grade_move" and _number(fact.get("to_pct")):
                return fact["to_pct"]
    return None


def easiest_win(facts, said=None):
    """The class whose story's step up (class_step) needs the lowest score, or None.

    said, when given, is the set of fact ids the published stories cite: a step no story says doesn't count.
    Across the kid's classes a named item's step beats any assumed one, then the lowest needed score wins;
    ties go to the smaller gap, then the class name.
    """
    best = None
    for name in class_names(facts):
        step = class_step(facts_for(facts, name), said)
        letter = next((f for f in facts if f.get("kind") == "next_letter" and f.get("class") == name), None)
        if step is None or letter is None or not _number(letter.get("gap")):
            continue
        key = (not _named(step), _spoken_needed(step), letter["gap"], name)
        best = key if best is None or key < best else best
    return best[-1] if best else None


def biggest_risk(facts, said=None):
    """The class whose story's keep sentence (class_keep) needs the highest score, or None.

    A can't-drop fact needs no score, so a class that keeps its letter even with a zero isn't eligible. said
    works as in easiest_win. Across the kid's classes a named item's keep beats any assumed one, then the
    highest needed score wins; ties go to the smaller cushion above the letter's floor (unknown last), then
    the name.
    """
    floors = dict(insights.SCALE)
    best = None
    for name in class_names(facts):
        fact = class_keep(facts_for(facts, name), said)
        if fact is None:
            continue
        pct = _class_pct(facts, name)
        cushion = pct - floors[fact["letter"]] if pct is not None and fact.get("letter") in floors else math.inf
        key = (not _named(fact), -_spoken_needed(fact), cushion, name)
        best = key if best is None or key < best else best
    return best[-1] if best else None


def story_tags(facts, classes=None, risk_classes=None, said=None):
    """{class name: [tags]} in STORY_TAGS order. One class can carry both.

    classes, when given, limits the race to those classes (write_up passes the ones with a published story),
    and risk_classes limits Biggest risk further (write_up passes the ones whose story carries a keep
    sentence). said is the fact ids those stories cite, so a tag only goes on a story that says its step or
    keep, and the next-best class gets a tag its winner can't carry."""
    def only(names):
        allowed = set(names)
        return [f for f in facts if f.get("class") in allowed]
    win_facts = facts if classes is None else only(classes)
    risk_facts = win_facts if risk_classes is None else only(set(risk_classes) & {f.get("class") for f in win_facts})
    tags = {}
    for tag, name in ((EASIEST_WIN, easiest_win(win_facts, said)), (BIGGEST_RISK, biggest_risk(risk_facts, said))):
        if name is not None:
            tags.setdefault(name, []).append(tag)
    return tags


def write_up(insight_facts, log_items=None, upcoming=None):
    """The whole write-up for one student: {"as_of", "stories"}, each story with its tags (story_tags) if any.

    upcoming (from upcoming_items) lets a what-if name the real next item and its due date. Tags are worked
    out over the classes whose stories survive, so each goes on a published story; Biggest risk only on a
    story that carries its keep sentence. Stories come sorted by class name (story_order), so the same facts
    in any order give the same bytes.
    """
    # One canonical order where the facts enter, so every picker below sees the same list.
    facts = canonical_facts(attach_next_items(merged_facts(insight_facts, log_items), canonical_upcoming(upcoming)))
    # A story over the Worker's limits is dropped alone; the others still publish.
    stories = [s for s in (write_class(facts, name) for name in class_names(facts)) if s and story_fits(s)]
    stories.sort(key=story_order)
    tags = story_tags(facts, [s["class"] for s in stories], [s["class"] for s in stories if s.get("keep_sentence")],
                      {c for s in stories for c in s.get("cites") or []})
    for story in stories:
        if story["class"] in tags:
            story["tags"] = tags[story["class"]]
    return {"as_of": (insight_facts or {}).get("as_of"), "stories": stories}


def story_order(story):
    """Publish order: class name, then the story's own text, cites and tags as a stable tie-break."""
    return (str(story.get("class")), str(story.get("story")),
            json.dumps(story.get("cites"), sort_keys=True), json.dumps(story.get("tags"), sort_keys=True))


# ── Takeover and publishing ──────────────────────────────────────────────────


def apply_write_up(ai_summary, local):
    """The local write-up takes every class's story slot and clears the model's headline.

    A class with no local story gets an empty story rather than the model's. The local write-up has no
    headline, so the model's headline (and the status line the page falls back to) is cleared, never kept.
    """
    summary = dict(ai_summary or {})
    stories = (local or {}).get("stories") or []
    summary["headline"] = ""
    summary.pop("status", None)
    rows = summary.get("classes") or []
    picks = match_stories(stories, [cls.get("class_name") for cls in rows])
    summary["classes"] = [{**cls, "story": pick["story"] if pick else ""} for cls, pick in zip(rows, picks)]
    return summary


def normalize_name(text):
    """index.html normalizeName: lowercase, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def match_stories(stories, class_names):
    """The story for each class name, or None. Names compare normalized (index.html matchLocalStories).

    An exact match wins. Otherwise a class takes a story by containment (either name inside the
    other) only when exactly one story not already taken qualifies for it and that story qualifies
    for no other unmatched class, so "Math" and "AP Math Analysis" each keep their own story, an
    ambiguous name gets none, and a contained story lands on at most one class.
    """
    keyed = [(normalize_name(s.get("class")), s) for s in stories or []]
    keyed = [(name, s) for name, s in keyed if name]
    wants = [normalize_name(n) for n in class_names]
    picks = []
    taken = set()
    for want in wants:
        hit = next((s for name, s in keyed if want and name == want and id(s) not in taken), None)
        picks.append(hit)
        if hit is not None:
            taken.add(id(hit))
    loose = {}
    for i, want in enumerate(wants):
        if picks[i] is None and want:
            loose[i] = [s for name, s in keyed if id(s) not in taken and (want in name or name in want)]
    for i, hits in loose.items():
        if len(hits) != 1:
            continue
        rivals = [j for j, other in loose.items() if j != i and any(s is hits[0] for s in other)]
        if not rivals:
            picks[i] = hits[0]
    return picks


def match_story(stories, class_name):
    """The story for one class name (see match_stories)."""
    return match_stories(stories, [class_name])[0]


def publish_enabled(env=None):
    return (env if env is not None else os.environ).get(PUBLISH_FLAG) == "1"


def publish_write_up(local, put, env=None):
    """Hand the write-up to `put` only when GRADE_INSIGHTS_PUBLISH=1.

    The runner's `put` is InsightsPublisher.put: the AERIES_INSIGHTS_SECRET bearer, never the PIN session.
    """
    if not publish_enabled(env):
        return False
    return put(public_write_up(local)) is not False


def public_write_up(local):
    """The write-ups as published: each story's class, note (note_text), cites and tags; no drop reasons,
    slots or sentences. A story counts only if both its sentences and its note fit the Worker's limits."""
    def one(up):
        up = dict(up or {})
        if "stories" in up:
            stories = [{k: (note_text(s) if k == "story" else s[k]) for k in ("class", "story", "cites", "tags")
                        if k in s} for s in up.get("stories") or [] if story_fits(s)]
            up["stories"] = [s for s in stories if story_fits(s)]
        return up

    return one(local) if isinstance(local, dict) else [one(up) for up in local or []]


# ── Nightly steps for the mini runner ────────────────────────────────────────


class WriterWork:
    """NightWork for the runner: one step per student write-up, then a publish step.

    load() returns [{"insight_facts": ..., "log_items": [...], "upcoming": [...]}] in a stable order;
    "upcoming" (from upcoming_items) is optional. Results are
    saved under store/<night>/ by step, so a repeated step does nothing.
    """

    def __init__(self, load, store, put=None, env=None):
        self.load, self.store = load, Path(store)
        self.put, self.env = put, env

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
            result = write_up(student.get("insight_facts"), student.get("log_items"), student.get("upcoming"))
            if student.get("sn"):
                result["sn"] = str(student["sn"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=1))
        tmp.replace(path)
