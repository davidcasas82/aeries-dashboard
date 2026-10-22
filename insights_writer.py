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
# Story order: the letter, then why it moved, then the pattern, then the one next step.
ORDER = {"next_letter": 0, "scale_break": 0, "grade_move": 1, "posting_day": 2, "what_if": 3, "keep_letter": 3}
VOTES = 3
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
# Spoken fractions the model uses for small gaps: "half a point", "a quarter point".
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
# Prompt wording the models echoed back. A note never talks like this.
_ECHO_RE = re.compile(
    r"distance to the next letter|\bnext letter\b|\bstudents?\b|\bthe letter is\b|\bwhat[- ]if\b|"
    r"\bwhat moved\b|\bof that size\b|\bnext item of\b|\bitem of similar size\b|\bthe next item\b",
    re.I,
)
# "Moved to a C" claims the class changed letter; it needs a move that crossed into that letter.
_LETTER_MOVE_RE = re.compile(
    r"\b(?i:moved|went|slipped|slid|dropped|fell|rose|climbed|jumped|dipped|changed|fallen|risen|is down|is up)"
    r" (?i:up |down |back )?to (?:an? )?([A-F])\b(?!\w)")
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
# Belt and braces behind the cause-sentence gate: words that say which way a cause pushed the grade.
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

# The made-up example: a class, items and numbers that match no real class. It has no posting_day fact, so it
# shows no weekday for the model to copy.
EXAMPLE_FACTS = [
    {"id": "next:ceramics", "kind": "next_letter", "class": "Ceramics", "pct": 68.8, "letter": "D",
     "next_letter": "C", "threshold": 70.0, "gap": 1.2, "say_opening": "Still a D, 1.2 points from a C."},
    {"id": "move:ceramics:2026-09-09", "kind": "grade_move", "class": "Ceramics", "since": "2026-09-09",
     "since_label": "Sep 9", "from_pct": 71.1, "to_pct": 68.8, "delta": -2.3, "explained": -2.2, "coverage": 0.96,
     "coverage_trimmed": False, "say_opening": "Slipped to a D since Sep 9, down 2.3.", "causes": [
         {"id": "cause:ceramics:2026-09-09:glazetest", "title": "Glaze Test", "category": "Projects",
          "bucket": "Projects", "bucket_weight_pct": 40.0, "change": "new", "earned": 11.0, "possible": 20.0,
          "points": -2.6, "share": -2.6, "effect": "hurt"},
         {"id": "cause:ceramics:2026-09-09:claysketch", "title": "Clay Sketch", "category": "Projects",
          "bucket": "Projects", "bucket_weight_pct": 40.0, "change": "new", "earned": 19.0, "possible": 20.0,
          "points": 0.4, "share": 0.4, "effect": "helped"}]},
    {"id": "whatif:ceramics:projects:20", "kind": "what_if", "class": "Ceramics", "bucket": "Projects",
     "bucket_weight_pct": 40.0, "points": 20.0, "target": 70.0, "needed_pct": 73.4, "say_needed": "74 or better",
     "reachable": True, "say_step": "A 74 or better on the {next:whatif:ceramics:projects:20} gets it to a C."},
]
EXAMPLE_NOTE = [
    {"text": "Slipped to a D since Sep 9, down 2.3.", "cites": ["move:ceramics:2026-09-09"]},
    {"text": "The {item:cause:ceramics:2026-09-09:glazetest} did most of the damage.",
     "cites": ["cause:ceramics:2026-09-09:glazetest"]},
    {"text": "The {item:cause:ceramics:2026-09-09:claysketch} helped.", "cites": ["cause:ceramics:2026-09-09:claysketch"]},
    {"text": "A 74 or better on the {next:whatif:ceramics:projects:20} gets it to a C.",
     "cites": ["whatif:ceramics:projects:20"]},
]
DRAFT_SYSTEM = """You write a short note to a parent about one class (the class story), the way a sharp teacher would.
You get FACTS as JSON. Every fact has an id. Use only those facts.

Name assignments and classes ONLY with slots, which code fills in with the real name:
- {item:<cause id>} ONLY for a cause inside a grade_move's causes (it has a title). Code adds its score, like "(6/10)"; don't write the score yourself.
- {next:<what_if id>} or {next:<keep_letter id>} for the coming item a what_if or keep_letter fact is about. Code writes "next test", "next assessment" or the item's title.
- {bucket:<fact id>} for the bucket or category of a cause, what_if or keep_letter fact ("{bucket:<cause id>} scores dragged it."). Code writes the name, like "Homework".
- {class:<fact id>} for the class name. You may also write the class's own name as FACTS gives it; never another class.
A what_if or keep_letter id goes in a {next:} slot, never in an {item:} slot: the next item has no title yet.
  Right: "An 85 or better on the {next:whatif:art:projects:20} gets it to a B."
  Wrong: "An 85 or better on the {item:whatif:art:projects:20} gets it to a B."
Never write an assignment word yourself (test, quiz, exam, lab, project, packet, homework, essay, report, assessment, ...), not even "the next test": use {next:<id>} or {bucket:<id>}. Never write a fact id outside the braces, and nothing but these slots in braces. A sentence that does is thrown away.

Write 1 to 4 short sentences, in this order: letter, cause, pattern, next step.
Every sentence cites at least one fact and is one of these forms, word for word. Any other sentence is thrown away.
- Opening: say_opening, from the grade_move if there is one, else from the next_letter. Cite that fact.
- Cause, citing the cause id (and its grade_move if you like):
  "Mostly the {item:<cause id>}." for the largest cause whose effect matches the move.
  "The {item:<cause id>} did most of the damage." for the largest cause whose effect is "hurt".
  "The {item:<cause id>} hurt." or "The {item:<cause id>} dragged it down." for a cause whose effect is "hurt".
  "The {item:<cause id>} helped." for a cause whose effect is "helped" (against a drop, only as a contrast).
  The same with "{bucket:<cause id>} scores" in place of "The {item:<cause id>}". Two or three causes with the same effect may share one form, joined with "and".
- Weight: "{bucket:<cause id>} is 40% of the grade.", with that bucket's bucket_weight_pct.
- Pattern: only if FACTS has a posting_day fact, its say_pattern, citing it. Never write a weekday any other way.
- Next step: the what_if fact's say_step ("An 85 or better on the {next:<what_if id>} gets it to a B."). With no what_if fact, no next step.
- Keep: if the grade dropped and FACTS has a keep_letter fact, end with its say_keep, citing it: "A 62 or better on the {next:<keep_letter id>} keeps the A." If its say_keep says the next item can't drop the letter, you may end with that or leave it out.
  Right: "The {item:cause:ceramics:2026-09-09:glazetest} hurt."
  Wrong: "Still a D, 1.2 points from a C, and slipping." (cites a fact but isn't its say_opening, word for word)
Never invent advice ("redo the packet", "study", "focus on", "should", "needs to").

Rules:
- Every sentence cites the ids it uses. A cause cites its own cause id. Name causes largest share first; "mostly", "mainly" or "most of" always names the largest-share cause.
- Order: letter, cause, pattern, next step.
- "Because", "mainly", "mostly", "due to" or "driven by" only with a cited grade_move or cause.
- If a grade_move has causes_withheld, say how much it moved but not why.
- With no grade_move, the grade didn't move: say "Still a C", never "moved to a C".
- No talk of the next letter without a next_letter fact. A class at an A has no next letter.
- "Back to" or "again" only when a grade_move shows the class had that letter before.
- Never say what isn't there ("no major assignments", "no pattern").
- Say "since" only with since_label; never "since last report".
- Numbers come from the cited facts, written as digits. A needed score is say_needed ("85 or better"), never rounded down.
- Whole letters only: A, B, C, D, F. Never a plus or minus.
- Don't mention missing, late, past-due, turned-in or awaiting work. The dashboard lists that.
- No dates or relative days ("today", "tomorrow", "this week", "next Friday"), except inside a say_opening.
- A weekday only in a posting_day pattern, citing that fact.
- Never call a 0% a grade.
- Never write "the student", "the letter is", "next letter", "distance", "what-if", "what moved", or "moved from A to A".
- If nothing here is worth saying, return {"sentences": []}.

MADE-UP EXAMPLE. This class doesn't exist; it shows the style only. Never copy its class, titles, numbers, letters or dates.
FACTS: """ + json.dumps(EXAMPLE_FACTS, separators=(",", ":")) + """
NOTE: """ + json.dumps({"sentences": EXAMPLE_NOTE}, separators=(",", ":")) + """

Reply with JSON only: {"sentences": [{"text": "...", "cites": ["fact id", ...]}]}"""

VOTE_SYSTEM = """You check sentences against FACTS (JSON, each with an id).
A sentence is backed only if every claim in it follows from the facts it cites.
Reply with JSON only: {"backed": [true or false, one per sentence, in order]}"""

HEADLINE_SYSTEM = """You write the one-line headline of a parent's grade briefing from the class STORIES (JSON).
Pick the one class whose story ends on a next step, and write one of these forms, word for word, from that story:
- its next step as the story says it: "An 85 or better on the {next:<what_if id>} gets it to a B." Cite the what_if.
- led by the class: "{class:<what_if id>}: an 85 or better on the {next:<what_if id>} gets it to a B." Cite the what_if.
- after the gap from its opening: "{class:<what_if id>} is 0.5 pts from a B; an 85 or better on the {next:<what_if id>} gets it to a B." Cite the next_letter and the what_if.
Copy the slots as the story has them. Any other headline is thrown away.
  Right: "{class:whatif:art:projects:20}: an 85 or better on the {next:whatif:art:projects:20} gets it to a B."
  Wrong: "Art needs the next project to go well." (not one of the forms)
If no story ends on a next step, reply {"text": "", "cites": []}.
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


def model_facts(class_facts):
    """What the model sees: a move whose causes don't explain it is sent without them, and of the
    class's keep_letter fact only on a drop story (pick_keep), with its say_keep wording."""
    out = []
    keep = pick_keep(class_facts)
    for fact in class_facts:
        if is_keep(fact):
            if keep is None or fact["id"] != keep["id"]:
                continue
            spoken = say_keep(fact)
            fact = {**fact, "say_keep": spoken} if spoken else fact
        # A scale break or a no-raise-math gate has no sentence of its own (form_reason): the model never sees it.
        if fact.get("kind") in UNSAYABLE_KINDS:
            continue
        if fact.get("kind") == "grade_move" and fact.get("coverage_trimmed"):
            fact = {**fact, "causes": [], "causes_withheld": "the earlier scores were trimmed, so the cause is unknown"}
        elif fact.get("kind") == "grade_move" and fact.get("causes"):
            if cause_allowed(fact):
                fact = {**fact, "causes": ranked_causes(fact)}
            else:
                fact = {**fact, "causes": [], "causes_withheld": "they don't explain the move"}
        out.append(_say_fields(fact))
    return out


def say_needed(needed_pct):
    """How a note says a needed score: the next whole number up, "or better". 84.2 -> "85 or better"."""
    return f"{math.ceil(round(needed_pct, 6))} or better"


def _article(word):
    return fix_articles(f"a {word}").split(" ")[0]


def say_opening(fact):
    """The story's first sentence, built from one next_letter or grade_move fact, or None.

    next_letter: "Still a C, 0.5 points from a B."  grade_move: "Still an A, down 1.6 since Oct 14."
    or, when the move crossed a letter, "Slipped to a C since Oct 14, down 1.6."
    """
    kind = fact.get("kind")
    if kind == "next_letter" and fact.get("letter") and fact.get("next_letter") and isinstance(fact.get("gap"), (int, float)):
        letter, nxt = fact["letter"], fact["next_letter"]
        return f"Still {_article(letter)} {letter}, {fact['gap']:g} points from {_article(nxt)} {nxt}."
    if kind == "grade_move" and all(isinstance(fact.get(k), (int, float)) for k in ("from_pct", "to_pct", "delta")):
        before, after = insights.letter_for(fact["from_pct"]), insights.letter_for(fact["to_pct"])
        way = "down" if fact["delta"] < 0 else "up"
        since = f" since {fact['since_label']}" if fact.get("since_label") else ""
        if before == after:
            return f"Still {_article(after)} {after}, {way} {abs(fact['delta']):g}{since}."
        verb = "Slipped" if fact["delta"] < 0 else "Up"
        return f"{verb} to {_article(after)} {after}{since}, {way} {abs(fact['delta']):g}."
    return None


def _say_fields(fact):
    """Ready-to-say wording the model copies instead of rounding: say_needed, since_label,
    say_opening, and each cause's effect ("hurt" or "helped") on the grade."""
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
    if fact.get("kind") == "grade_move" and fact.get("causes"):
        def effect(cause):
            size = cause.get("share") if cause.get("share") is not None else cause.get("points")
            return "hurt" if (size or 0) < 0 else "helped"
        fact = {**fact, "causes": [{**c, "effect": effect(c), "slot": f"{{item:{c['id']}}}"} for c in fact["causes"]]}
    opening = say_opening(fact)
    if opening:
        fact = {**fact, "say_opening": opening}
    pattern = say_pattern(fact)
    if pattern:
        fact = {**fact, "say_pattern": pattern}
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
    item with that class, bucket and points possible. Any other what-if stays "the next test".
    """
    out = []
    for fact in facts or []:
        if fact.get("kind") in ("what_if", "keep_letter"):
            match = next((i for i in upcoming or [] if i["class"] == fact.get("class")
                          and i["bucket"] == fact.get("bucket") and i["points"] == fact.get("points")), None)
            if match:
                fact = {**fact, "next_title": match["title"], "next_due": match["due"],
                        "next_due_label": due_label(match["due"])}
        out.append(fact)
    return out


def is_keep(fact):
    """The engine's keep-the-letter fact (kind keep_letter), never a next-letter what-if."""
    return fact.get("kind") == "keep_letter"


def pick_keep(class_facts):
    """The class's keep_letter fact when its grade dropped, else None.

    The engine already picks one per class (a needed score beats can't-drop, then the soonest named
    next item, the drop's biggest cause, the heaviest bucket), so the writer only takes it.
    """
    facts = [f for f in class_facts if not f.get("move")]
    dropped = any(f.get("kind") == "grade_move" and isinstance(f.get("delta"), (int, float)) and f["delta"] < 0
                  for f in facts)
    keep = next((f for f in facts if is_keep(f)), None)
    if not dropped or keep is None:
        return None
    if not isinstance(keep.get("needed_pct"), (int, float)) and not _named_cannot_lose(keep):
        return None
    return keep


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
    item's title or "next <bucket>" after the checks."""
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


def worth_saying(class_facts):
    """No filler: a story needs an explained move, a reachable what-if or a pattern. A keep_letter fact alone isn't one."""
    for fact in class_facts:
        if is_keep(fact):
            continue
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


def check_sentence(text, cites, index, name=None, classes=None, headline=False):
    """None if the sentence is backed by the facts it cites, else the reason it's dropped.

    classes is the student's class names (write_class passes them); the index's own classes are added.
    A sentence that cites a fact must be one of the fixed forms for the kinds it cites (headline: the headline
    forms). A sentence that cites nothing drops.
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
    # letters or "B+" never trip them; what the model wrote outside the slots is checked as usual.
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
    reason = form_reason(text, cites, index, headline=headline)
    if reason:
        return reason
    # Whatever the fill produces must not carry a raw id or a brace either.
    return leak_reason(fill_slots(text, index), index, filled=True)


# ── Slots ─────────────────────────────────────────────────────────────────────
# The model names items, buckets and classes only through slots, which code fills after the checks:
#   {item:<cause id>}     the cause's title, plus "(earned/possible)" unless the model wrote a "(...)" after it
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
    """The letter each cited class holds now, from its next_letter fact or its latest move."""
    classes = {f.get("class") for f in cited}
    out = set()
    for fact in index.values():
        if fact.get("class") not in classes or fact.get("move"):
            continue
        if fact.get("kind") == "next_letter" and fact.get("letter"):
            out.add(fact["letter"])
        elif fact.get("kind") == "grade_move" and isinstance(fact.get("to_pct"), (int, float)):
            out.add(insights.letter_for(fact["to_pct"]))
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
        # The id and slot-debris filters ran on the model's raw text (below). A fill only adds names from
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
            return _clean(fact["next_title"]) if fact.get("next_title") else f"next {_bucket_noun(_clean(fact.get('bucket')))}"
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
        pct = f.get("pct") if f.get("kind") == "next_letter" else f.get("to_pct")
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
    word around. This backs up the cause-sentence gate; it is not the main check.
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
    if _ADVICE_RE.search(_strip_names(masked, cited)) and not any(f.get("kind") in ("what_if", "keep_letter")
                                                                     for f in cited):
        return "gives advice no what_if backs"
    if _NOTHING_RE.search(_strip_names(masked, cited)):
        return "says what isn't there"
    move_ids = [f["move"] if f.get("move") else f["id"] for f in cited if f.get("kind") == "grade_move"]
    moves = [index.get(m) or {} for m in dict.fromkeys(move_ids)]
    # A bare "mainly"/"mostly" is cause wording, except in a posting pattern ("scores mostly post Fridays").
    pattern_only = any(f.get("kind") == "posting_day" for f in cited) and not _GRADE_WORDS_RE.search(masked)
    cause_words = _CAUSE_CLAIM_RE.search(masked) or (_BARE_CAUSE_RE.search(masked) and not pattern_only)
    if cause_words and not moves:
        return "states a cause without citing a grade_move or cause"
    if _MOVE_REF_RE.search(masked) and not moves:
        return "talks about a move with no grade_move cited"
    # The exact contrast forms ("The {item:...} helped." on a drop, "... hurt." on a rise) are judged by the
    # cause-sentence gate on the cause's own share, never as the reason for the move.
    # The exact contrast forms, and a bucket's weight (no direction at all), may name an offsetting cause.
    form = (slotted or "").strip()
    contrast_form = bool(form and (_FORM_CONTRAST_RE.match(form) or _FORM_WEIGHT_RE.match(form)))
    # "Mostly {bucket:...} scores." ranks buckets by net share; the cause-sentence gate checks it.
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
        if keeps:
            return "a keep_letter fact never backs a gets-it-to claim"
        if not any(f.get("kind") in ("next_letter", "what_if") for f in cited):
            return "claims a next letter with no next_letter or what_if fact"
        if _class_at_a(index, cited):
            return "claims a next letter for a class at an A"
    if _BACK_RE.search(bare) and said:
        held = {insights.letter_for(m["from_pct"]) for m in _class_moves(index, cited)
                if insights.letter_for(m["from_pct"]) != insights.letter_for(m.get("to_pct", m["from_pct"]))}
        if not set(said) & held:
            return "says back to or again with a letter no move shows the class held"
    for match in _LETTER_MOVE_RE.finditer(bare):
        crossed = {insights.letter_for(m["to_pct"]) for m in moves
                   if isinstance(m.get("to_pct"), (int, float)) and isinstance(m.get("from_pct"), (int, float))
                   and insights.letter_for(m["from_pct"]) != insights.letter_for(m["to_pct"])}
        if match.group(1) not in crossed:
            return f"says the class moved to {match.group(1)} but no move crossed into that letter"
    for match in _NUMBER_RE.finditer(bare):
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
        if pattern.search(bare) and not any((m.get("delta") or 0) * sign > 0 for m in moves):
            return "says the grade moved a way no cited move did"
    return _direction_reason(slotted if slotted is not None else text, cited, index)


# ── Cause-sentence gate ──────────────────────────────────────────────────────
# A sentence that names a cause ({item:} or {bucket:} on a cause), cites a cause, or cites a grade_move
# with direction wording must be one of these forms, or it is dropped. This is the main direction check;
# _direction_reason's word lists only back it up.
#   "The {item:<cause>} helped."                 share > 0
#   "The {item:<cause>} hurt." / "... dragged it down."   share < 0
#   "The {item:<cause>} did most of the damage." share < 0 and the move's largest negative share
#   "Mostly the {item:<cause>}." / "Mostly from the {item:<cause>}."  the largest share in the move's direction
#   the same with "{bucket:<cause>} scores" ("Mostly one {bucket:<cause>} score." too)
#   helped / hurt / dragged it down may name 2 or 3 causes joined with "and", all of one sign
#   the cited move's say_opening, word for word ("{class:<id>} slipped to a D since Oct 14, down 2.5." too)
# The item form may carry " score" and the "(n/m)" score the fill adds.
_FORM_ITEM = r"(?i:the) \{item:([^{}\s]+)\}(?: score)?(?: \(\d+(?:\.\d+)?/\d+(?:\.\d+)?\))?"
_FORM_BUCKET = r"\{bucket:([^{}\s]+)\}(?: scores?)?"
_FORM_ONE = rf"(?:{_FORM_ITEM}|{_FORM_BUCKET})"
_FORM_SLOT_RE = re.compile(r"\{(item|bucket):([^{}\s]+)\}")
_FORM_LIST = rf"{_FORM_ONE}(?:(?:, {_FORM_ONE})?,? and {_FORM_ONE})?"
_FORM_SIGNED_RE = re.compile(rf"^(?P<who>{_FORM_LIST}) (?P<verb>helped|hurt|dragged it(?: down)?)\.$")
_FORM_DAMAGE_RE = re.compile(rf"^(?P<who>{_FORM_ONE}) did most of the damage\.$")
_FORM_MOSTLY_RE = re.compile(
    rf"^Mostly (?:from )?(?P<who>{_FORM_ITEM}|(?:one )?\{{bucket:[^{{}}\s]+\}}(?: scores?)?)\.$")
_FORM_CONTRAST_RE = re.compile(rf"^(?P<who>{_FORM_LIST}) (?:helped|hurt)\.$")
# A bucket's weight with no direction in it: "{bucket:<cause>} is 50% of the grade."
_FORM_WEIGHT_RE = re.compile(r"^\{bucket:[^{}\s]+\}(?: scores)? (?:is|are|counts? for) \d+(?:\.\d+)?% of the grade\.$")
_DIRECTION_WORDS_RE = re.compile(
    rf"\b(?:{_UP_WORD}|{_DOWN_WORD}|rose|rise[sn]?|fell|fall(?:s|en)?|drop\w*|dip\w*|slip\w*|slid\w*|climb\w*|"
    r"jump\w*|gain\w*|"
    r"lost|los(?:e|es|ing)|moved?|moving|went|cost\w*|pull\w*|push\w*|drag\w*|hurt\w*|help\w*|lift\w*|boost\w*|"
    r"rais\w*|sav\w*|damag\w*|because|due|thanks|driv\w*|caus\w*|mostly|mainly|largely|primarily|chiefly|"
    r"higher|lower|better|worse|back|again)\b", re.I)
# Direction wording that sends any sentence with an {item:} or {bucket:} slot through the gate, whatever fact
# the slot names. "Better", "back" and "again" are left out: "A 74 or better on the next {bucket:...} score".
_SLOT_DIRECTION_RE = re.compile(
    rf"\b(?:{_UP_WORD}|{_DOWN_WORD}|rose|rise[sn]?|risen|fell|fall(?:s|en)?|drop\w*|dip\w*|slip\w*|slid\w*|"
    r"climb\w*|jump\w*|"
    r"gain\w*|lost|los(?:e|es|ing)|moved|cost\w*|pull\w*|push\w*|drag\w*|hurt\w*|help\w*|lift\w*|boost\w*|"
    r"rais\w*|sav\w*|damag\w*|because|due to|thanks|driv\w*|caus\w*|mostly|mainly|largely|primarily|chiefly|"
    r"carr\w*|rescu\w*|improv\w*|tank\w*|cut\w*|dent\w*|slash\w*|blam\w*|sank|sunk)\b", re.I)
CAUSE_FORM_REASON = "cited sentence not in an allowed form"
# A bucket whose causes net to less than this, either way, has no direction to claim.
BUCKET_MIN_NET = 0.1


# Two shares within this of each other tie: neither is "most of" the move.
TOP_TIE = 1e-6


def _top_cause(move, sign):
    """The move's cause with the largest share of the given sign, or None (also on a tie for the top)."""
    # Safe under #54's fold: it sorts causes by |share| before folding, so no folded cause outranks the top one.
    signed = [c for c in ranked_causes(move) if (_cause_share(c) or 0) * sign > 0]
    if len(signed) > 1 and abs(_cause_share(signed[0]) - _cause_share(signed[1])) <= TOP_TIE:
        return None
    return signed[0] if signed else None


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


def _top_bucket(move, sign):
    """The move's bucket with the largest net in the given direction (|net| >= BUCKET_MIN_NET), or None.

    None too on a tie for the top, or when the move has folded causes (no bucket net is known).
    """
    if _folded(move):
        return None
    nets = sorted((abs(v), b) for b, v in _bucket_nets(move).items() if v * sign >= BUCKET_MIN_NET - 1e-9)
    if len(nets) > 1 and nets[-1][0] - nets[-2][0] <= TOP_TIE:
        return None
    return nets[-1][1] if nets else None


def _opening_forms(move):
    opening = say_opening(_say_fields(move)) if move else None
    if not opening:
        return set()
    lower = opening[0].lower() + opening[1:]
    return {opening, fix_articles(opening), f"{{class:{move['id']}}} {lower}"}


def cause_form_reason(text, cites, index):
    """None if the sentence is no cause sentence or is one of the fixed forms, else CAUSE_FORM_REASON.

    A cause sentence slots a cause, cites a cause, cites a grade_move with direction wording, or has any
    {item:} or {bucket:} slot plus a direction word. In the forms every slot names a cause; an item slot is
    read by its own share, a bucket slot by its bucket's net share in the move (|net| < 0.1 has no direction).
    """
    text = (text or "").strip()
    cited = _cited_facts(cites, index)
    slots = _FORM_SLOT_RE.findall(text)
    cause_slots = [(kind, fid) for kind, fid in slots if (index.get(fid) or {}).get("move")]
    cited_causes = [f for f in cited if f.get("move")]
    moves = [f for f in cited if f.get("kind") == "grade_move" and not f.get("move")]
    bare = SLOT_RE.sub(" ", text)
    worded = bool(_DIRECTION_WORDS_RE.search(bare))
    slot_worded = bool(slots) and bool(_SLOT_DIRECTION_RE.search(bare))
    if not cause_slots and not cited_causes and not (moves and worded) and not slot_worded:
        return None
    if not slots and not cited_causes:
        # Only the move itself: its say_opening, word for word.
        return None if any(text in _opening_forms(m) for m in moves) else CAUSE_FORM_REASON
    # Every slot names a cause, every cited cause is slotted, and nothing else is cited but the move.
    named = list(dict.fromkeys(fid for _, fid in cause_slots))
    if len(cause_slots) != len(slots) or set(named) != {f["id"] for f in cited_causes} or \
            len(cited_causes) + len(moves) != len(cited):
        return CAUSE_FORM_REASON
    if _FORM_WEIGHT_RE.match(text) and len(slots) == 1:
        return None
    # One reading per slot: (move id, the item or bucket it names) and its signed value.
    readings = []
    for kind, fid in cause_slots:
        cause = index[fid]
        if kind == "bucket":
            readings.append(((cause.get("move"), "bucket", _bucket_of(cause)), _bucket_net(index, cause)))
        else:
            readings.append(((cause.get("move"), "item", fid), _cause_share(cause) or 0))
    if len({key for key, _ in readings}) != len(readings):
        return CAUSE_FORM_REASON
    match = _FORM_SIGNED_RE.match(text)
    if match and len(_FORM_SLOT_RE.findall(match.group("who"))) == len(slots):
        sign = 1 if match.group("verb") == "helped" else -1
        return None if all(v * sign > 0 for _, v in readings) else CAUSE_FORM_REASON
    if len(readings) != 1:
        return CAUSE_FORM_REASON
    (move_id, kind, what), value = readings[0]
    move = index.get(move_id) or {}
    if _FORM_DAMAGE_RE.match(text):
        top = _top_cause(move, -1)["id"] if kind == "item" and _top_cause(move, -1) else _top_bucket(move, -1)
        return None if value < 0 and top == what else CAUSE_FORM_REASON
    if _FORM_MOSTLY_RE.match(text):
        sign = 1 if (move.get("delta") or 0) >= 0 else -1
        if kind == "item":
            top = _top_cause(move, sign)
            top = top["id"] if top else None
        else:
            top = _top_bucket(move, sign)
        return None if value * sign > 0 and top == what else CAUSE_FORM_REASON
    return CAUSE_FORM_REASON


FORM_REASON = CAUSE_FORM_REASON  # one reason for every fixed-form gate
UNCITED_REASON = "cites nothing"  # every sentence cites at least one fact
# Kinds no sentence may cite: the model never sees them (model_facts leaves them out).
UNSAYABLE_KINDS = ("scale_break", "no_raise_math")


def say_pattern(fact):
    """A posting_day fact's one sentence: "Scores usually post Thursdays." """
    day = fact.get("weekday")
    return f"Scores usually post {day}s." if fact.get("kind") == "posting_day" and day else None


def _prefixed(sentence, fact):
    """The sentence, and the same led by the fact's {class:} slot ("{class:<id>} scores usually post ...")."""
    return {sentence, f"{{class:{fact['id']}}} {sentence[0].lower()}{sentence[1:]}"}


def _step(fact):
    return _say_fields(fact).get("say_step") if fact.get("kind") == "what_if" else None


def _lower_first(sentence):
    return sentence[0].lower() + sentence[1:]


def _headline_forms(cited):
    """The headline forms for the cited facts: a what_if step alone, led by its class, or after the gap.
    Each may cite the step's class's next_letter or not (the gap form needs it). A headline cites one what_if and
    at most that class's next_letter; anything else (a second what_if, another class's letter) has no form.

    "An 88 or better on the {next:<id>} gets it to an A."
    "{class:<id>}: an 88 or better on the {next:<id>} gets it to an A."
    "{class:<id>} is 2.1 pts from an A; an 88 or better on the {next:<id>} gets it to an A."
    The class may be the {class:} slot of a cited fact or the class's own name.
    """
    steps = [f for f in cited if f.get("kind") == "what_if" and _step(f)]
    letters = [f for f in cited if f.get("kind") == "next_letter"]
    # Fail closed: one what_if, at most one next_letter, both the same class. So the {class:} lead, the gap and
    # the step can only ever be one class's.
    if len(steps) != 1 or len(steps) + len(letters) != len(cited) or len(letters) > 1:
        return set()
    forms = set()
    for step_fact in steps:
        step = _step(step_fact)
        if any(letter.get("class") != step_fact.get("class") for letter in letters):
            continue
        leads = {step_fact["class"]} | {f"{{class:{f['id']}}}" for f in cited if f.get("class") == step_fact.get("class")}
        # As in a story, the step may cite its class's next_letter or not.
        forms.add(step)
        forms |= {f"{lead}: {_lower_first(step)}" for lead in leads}
        for letter in letters:
            if not isinstance(letter.get("gap"), (int, float)):
                continue
            gap = f"{letter['gap']:g} pts from {_article(letter['next_letter'])} {letter['next_letter']}"
            forms |= {f"{lead} is {gap}; {_lower_first(step)}" for lead in leads}
    return forms


def form_reason(text, cites, index, headline=False):
    """None if a sentence that cites facts is one of the fixed forms for the set of kinds it cites.

      grade_move                 its say_opening ({class:} prefix allowed)
      next_letter                its say_opening ({class:} prefix allowed)
      cause, with or without its grade_move   the cause forms (cause_form_reason), the weight sentence among them
      what_if, with or without the next_letter   its say_step, word for word
      what_if alone              or the weight sentence on its bucket: "{bucket:<what_if id>} is 50% of the grade."
      posting_day                say_pattern ({class:} prefix allowed): "Scores usually post Thursdays."
      keep_letter                its say_keep, word for word (keep-a)
      scale_break, no_raise_math, any other kind or mix: no sentence.
    A headline is one of the _headline_forms instead.
    """
    text = (text or "").strip()
    cited = _cited_facts(cites, index)
    if not cited:
        return None
    if headline:
        return None if text in _headline_forms(cited) else FORM_REASON
    kinds = {"cause" if f.get("move") else f.get("kind") for f in cited}
    if "cause" in kinds:
        ok = kinds <= {"cause", "grade_move"} and cause_form_reason(text, cites, index) is None
    elif kinds == {"grade_move"}:
        ok = any(text in _opening_forms(f) for f in cited)
    elif kinds == {"next_letter"}:
        ok = any(text in _opening_forms(f) for f in cited)
    elif kinds in ({"what_if"}, {"what_if", "next_letter"}):
        ok = any(text == _step(f) for f in cited if f.get("kind") == "what_if")
        if not ok and kinds == {"what_if"} and len(cited) == 1 and _FORM_WEIGHT_RE.match(text):
            ok = _FORM_SLOT_RE.findall(text) == [("bucket", cited[0]["id"])]
    elif kinds == {"posting_day"}:
        ok = any(say_pattern(f) and text in _prefixed(say_pattern(f), f) for f in cited)
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

    Fails a story that cites no grade_move, what_if or pattern fact (a bare next_letter is
    not an insight), that restates past-due or missing work, or that's out of order
    (letter, cause, pattern, next step). The first sentence must state the letter with the
    gap or the move, backed by a cited next_letter or grade_move. A story about a drop must
    end on the class's what_if step when the class has one.
    """
    reason = _story_basics(sentences, index)
    if reason:
        return reason
    if not _opens_with_letter(sentences[0], index):
        return "doesn't open with the letter and the gap or move"
    drops = [f for f in index.values() if f.get("kind") == "grade_move" and not f.get("move")
             and isinstance(f.get("delta"), (int, float)) and f["delta"] < 0]
    steps = {fid for fid, f in index.items() if f.get("kind") == "what_if" and f.get("reachable")
             and isinstance(f.get("needed_pct"), (int, float))}
    keep = pick_keep(list(index.values()))
    if keep and isinstance(keep.get("needed_pct"), (int, float)):
        steps.add(keep["id"])
    if drops and steps and not set(sentences[-1].get("cites") or []) & steps:
        return "a drop story must end on its what-if step"
    return None


def _story_basics(sentences, index):
    if not sentences:
        return "no story"
    kinds = []
    for sentence in sentences:
        masked, _, _ = _mask_allowed_dates(sentence.get("text") or "", _cited_facts(sentence.get("cites"), index))
        if _RESTATE_RE.search(masked):
            return "restates past-due or missing work"
        ranks = [ORDER.get((index.get(fid) or {}).get("kind"), 9) for fid in sentence.get("cites") or []]
        kinds.extend((index.get(fid) or {}).get("kind") for fid in sentence.get("cites") or [])
        # A sentence sits where its latest kind goes: a what-if that also cites the move is a next step.
        sentence["_rank"] = max(ranks) if ranks else 9
    order = [s.pop("_rank") for s in sentences]
    if not any(kind in INSIGHT_KINDS for kind in kinds):
        return "cites no grade_move, what_if or pattern fact"
    if order != sorted(order):
        return "out of order (letter, cause, pattern, next step)"
    return None


def _opens_with_letter(sentence, index):
    """The first sentence states a letter and a gap or move from a cited next_letter or grade_move."""
    cited = [index[fid] for fid in sentence.get("cites") or [] if fid in index]
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


def _headline_needed_reason(text, cites, index):
    """A needed score in a headline is the exact round-up plus "or better" ("85 or better"), or it's dropped."""
    cited = [index[fid] for fid in cites if fid in index]
    needs = [f["needed_pct"] for f in cited if isinstance(f.get("needed_pct"), (int, float))]
    if not needs:
        return None
    bare = SLOT_RE.sub(" ", spelled_to_digits(text))
    for match in _NUMBER_RE.finditer(bare):
        n = float(match.group(0))
        near = [v for v in needs if abs(n - v) < 0.051 or v <= n < v + 1]
        if not near:
            continue
        if not any(n == math.ceil(round(v, 6)) for v in near):
            return f"headline needed score {match.group(0)} isn't the round-up"
        if not re.match(r"\s*(?:%|percent)?\s*or better\b", bare[match.end():], re.I):
            return f"headline needed score {match.group(0)} isn't said as \"or better\""
    return None


def _concrete_headline(text, cites, index):
    """A headline names a number, a letter or a cited item; class names don't count."""
    cited = [index[fid] for fid in cites if fid in index]
    titles = [str(f.get(k)) for f in cited for k in ("title", "next_title") if f.get(k)]
    if any(t.lower() in text.lower() or re.sub(r"\s*\([^)]*\)\s*$", "", t).lower() in text.lower() for t in titles):
        return True
    masked, _, _ = _mask_allowed_dates(spelled_to_digits(text), cited)
    bare = _strip_names(masked, cited)
    return bool(_NUMBER_RE.search(bare) or _said_letters(bare))


# ── Model steps ──────────────────────────────────────────────────────────────


_THINK_RE = re.compile(r"<think>.*?</think>|^.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


def _json(raw):
    """A JSON object from a model reply, after dropping <think> blocks and a ```json fence."""
    if not isinstance(raw, str):
        return None
    # A reply that is already clean JSON keeps every string as is, even one holding "<think>".
    try:
        value = json.loads(_FENCE_RE.sub("", raw.strip()).strip())
        if isinstance(value, dict):
            return value
    except (TypeError, ValueError):
        pass
    text = _THINK_RE.sub("", raw)
    # An unclosed <think> runs to the end: nothing after it is the answer.
    text = re.split(r"<think>", text, maxsplit=1, flags=re.I)[0]
    text = _FENCE_RE.sub("", text.strip()).strip()
    for candidate in (text, text[text.find("{"): text.rfind("}") + 1] if "{" in text else ""):
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    # The one leniency: extra closing braces at the very end ('...]}}'). Strip the excess, retry once.
    excess = text.count("}") - text.count("{")
    if excess > 0 and text.endswith("}" * excess):
        try:
            value = json.loads(text[:-excess])
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None
    return None


def _unique(items):
    """Items with repeats removed, first occurrence kept, order kept."""
    seen = set()
    return [x for x in items if not (x in seen or seen.add(x))]


# family-data src/insights.js cleanInsights limits (all inclusive, lengths in UTF-16 code units like JS):
# text <= 600, cites 1..12 and each cite 1..120, class <= 120. A story over any limit, or with no cites, is dropped alone,
# so the rest of the publish still goes through; a headline over a limit is dropped the same way.
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
    """True when the Worker would accept this story or headline line as is."""
    if not _fits_text(text, PUBLISH_MAX_TEXT):
        return False
    if class_name is not None and not _fits_text(class_name, PUBLISH_MAX_CLASS):
        return False
    cites = cites or []
    return 0 < len(cites) <= PUBLISH_MAX_CITES and all(_fits_text(c, PUBLISH_MAX_CITE) for c in cites)


def story_fits(story):
    return within_publish_limits((story or {}).get("story"), (story or {}).get("cites"), (story or {}).get("class"))


def headline_fits(headline):
    return within_publish_limits((headline or {}).get("text"), (headline or {}).get("cites"))


def _sentences(raw):
    parsed = _json(raw) or {}
    out = []
    for item in parsed.get("sentences") or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            cites = _unique([c for c in item.get("cites") or [] if isinstance(c, str)])
            out.append({"text": fix_articles(item["text"].strip()), "cites": cites})
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


_CODE_LETTER_RE = re.compile(r"(?<![\w'’])[A-F](?:\+|-|−|–)?(?![\w'’])")
_CODE_DAY_RE = re.compile(r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)days?\b", re.I)


def reason_code(reason):
    """A drop reason without its numbers, ids, quotes, letters, weekdays or words: safe to log or store
    ("number # isn't in the cited facts", "letter <letter> isn't in the cited facts")."""
    code = re.sub(r"\{[^}]*\}|\([^)]*\)|\"[^\"]*\"", "", str(reason or ""))
    code = re.sub(r"\b[\w.-]*:[\w:.-]+", "<id>", code)
    code = re.sub(r"\d+(?:[./]\d+)?", "#", code)
    code = _CODE_LETTER_RE.sub("<letter>", code)
    code = _CODE_DAY_RE.sub("<day>", code)
    return re.sub(r"\s+", " ", code).strip()


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
        reason = check_sentence(sentence["text"], sentence["cites"], index, name, class_names(facts))
        if reason:
            dropped.append({**sentence, "reason": reason})
        else:
            kept.append({**sentence, "slots": sentence["text"], "text": fix_articles(fill_slots(sentence["text"], index))})
    voted = vote(model, kept, class_facts, votes)
    dropped += [{**s, "reason": "vote"} for s in kept if s not in voted]
    if eval_story([dict(s) for s in voted], index):
        return None
    return {
        "class": name,
        "story": " ".join(s["text"] for s in voted),
        "slotted": " ".join(s.get("slots", s["text"]) for s in voted),
        "cites": _unique([c for s in voted for c in s["cites"]]),
        # Reason codes only: the full reasons quote grade numbers, so they stay out of stores and logs.
        "dropped": [reason_code(d["reason"]) for d in dropped],
    }


def write_headline(model, facts, stories):
    """One cited line for the Briefing masthead, or None."""
    if not stories:
        return None
    index = fact_index(facts)
    user = json.dumps([{"class": s["class"], "story": s.get("slotted", s["story"]), "cites": s["cites"]} for s in stories],
                      indent=1)
    parsed = _json(model.complete(HEADLINE_SYSTEM, user)) or {}
    text = fix_articles((parsed.get("text") or "").strip()) if isinstance(parsed.get("text"), str) else ""
    cites = _unique([c for c in parsed.get("cites") or [] if isinstance(c, str)])
    used = {c for s in stories for c in s["cites"]}
    # A headline cites a fact from the stories and says something concrete, or there is none.
    if not text or not cites or not set(cites) <= used or check_sentence(text, cites, index, headline=True):
        return None
    if _headline_needed_reason(text, cites, index):
        return None
    text = fix_articles(fill_slots(text, index))
    if _story_basics([{"text": text, "cites": cites}], index) or not _concrete_headline(text, cites, index):
        return None
    headline = {"text": text, "cites": cites}
    return headline if headline_fits(headline) else None


def write_up(model, insight_facts, log_items=None, votes=VOTES, upcoming=None):
    """The whole write-up for one student: {"as_of", "headline", "stories"}.

    upcoming (from upcoming_items) lets a what-if name the real next item and its due date.
    """
    facts = attach_next_items(merged_facts(insight_facts, log_items), upcoming)
    # A story over the Worker's limits is dropped alone; the others still publish.
    stories = [s for s in (write_class(model, facts, name, votes) for name in class_names(facts)) if s and story_fits(s)]
    return {
        "as_of": (insight_facts or {}).get("as_of"),
        "headline": write_headline(model, facts, stories),
        "stories": stories,
    }


# ── Takeover and publishing ──────────────────────────────────────────────────


def apply_write_up(ai_summary, local):
    """The local write-up takes the headline and every class's story slot.

    A class with no local story gets an empty story rather than the model's. With no local
    headline the masthead shows none: the model's headline (and the status line the page
    falls back to) is cleared, never kept.
    """
    summary = dict(ai_summary or {})
    stories = (local or {}).get("stories") or []
    headline = (local or {}).get("headline")
    summary["headline"] = headline["text"] if headline else ""
    if not headline:
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
    """The write-ups as published: headline and each story's class, text and cites; no drop reasons or slots."""
    def one(up):
        up = dict(up or {})
        if "stories" in up:
            up["stories"] = [{k: s[k] for k in ("class", "story", "cites") if k in s}
                             for s in up.get("stories") or [] if story_fits(s)]
        if up.get("headline") and not headline_fits(up["headline"]):
            up["headline"] = None
        return up

    return one(local) if isinstance(local, dict) else [one(up) for up in local or []]


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
            if student.get("sn"):
                result["sn"] = str(student["sn"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=1))
        tmp.replace(path)
