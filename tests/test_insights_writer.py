"""The local write-up: synthetic facts only, and a scripted model in place of a real one."""

import json
import random
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import insights  # noqa: E402
import insights_writer as w  # noqa: E402

MOVE = "move:math:2026-10-15"
CAUSE = "cause:math:2026-10-15:ch4quiz"
NEXT = "next:math"
WHATIF = "whatif:math:tests:50"
PATTERN = "pattern:posting_day:math"


def math_facts(explained=-2.4):
    coverage = None if explained is None else round(explained / -2.5, 2)
    return [
        {"id": NEXT, "kind": "next_letter", "class": "Math", "pct": 87.9, "letter": "B",
         "next_letter": "A", "threshold": 90.0, "gap": 2.1},
        {"id": MOVE, "kind": "grade_move", "class": "Math", "since": "2026-10-15", "from_pct": 90.4,
         "to_pct": 87.9, "delta": -2.5, "explained": explained, "coverage": coverage,
         "coverage_trimmed": False, "causes": [
             {"id": CAUSE, "title": "Ch.4 Quiz", "category": "Tests", "bucket": "Tests",
              "bucket_weight_pct": 60.0, "change": "new", "earned": 31.0, "possible": 50.0,
              "points": -2.4, "share": -2.4},
         ]},
        {"id": WHATIF, "kind": "what_if", "class": "Math", "bucket": "Tests", "bucket_weight_pct": 60.0,
         "points": 50.0, "target": 90.0, "needed_pct": 88.0, "reachable": True},
        {"id": PATTERN, "kind": "posting_day", "class": "Math", "weekday": "Friday", "share": 0.75,
         "scores": 8, "window_days": 28},
    ]


def quiet_facts():
    """Biology: a letter and a what-if nothing can reach. Nothing worth a story."""
    return [
        {"id": "next:biology", "kind": "next_letter", "class": "Biology", "pct": 84.0, "letter": "B",
         "next_letter": "A", "threshold": 90.0, "gap": 6.0},
        {"id": "whatif:biology:tests:50", "kind": "what_if", "class": "Biology", "bucket": "Tests",
         "bucket_weight_pct": 60.0, "points": 50.0, "target": 90.0, "needed_pct": None, "reachable": False},
    ]


def insight_facts():
    return {"scale": "90/80/70/60", "as_of": "2026-10-22", "facts": math_facts() + quiet_facts()}


GOOD = [
    {"text": "Still a B, 2.1 points from an A.", "cites": [NEXT]},
    {"text": "Mostly the {item:" + CAUSE + "}.", "cites": [CAUSE]},
    {"text": "Scores usually post Fridays.", "cites": [PATTERN]},
    {"text": "An 88 or better on the {next:" + WHATIF + "} gets it to an A.", "cites": [WHATIF]},
]
GOOD_STORY = ("Still a B, 2.1 points from an A. Mostly the Ch.4 Quiz (31/50). "
              "Scores usually post Fridays. An 88 or better on the next test gets it to an A.")


class ScriptedModel:
    """Answers by prompt: drafts by class, votes, and a headline. Records every call."""

    def __init__(self, drafts, votes=None, headline=None):
        self.drafts, self.votes, self.headline = drafts, votes, headline
        self.calls = []

    def complete(self, system, user):
        self.calls.append(system)
        if system == w.DRAFT_SYSTEM:
            return json.dumps({"sentences": self.drafts.get(json.loads(user)["class"], [])})
        if system == w.VOTE_SYSTEM:
            count = len(json.loads(user)["sentences"])
            votes = self.votes.pop(0) if self.votes else [True] * count
            return json.dumps({"backed": votes})
        return json.dumps(self.headline or {})


def index(facts=None):
    return w.fact_index(facts or math_facts())


def slotted(text, cites, idx):
    """The slot form a model would write for a plain sentence: each cited title becomes {item:<id>}
    (any "(a/b)" the sentence wrote stays, for the score check), and "next <bucket>" or the named next
    item of a cited what_if becomes {next:<id>}."""
    for fid in cites:
        fact = idx.get(fid) or {}
        if fact.get("title"):
            for name in sorted({fact["title"], w._short_title(fact["title"])}, key=len, reverse=True):
                text, n = re.subn(re.escape(name), "{item:" + fid + "}", text, flags=re.I)
                if n:
                    break
        if fact.get("class") and cites and fid == cites[0]:
            text = re.sub(r"(?<![\w{:])" + re.escape(fact["class"]) + r"(?![\w}])", "{class:" + fid + "}", text)
        if fact.get("kind") == "what_if":
            names = [re.escape(fact["next_title"])] if fact.get("next_title") else []
            names.append(r"next (?:test|quiz|project|lab|assessment|assignment)s?")
            text = re.sub("|".join(names), "{next:" + fid + "}", text, flags=re.I, count=1)
    return text


def claims(text, cites, idx, name=None, classes=None):
    """check_sentence with the fixed-form gates set aside: None when only a gate would drop it.

    The gates run last, so every other check keeps its own reason; this reads those checks alone.
    CauseFormGateTests covers the cause-sentence gate, LetterFormTests the letter-sentence one.
    """
    reason = w.check_sentence(text, cites, idx, name, classes)
    return None if reason == w.FORM_REASON else reason


def check(text, cites, idx, name=None):
    """The claim checks (gate aside, see claims) on the slot form of a plain sentence."""
    return claims(slotted(text, cites, idx), cites, idx, name)


def slot_all(sentences, idx):
    return [{**s, "text": slotted(s["text"], s["cites"], idx)} for s in sentences]


def filled(sentences, idx):
    """The story text the writer gives after filling each sentence's slots."""
    return " ".join(w.fix_articles(w.fill_slots(s["text"], idx)) for s in sentences)


class AcceptanceBarTests(unittest.TestCase):
    def test_a_good_story_passes(self):
        story = w.write_class(ScriptedModel({"Math": GOOD}), math_facts(), "Math")
        self.assertEqual(story["story"], GOOD_STORY)
        self.assertEqual(story["cites"], [NEXT, CAUSE, PATTERN, WHATIF])
        self.assertEqual(story["dropped"], [])

    def test_a_past_due_only_story_fails(self):
        past_due = [{"text": "Math has 2 missing assignments past due.", "cites": [NEXT]}]
        self.assertEqual(w.eval_story(past_due, index()), "restates past-due or missing work")
        self.assertIsNone(w.write_class(ScriptedModel({"Math": past_due}), math_facts(), "Math"))

    def test_a_story_citing_only_next_letter_fails(self):
        only_next = [{"text": "Math: B, 2.1 pts from an A.", "cites": [NEXT]}]
        self.assertEqual(w.eval_story(only_next, index()), "cites no grade_move, what_if or pattern fact")
        self.assertIsNone(w.write_class(ScriptedModel({"Math": only_next}), math_facts(), "Math"))

    def test_restating_is_a_fail_even_beside_an_insight(self):
        mixed = GOOD[:1] + [{"text": "The Ch.4 Quiz retake is still missing.", "cites": [WHATIF]}]
        self.assertEqual(w.eval_story([dict(s) for s in mixed], index()), "restates past-due or missing work")

    def test_the_insight_comes_in_order(self):
        backwards = [GOOD[0], GOOD[3], GOOD[2]]
        self.assertEqual(w.eval_story([dict(s) for s in backwards], index()),
                         "out of order (letter, cause, pattern, next step)")

    def test_no_filler_a_quiet_class_gets_no_story_and_no_model_call(self):
        model = ScriptedModel({"Biology": [{"text": "Biology: B, 6 pts from an A.", "cites": ["next:biology"]}]})
        self.assertIsNone(w.write_class(model, quiet_facts(), "Biology"))
        self.assertEqual(model.calls, [])

    def test_the_prompt_labels_the_example_as_made_up(self):
        self.assertIn("MADE-UP EXAMPLE", w.DRAFT_SYSTEM)
        self.assertIn("Never copy its class, titles, numbers", w.DRAFT_SYSTEM)
        self.assertIn(w.EXAMPLE_NOTE[0]["text"], w.DRAFT_SYSTEM)


class OldSanitizerCaseTests(unittest.TestCase):
    """Cases the Grok briefing's sanitizer had to patch up. Here the checker drops them."""

    def test_a_placeholder_zero_is_not_a_grade(self):
        reason = w.check_sentence("Grade at 0% after 62% on the Ch.4 Quiz.", [CAUSE], index())
        self.assertEqual(reason, "0% as a grade")
        self.assertIsNotNone(w.check_sentence("Math at 0.0 % needs attention.", [NEXT], index()))

    def test_stale_weekday_due_dates(self):
        self.assertEqual(w.check_sentence("Ch.4 Quiz · due Wed", [CAUSE], index()), "restates the to-do list")
        reason = w.check_sentence("An 88% on Wednesday's test gets the A back.", [WHATIF], index())
        self.assertEqual(reason, "names a weekday (Wednesday) no pattern backs")
        self.assertEqual(w.check_sentence("An 88% on the 10/30 test gets the A back.", [WHATIF], index()),
                         "names a date")

    def test_awaiting_score_is_not_called_missing(self):
        self.assertEqual(w.check_sentence("The Ch.4 Quiz is missing.", [CAUSE], index()), "restates the to-do list")
        self.assertEqual(w.check_sentence("The Ch.4 Quiz is awaiting a score.", [CAUSE], index()),
                         "restates the to-do list")


class CheckerTests(unittest.TestCase):
    def test_numbers_and_letters_must_be_in_the_cited_facts(self):
        self.assertIsNone(check(GOOD[0]["text"], [NEXT], index()))
        self.assertEqual(w.check_sentence("Math: B, 3.4 pts from an A.", [NEXT], index()),
                         "number 3.4 isn't in the cited facts")
        self.assertEqual(w.check_sentence("Math: C, 2.1 pts from an A.", [NEXT], index()),
                         "letter C isn't in the cited facts")
        self.assertEqual(w.check_sentence("A 95% on the next test gets the A back.", [NEXT], index()),
                         "number 95 isn't in the cited facts")

    def test_cites_must_exist_and_belong_to_the_class(self):
        self.assertEqual(w.check_sentence("Math: B.", [], index()), w.UNCITED_REASON)
        self.assertEqual(w.check_sentence("Math: B.", ["next:art"], index()), "unknown fact next:art")
        both = w.fact_index(math_facts() + quiet_facts())
        self.assertEqual(w.check_sentence("Biology: B.", ["next:biology"], both, "Math"),
                         "fact next:biology is another class")

    def test_a_weekday_needs_a_pattern_naming_it(self):
        self.assertIsNone(check("Scores mostly post on Fridays.", [PATTERN], index()))
        self.assertIsNotNone(w.check_sentence("Scores mostly post on Mondays.", [PATTERN], index()))

    def test_aeries_truncates_rather_than_rounds(self):
        facts = math_facts()
        facts[0] = {**facts[0], "pct": 87.96, "gap": 2.04}
        self.assertIsNone(check("Math: B at 87.9%.", [NEXT], w.fact_index(facts)))
        self.assertIsNone(check("Math: B at 88%.", [NEXT], w.fact_index(facts)))

    def test_a_sentence_most_votes_reject_is_dropped(self):
        model = ScriptedModel({"Math": GOOD}, votes=[[True, False, True, True], [True, False, True, True],
                                                    [True, True, True, True]])
        story = w.write_class(model, math_facts(), "Math")
        self.assertNotIn("Ch.4", story["story"])
        self.assertEqual(story["dropped"], ["vote"])
        self.assertEqual(model.calls.count(w.VOTE_SYSTEM), 3)

    def test_a_malformed_draft_writes_nothing(self):
        model = ScriptedModel({})
        model.complete = lambda system, user: "not json"
        self.assertIsNone(w.write_class(model, math_facts(), "Math"))


class CauseRuleTests(unittest.TestCase):
    """A cause may be stated only when explained has delta's sign and is within 20% of it."""

    def move(self, explained, delta=-1.6, coverage="auto", trimmed=False):
        if coverage == "auto":
            coverage = None if explained is None else round(explained / delta, 2)
        return {"id": MOVE, "kind": "grade_move", "class": "Math", "since": "2026-10-15", "from_pct": 89.5,
                "to_pct": 87.9, "delta": delta, "explained": explained, "coverage": coverage,
                "coverage_trimmed": trimmed, "causes": [
                    {"id": CAUSE, "title": "Ch.4 Quiz", "category": "Tests", "bucket": "Tests",
                     "bucket_weight_pct": 60.0, "change": "new", "earned": 31.0, "possible": 50.0,
                     "points": -1.5, "share": -1.5}]}

    CAUSE_LINE = "Down mainly from a 62% on the Ch.4 Quiz."

    def test_explained_with_the_wrong_sign_allows_no_cause(self):
        move = self.move(0.58)
        self.assertFalse(w.cause_allowed(move))
        idx = w.fact_index([move])
        self.assertEqual(w.check_sentence(self.CAUSE_LINE, [CAUSE], idx),
                         "states a cause the move's causes don't explain")
        self.assertEqual(w.check_sentence("Down 1.6 pts, mainly from quizzes.", [MOVE], idx),
                         "states a cause the move's causes don't explain")
        self.assertIsNone(check("Down 1.6 pts since the last check.", [MOVE], idx))

    def test_no_explained_allows_no_cause(self):
        move = self.move(None)
        self.assertFalse(w.cause_allowed(move))
        self.assertIsNotNone(w.check_sentence(self.CAUSE_LINE, [CAUSE], w.fact_index([move])))

    def test_explained_close_to_delta_allows_the_cause(self):
        move = self.move(-1.5)
        self.assertTrue(w.cause_allowed(move))
        self.assertIsNone(check(self.CAUSE_LINE, [CAUSE], w.fact_index([move])))

    def test_the_twenty_percent_edge(self):
        self.assertTrue(w.cause_allowed(self.move(-1.28)))
        self.assertFalse(w.cause_allowed(self.move(-1.2)))
        self.assertFalse(w.cause_allowed(self.move(-2.0)))

    def test_the_model_never_sees_causes_it_may_not_state(self):
        sent = w.model_facts([self.move(0.58)])[0]
        self.assertEqual(sent["causes"], [])
        self.assertIn("causes_withheld", sent)
        self.assertEqual(w.model_facts([self.move(-1.5)])[0]["causes"][0]["id"], CAUSE)

    def test_an_unexplained_move_alone_is_not_worth_a_story(self):
        self.assertFalse(w.worth_saying([self.move(0.58)]))
        self.assertTrue(w.worth_saying([self.move(-1.5)]))


class ReviewFindingTests(unittest.TestCase):
    """Each GitHub Engineer finding on #52: a real fact id cited, with content the facts don't back."""

    def test_spelled_out_numbers_are_checked(self):
        self.assertEqual(w.check_sentence("Math: B, three points from an A.", [NEXT], index()),
                         "number 3 isn't in the cited facts")
        self.assertEqual(w.check_sentence("A ninety-five on the next test gets the A back.", [WHATIF], index()),
                         "number 95 isn't the needed score or a round-up of it")
        self.assertIsNone(check("An eighty-eight on the next test gets the A back.", [WHATIF], index()))
        self.assertIsNone(check("Math: B, two point one pts from an A.", [NEXT], index()))
        self.assertEqual(w.spelled_to_digits("no one, one of them, twenty one points"),
                         "no one, one of them, 21 points")

    def test_a_plus_or_minus_letter_is_rejected(self):
        for text in ("Math is a C+.", "Math is a B-.", "Math is a B+ now.", "Math is a B plus."):
            self.assertIn("whole-letter scale", w.check_sentence(text, [NEXT], index()), text)
        self.assertIsNone(check("Math is a B.", [NEXT], index()))

    def test_cause_wording_must_cite_a_move_or_cause(self):
        self.assertEqual(w.check_sentence("Down mainly because of the Ch.4 Quiz.", [WHATIF], index()),
                         "states a cause without citing a grade_move or cause")
        for text in ("Math slipped due to the Ch.4 Quiz.", "The drop was driven by the Ch.4 Quiz."):
            self.assertEqual(w.check_sentence(text, [NEXT], index()),
                             "states a cause without citing a grade_move or cause", text)
        self.assertIsNone(check("Down mainly because of the Ch.4 Quiz.", [CAUSE], index()))

    def test_digits_after_a_period_are_checked(self):
        self.assertEqual(w.check_sentence("Down mainly from the Ch.9 quiz.", [CAUSE], index()),
                         "number 9 isn't in the cited facts")
        self.assertIsNone(check("Down mainly from the Ch.4 Quiz.", [CAUSE], index()))

    def test_a_number_must_match_the_field_the_claim_is_about(self):
        self.assertEqual(w.check_sentence("Math is up 60 percent after the Ch.4 Quiz.", [CAUSE], index()),
                         "number 60 isn't in the cited facts as a move")
        self.assertEqual(w.check_sentence("Math: B, 60 pts from an A.", [NEXT, WHATIF], index()),
                         "number 60 isn't in the cited facts")
        self.assertEqual(w.check_sentence("Tests are 88% of the grade.", [WHATIF], index()),
                         "number 88 isn't in the cited facts as a weight")
        # A bucket is school work: outside a slot it's dropped, so the weight can't be said that way.
        self.assertEqual(w.check_sentence("Tests are 60% of the grade.", [WHATIF], index()),
                         "names school work (Tests) outside a slot")
        self.assertIsNone(check("Down 2.5 pts since the last check.", [MOVE], index()))

    def test_a_score_fraction_is_not_a_date(self):
        self.assertIsNone(check("Down mainly from a 31/50 on the Ch.4 Quiz.", [CAUSE], index()))
        self.assertEqual(w.check_sentence("An 88% on the next test, after a 31/50.", [WHATIF], index()),
                         "score 31/50 isn't in the cited facts")
        self.assertEqual(w.check_sentence("An 88% on the 10/30 test gets the A back.", [WHATIF], index()),
                         "names a date")

    def test_apply_write_up_matches_class_names_like_the_front_end(self):
        self.assertEqual(w.normalize_name("AP Calc (BC) - P3"), "apcalcbcp3")
        summary = {"classes": [{"class_name": "Math 7 Accel"}, {"class_name": "P3 Biology"}, {"class_name": "Art"}]}
        local = {"stories": [{"class": "Math", "story": "short"}, {"class": "math 7 accel", "story": "long"},
                             {"class": "Biology", "story": "bio"}]}
        out = w.apply_write_up(summary, local)
        self.assertEqual([c["story"] for c in out["classes"]], ["long", "bio", ""])


class ClassSlotTests(unittest.TestCase):
    def test_exact_names_first_and_an_ambiguous_contains_match_lands_nowhere(self):
        local = {"stories": [{"class": "Math", "story": "math story"},
                             {"class": "AP Math Analysis", "story": "ap story"}]}
        summary = {"classes": [{"class_name": "AP Math Analysis"}, {"class_name": "math"}]}
        self.assertEqual([c["story"] for c in w.apply_write_up(summary, local)["classes"]],
                         ["ap story", "math story"])
        # No exact name and two stories contain "Math": ambiguous, so no story.
        two = {"stories": [{"class": "AP Math Analysis", "story": "ap story"},
                           {"class": "Math Honors", "story": "honors story"}]}
        out = w.apply_write_up({"classes": [{"class_name": "Math"}, {"class_name": "Biology"}]}, two)
        self.assertEqual([c["story"] for c in out["classes"]], ["", ""])
        # A story already taken by an exact match isn't reused by a contains match.
        out = w.apply_write_up({"classes": [{"class_name": "Math"}, {"class_name": "Math Lab"}]},
                               {"stories": [{"class": "Math", "story": "math story"}]})
        self.assertEqual([c["story"] for c in out["classes"]], ["math story", ""])
        # One contains candidate still lands.
        out = w.apply_write_up({"classes": [{"class_name": "P3 Biology"}]},
                               {"stories": [{"class": "Biology", "story": "bio"}]})
        self.assertEqual(out["classes"][0]["story"], "bio")


def ch5(due="2026-10-30", points=50.0, bucket="Tests"):
    return {"class": "Math", "bucket": bucket, "title": "Ch. 5 Test", "due": due, "points": points}


class WeekdayRuleTests(unittest.TestCase):
    """No relative days. A named next item carries its own due date; a weekday needs a posting pattern."""

    def facts_with_item(self, upcoming):
        return w.fact_index(w.attach_next_items(math_facts(), upcoming))

    def test_a_relative_weekday_is_dropped(self):
        for text in ("An 88% on next Friday's test gets the A back.", "An 88% on this Thursday's quiz helps.",
                     "An 88% on the test tomorrow gets the A back."):
            self.assertIn("relative day", w.check_sentence(text, [WHATIF, PATTERN], index()), text)

    def test_the_named_item_with_its_due_date_passes(self):
        idx = self.facts_with_item([ch5()])
        self.assertEqual(idx[WHATIF]["next_due_label"], "Oct 30")
        line = "An 88% on the Ch. 5 Test, due Oct 30, gets the A back."
        self.assertIsNone(check(line, [WHATIF], idx))
        self.assertIsNone(w.eval_story([dict(GOOD[0]), {"text": line, "cites": [WHATIF]}], idx))

    def test_the_wrong_date_is_dropped(self):
        idx = self.facts_with_item([ch5()])
        for text in ("An 88% on the Ch. 5 Test, due Oct 29, gets the A back.",
                     "An 88% on the Ch. 5 Test, due 10/31, gets the A back."):
            self.assertEqual(w.check_sentence(text, [WHATIF], idx), "names a date", text)

    def test_a_date_needs_the_items_own_fact(self):
        idx = self.facts_with_item([ch5()])
        self.assertEqual(w.check_sentence("Math: B, due Oct 30.", [NEXT], idx), "names a date")

    def test_no_due_date_means_the_next_test(self):
        idx = self.facts_with_item([])
        self.assertNotIn("next_due", idx[WHATIF])
        self.assertIsNone(check("An 88% on the next test gets the A back.", [WHATIF], idx))
        self.assertEqual(w.check_sentence("An 88% on the Ch. 5 Test, due Oct 30, gets the A back.", [WHATIF], idx),
                         "names a date")

    def test_an_item_of_another_size_or_bucket_is_not_attached(self):
        for item in (ch5(points=100.0), ch5(bucket="Quizzes")):
            self.assertNotIn("next_title", self.facts_with_item([item])[WHATIF])

    def test_a_posting_day_weekday_needs_the_pattern(self):
        self.assertIsNone(check("Scores usually post Fridays.", [PATTERN], index()))
        self.assertEqual(w.check_sentence("Scores usually post Fridays.", [NEXT], index()),
                         "names a weekday (Fridays) no pattern backs")

    def test_upcoming_items_come_from_unscored_work_in_its_bucket(self):
        classes = [{"name": "Math", "counted_insight": {"layout": "percent_of_grade", "categories": [
            {"name": "Tests", "weight_pct": 60.0}, {"name": "Homework", "weight_pct": 40.0}]}, "assignments": [
            {"description": "Ch. 5 Test", "category": "Tests", "points_earned": None, "points_possible": 50,
             "due_date": "10/30/2026"},
            {"description": "Ch. 4 Quiz", "category": "Tests", "points_earned": 31, "points_possible": 50,
             "due_date": "10/14/2026"},
            {"description": "Old Test", "category": "Tests", "points_earned": None, "points_possible": 50,
             "due_date": "10/01/2026"},
            {"description": "Worksheet 9", "category": "Homework", "points_earned": None, "points_possible": 10,
             "due_date": "10/23/2026"},
        ]}]
        self.assertEqual(w.upcoming_items(classes, "2026-10-22"), [
            {"class": "Math", "bucket": "Homework", "title": "Worksheet 9", "due": "2026-10-23", "points": 10.0},
            {"class": "Math", "bucket": "Tests", "title": "Ch. 5 Test", "due": "2026-10-30", "points": 50.0},
        ])

    def test_the_prompt_example_has_no_relative_day_and_passes_the_claim_checks(self):
        idx = w.fact_index(w.EXAMPLE_FACTS)
        for sentence in w.EXAMPLE_NOTE:
            self.assertIsNone(w._RELATIVE_RE.search(sentence["text"]))
            cited = w._cited_facts(sentence["cites"], idx)
            self.assertIsNone(w._claims_reason(w.fill_slots(sentence["text"], idx), cited, idx, slotted=sentence["text"]),
                              sentence["text"])
            self.assertIsNone(w.check_sentence(sentence["text"], sentence["cites"], idx), sentence["text"])
        self.assertIsNone(w.eval_story([dict(s) for s in w.EXAMPLE_NOTE], idx))


class CoverageFieldTests(unittest.TestCase):
    """The engine's coverage field decides the 20% cause rule; trimmed history allows no cause."""

    LINE = "Down mainly from a 62% on the Ch.4 Quiz."

    def move(self, **fields):
        return {**math_facts()[1], **fields}

    def test_the_rule_reads_coverage_not_explained(self):
        self.assertTrue(w.cause_allowed(self.move(explained=None, coverage=0.8)))
        self.assertTrue(w.cause_allowed(self.move(explained=None, coverage=1.2)))
        self.assertFalse(w.cause_allowed(self.move(explained=-2.4, coverage=None)))
        self.assertFalse(w.cause_allowed(self.move(explained=-2.4, coverage=0.79)))
        self.assertFalse(w.cause_allowed(self.move(explained=-2.4, coverage=1.21)))
        self.assertFalse(w.cause_allowed(self.move(explained=-2.4, coverage=-1.0)))
        idx = w.fact_index([self.move(coverage=0.5)])
        self.assertEqual(w.check_sentence(self.LINE, [CAUSE], idx), "states a cause the move's causes don't explain")
        self.assertIsNone(check(self.LINE, [CAUSE], w.fact_index([self.move(coverage=0.96)])))

    def test_a_trimmed_history_allows_no_cause(self):
        move = self.move(coverage=0.96, coverage_trimmed=True)
        self.assertFalse(w.cause_allowed(move))
        idx = w.fact_index([move])
        self.assertEqual(w.check_sentence(self.LINE, [CAUSE], idx), "states a cause the move's causes don't explain")
        self.assertEqual(w.check_sentence("Down 2.5 pts, mainly from quizzes.", [MOVE], idx),
                         "states a cause the move's causes don't explain")
        self.assertIsNone(check("Down 2.5 pts since the last check.", [MOVE], idx))
        sent = w.model_facts([move])[0]
        self.assertEqual(sent["causes"], [])
        self.assertIn("trimmed", sent["causes_withheld"])
        self.assertFalse(w.worth_saying([move]))

    def test_causes_are_cited_in_share_order(self):
        second = "cause:math:2026-10-15:hw3"
        move = self.move(causes=[
            {"id": second, "title": "HW 3", "category": "Homework", "bucket": "Homework", "bucket_weight_pct": 40.0,
             "change": "changed", "earned": 2.0, "possible": 10.0, "prior_earned": 9.0, "prior_possible": 10.0,
             "points": -0.6, "share": -0.5},
            math_facts()[1]["causes"][0] | {"share": -1.9},
        ])
        idx = w.fact_index([move])
        self.assertEqual([c["id"] for c in w.ranked_causes(move)], [CAUSE, second])
        self.assertEqual([c["id"] for c in w.model_facts([move])[0]["causes"]], [CAUSE, second])
        self.assertIsNone(check(self.LINE, [CAUSE], idx))
        self.assertIsNone(check("Down from the Ch.4 Quiz, then HW 3.", [CAUSE, second], idx))
        self.assertEqual(w.check_sentence("Down mainly from HW 3.", [second], idx), "names causes out of share order")
        # Without most-family wording a list of causes may come in any order.
        self.assertIsNone(check("Down from the {item:%s} and the {item:%s}." % (second, CAUSE), [second, CAUSE], idx))
        # With it, the named cause must be the top share.
        for most in ("Mostly the {item:%s}.", "Down mainly from the {item:%s}.", "The {item:%s} did most of the damage.",
                     "Most of the drop was the {item:%s}.", "The biggest hit was the {item:%s}."):
            self.assertEqual(w.check_sentence(most % second, [second], idx), "names causes out of share order", most)
            self.assertIsNone(check(most % CAUSE, [CAUSE], idx), most)
        self.assertEqual(w.check_sentence("Mostly the {item:%s} and the {item:%s}." % (second, CAUSE), [second, CAUSE], idx),
                         "names causes out of share order")

    def test_engine_moves_carry_the_fields_the_writer_reads(self):
        from test_insights import POG_ROWS, POG_TOTALS, class_input, snapshot, TODAY

        prior = snapshot("2026-10-21", {"Biology": (91.8, "A")})
        cls = class_input("Biology", POG_TOTALS, POG_ROWS, 85.2, "B", new_keys=["unit2test"])
        move = next(f for f in insights.class_facts(cls, [prior], prior, TODAY) if f["kind"] == "grade_move")
        self.assertIn("coverage", move)
        self.assertIs(move["coverage_trimmed"], False)
        self.assertEqual(w.cause_allowed(move), 0.8 <= move["coverage"] <= 1.2)
        self.assertTrue(all({"change", "share"} <= set(c) for c in move["causes"]))
        trimmed = insights.class_facts({**cls, "scores_trimmed": True}, [prior], prior, TODAY)
        trimmed_move = next(f for f in trimmed if f["kind"] == "grade_move")
        self.assertTrue(trimmed_move["coverage_trimmed"])
        self.assertFalse(w.cause_allowed(trimmed_move))


GEO_NEXT, GEO_WHATIF = "next:geometry", "whatif:geometry:tests:10"
CHEM_MOVE, CHEM_C1, CHEM_C2 = "move:chem:2026-10-14", "cause:chem:2026-10-14:lab3packet", "cause:chem:2026-10-14:warmups"


def geometry_facts():
    """A C that has always been a C: 0.5 from a B, an 84.2 needed on the next 10-point test. No move."""
    return [
        {"id": GEO_NEXT, "kind": "next_letter", "class": "Geometry", "pct": 79.5, "letter": "C",
         "next_letter": "B", "threshold": 80.0, "gap": 0.5},
        {"id": GEO_WHATIF, "kind": "what_if", "class": "Geometry", "bucket": "Tests", "bucket_weight_pct": 90.0,
         "points": 10.0, "target": 80.0, "needed_pct": 84.2, "reachable": True},
    ]


def chem_facts():
    """An A that stays an A: down 1.6 since Oct 14, mostly from the Lab 3 Packet. No next letter."""
    return [{"id": CHEM_MOVE, "kind": "grade_move", "class": "Chem", "since": "2026-10-14", "from_pct": 97.9,
             "to_pct": 96.3, "delta": -1.6, "explained": -1.7, "coverage": 1.06, "coverage_trimmed": False,
             "causes": [
                 {"id": CHEM_C1, "title": "Lab 3 Packet", "category": "Coursework", "bucket": "Coursework",
                  "bucket_weight_pct": 10.0, "change": "new", "earned": 4.0, "possible": 10.0, "points": -0.8,
                  "share": -1.3},
                 {"id": CHEM_C2, "title": "Warm Ups", "category": "Coursework", "bucket": "Coursework",
                  "bucket_weight_pct": 10.0, "change": "new", "earned": 1.0, "possible": 3.0, "points": -0.2,
                  "share": -0.4}]}]


class BakeoffFindingTests(unittest.TestCase):
    """Wrong claims from the Oct 21 local-model bake-off, rebuilt on synthetic facts."""

    def test_a_needed_score_is_exact_or_rounded_up_never_down(self):
        idx = w.fact_index(geometry_facts())
        self.assertEqual(w.say_needed(84.2), "85 or better")
        self.assertEqual(w.say_needed(84.0), "84 or better")
        self.assertEqual(w.model_facts(geometry_facts())[1]["say_needed"], "85 or better")
        self.assertEqual(w.check_sentence("An 84% on the next test gets the B.", [GEO_WHATIF], idx),
                         "number 84 isn't the needed score or a round-up of it")
        self.assertIsNone(check("An 85 or better on the next test gets there.", [GEO_WHATIF], idx))
        self.assertIsNone(check("An 84.2% on the next test gets the B.", [GEO_WHATIF], idx))
        self.assertEqual(w.check_sentence("An 86 or better on the next test gets there.", [GEO_WHATIF], idx),
                         "number 86 isn't the needed score or a round-up of it")

    def test_what_moved_and_bare_mainly_need_a_real_move(self):
        idx = w.fact_index(geometry_facts())
        self.assertEqual(w.check_sentence("What moved the grade was a 10-point test.", [GEO_WHATIF], idx),
                         "echoes the prompt (What moved)")
        self.assertEqual(w.check_sentence("The grade moved because of the tests.", [GEO_WHATIF], idx),
                         "states a cause without citing a grade_move or cause")
        self.assertEqual(w.check_sentence("The grade is mainly an 84.2% on tests.", [GEO_WHATIF], idx),
                         "states a cause without citing a grade_move or cause")
        self.assertIsNone(check("Scores mostly post on Fridays.", [PATTERN], index()))

    def test_back_to_a_letter_needs_a_move_from_that_letter(self):
        line = "An 85 or better on the next test gets it back to a B."
        self.assertEqual(w.check_sentence(line, [GEO_WHATIF], w.fact_index(geometry_facts())),
                         "says back to or again with a letter no move shows the class held")
        moved = geometry_facts() + [{"id": "move:geometry:2026-10-14", "kind": "grade_move", "class": "Geometry",
                                     "since": "2026-10-14", "from_pct": 81.0, "to_pct": 79.5, "delta": -1.5,
                                     "explained": None, "coverage": None, "coverage_trimmed": False, "causes": []}]
        self.assertIsNone(check(line, [GEO_WHATIF], w.fact_index(moved)))
        self.assertEqual(w.check_sentence("A B again with an 85 or better on the next test.", [GEO_WHATIF],
                                          w.fact_index(geometry_facts())),
                         "says back to or again with a letter no move shows the class held")
        self.assertEqual(w.check_sentence("Down 1.6, so it has to regain the A.", [CHEM_MOVE], w.fact_index(chem_facts())),
                         "says back to or again with a letter no move shows the class held")

    def test_moved_to_a_letter_needs_a_move_that_crossed_into_it(self):
        # Re-run finding: "Moved to a C, 0.5 points from a B." for a class with no grade_move at all.
        line = "Moved to a C, 0.5 points from a B."
        geo_next = GEO_NEXT
        self.assertEqual(w.check_sentence(line, [geo_next], w.fact_index(geometry_facts())),
                         "says the class moved to C but no move crossed into that letter")
        crossed = geometry_facts() + [{"id": "move:geometry:2026-10-14", "kind": "grade_move", "class": "Geometry",
                                       "since": "2026-10-14", "from_pct": 81.0, "to_pct": 79.5, "delta": -1.5,
                                       "explained": None, "coverage": None, "coverage_trimmed": False, "causes": []}]
        self.assertIsNone(check(line, [geo_next, "move:geometry:2026-10-14"], w.fact_index(crossed)))
        self.assertEqual(w.check_sentence(line, [geo_next], w.fact_index(crossed)),
                         "says the class moved to C but no move crossed into that letter")
        held = crossed[:-1] + [dict(crossed[-1], from_pct=79.9)]
        self.assertEqual(w.check_sentence(line, [geo_next, "move:geometry:2026-10-14"], w.fact_index(held)),
                         "says the class moved to C but no move crossed into that letter")
        self.assertEqual(w.check_sentence("Slipped to a C this term.", [geo_next], w.fact_index(geometry_facts())),
                         "says the class moved to C but no move crossed into that letter")
        self.assertIsNone(check("Still a C, 0.5 points from a B.", [geo_next], w.fact_index(geometry_facts())))

    def test_no_next_letter_claim_for_an_a_or_without_a_next_letter_fact(self):
        idx = w.fact_index(chem_facts())
        self.assertEqual(w.check_sentence("A 99 or better on the next test gets it up to an A.", [CHEM_MOVE], idx),
                         "claims a next letter with no next_letter or what_if fact")
        self.assertEqual(w.check_sentence("Down 1.6, now 1.6 points from an A.", [CHEM_MOVE], idx),
                         "claims a next letter with no next_letter or what_if fact")
        at_a = chem_facts() + [dict(geometry_facts()[1], id="whatif:chem:tests:10", **{"class": "Chem"})]
        self.assertEqual(w.check_sentence("An 85 or better on the next test gets it to a B.", ["whatif:chem:tests:10"],
                                          w.fact_index(at_a)),
                         "claims a next letter for a class at an A")

    def test_a_score_on_an_item_is_a_percent_not_points_earned(self):
        chem = w.fact_index(chem_facts())
        self.assertEqual(w.check_sentence("Down 1.6, mainly due to a 4.0 on the Lab 3 Packet.", [CHEM_MOVE, CHEM_C1], chem),
                         "number 4.0 isn't in the cited facts as a score")
        self.assertIsNone(check("Down 1.6, mainly due to a 40% on the Lab 3 Packet.", [CHEM_MOVE, CHEM_C1], chem))
        self.assertIsNone(check("Down 1.6, mainly due to a 4/10 on the Lab 3 Packet.", [CHEM_MOVE, CHEM_C1], chem))

    def test_a_title_written_without_its_parenthetical_is_still_a_name(self):
        facts = chem_facts()
        facts[0]["causes"][1]["title"] = "Warm Ups (wk9-10)"
        idx = w.fact_index(facts)
        self.assertIsNone(check("The Lab 3 Packet (4/10) and Warm Ups (1/3) hurt the grade most.",
                                           [CHEM_C1, CHEM_C2], idx))

    def test_an_offsetting_cause_may_be_named_but_not_as_the_reason(self):
        facts = chem_facts()
        facts[0]["causes"].append({"id": "cause:chem:2026-10-14:rocketlab", "title": "Rocket Lab",
                                   "category": "Coursework", "bucket": "Coursework", "bucket_weight_pct": 10.0,
                                   "change": "new", "earned": 9.0, "possible": 10.0, "points": 0.8, "share": 0.4})
        idx = w.fact_index(facts)
        # The exact contrast form passes; "helped a little" is cause wording off the forms (#52 868098d review).
        self.assertIsNone(check("The Rocket Lab (9/10) helped.", ["cause:chem:2026-10-14:rocketlab"], idx))
        self.assertIsNotNone(check("The Rocket Lab (9/10) helped a little.", ["cause:chem:2026-10-14:rocketlab"], idx))
        self.assertEqual(w.check_sentence("Down 1.6, mostly from the Rocket Lab (9/10).",
                                          [CHEM_MOVE, "cause:chem:2026-10-14:rocketlab"], idx),
                         "gives an offsetting cause as the reason for the move")
        self.assertEqual(w.check_sentence("Down 1.6, mostly from the Warm Ups (1/3).", [CHEM_MOVE, CHEM_C2], idx),
                         "names causes out of share order")
        # Re-run finding: the helping lab named beside the drivers as if it caused the drop.
        lab = "cause:chem:2026-10-14:rocketlab"
        for line in ("Mainly the Lab 3 Packet (4/10) and the Rocket Lab (9/10).",
                     "The Lab 3 Packet (4/10) and the Rocket Lab (9/10) drove the drop.",
                     "The Lab 3 Packet (4/10) and the Rocket Lab (9/10) had the biggest impact."):
            self.assertEqual(w.check_sentence(line, [CHEM_C1, lab], idx),
                             "names an offsetting cause as if it drove the move", line)
        self.assertIsNone(check("The Lab 3 Packet (4/10) hurt the grade, while the Rocket Lab (9/10) helped.",
                                           [CHEM_C1, lab], idx))

    def test_students_wording_is_dropped(self):
        self.assertIn("echoes the prompt", w.check_sentence(
            "Students with an 85 or better on the next test earn a B.", [GEO_WHATIF], w.fact_index(geometry_facts())))

    def test_json_reads_fenced_and_thinking_replies(self):
        body = '{"sentences": [{"text": "x", "cites": ["a"]}]}'
        for raw in (f"```json\n{body}\n```", f"```\n{body}\n```", f"<think>hmm, the facts...</think>\n{body}",
                    f"<think>a</think>```json\n{body}\n```", f"Here you go: {body}"):
            self.assertEqual(w._json(raw), json.loads(body), raw)
        self.assertEqual(w._sentences(f"```json\n{body}\n```"), [{"text": "x", "cites": ["a"]}])
        self.assertIsNone(w._json("```json\nnot json\n```"))
        # "<think>" inside a JSON string is the answer, not a think block.
        self.assertEqual(w._json('{"a":"<think>"}'), {"a": "<think>"})
        self.assertEqual(w._json('```json\n{"a":"x <think> y"}\n```'), {"a": "x <think> y"})
        self.assertIsNone(w._json("<think>x"))

    def test_names_must_match_a_cited_fact(self):
        # Review repro: Geo's only cause is the Unit Test (20/40), and its scores post Thursdays.
        geo = [{"id": "next:g", "kind": "next_letter", "class": "Geo", "pct": 69.0, "letter": "D", "next_letter": "C",
                "threshold": 70.0, "gap": 1.0},
               {"id": "move:g", "kind": "grade_move", "class": "Geo", "since": "2026-10-14", "from_pct": 71.5,
                "to_pct": 69.0, "delta": -2.5, "explained": -2.5, "coverage": 1.0, "coverage_trimmed": False,
                "causes": [{"id": "cause:g:t", "title": "Unit Test", "bucket": "Tests", "bucket_weight_pct": 50.0,
                            "earned": 20.0, "possible": 40.0, "points": -2.5, "share": -2.5}]},
               {"id": "pat:g", "kind": "posting_day", "class": "Geo", "weekday": "Thursday", "share": 0.8,
                "scores": 10, "window_days": 28}]
        idx = w.fact_index(geo)
        for text, cites in (("The Glaze Test did most of the damage.", ["cause:g:t"]),
                            ("Ceramics scores usually post Thursdays.", ["pat:g"]),
                            ("The Final Exam did most of the damage.", ["cause:g:t"]),
                            ("Mostly the Final Exam.", ["cause:g:t"]),
                            ("Mostly Final Exam (20/40).", ["cause:g:t"]),
                            ("The Unit Test (20/40) did most of the damage.", ["cause:g:t"])):
            self.assertRegex(w.check_sentence(text, cites, idx, "Geo") or "", r"outside a slot|another class", text)
        # Slots: the id must be cited, and an item slot needs a fact with a title.
        self.assertIn("doesn't cite", w.check_sentence("The {item:cause:g:t} did most of the damage.", ["move:g"], idx))
        self.assertEqual(w.check_sentence("Mostly the {item:move:g}.", ["move:g"], idx), "item slot move:g has no title")
        self.assertEqual(w.check_sentence("Mostly the {item:cause:g:t.", ["cause:g:t"], idx), "malformed slot")
        self.assertIn("isn't a what_if", w.check_sentence("A 74 or better on the {next:cause:g:t}.", ["cause:g:t"], idx))
        for text, cites in (("The {item:cause:g:t} did most of the damage.", ["cause:g:t"]),
                            ("Mostly the {item:cause:g:t}.", ["cause:g:t"]),
                            ("{class:pat:g} scores usually post Thursdays.", ["pat:g"]),
                            ("Scores usually post Thursdays.", ["pat:g"]),
                            ):
            self.assertIsNone(w.check_sentence(text, cites, idx, "Geo"), text)
        # A cause named without a slot passes the claim checks but not the cause-sentence gate.
        self.assertIsNone(claims("Low scores dragged it.", ["cause:g:t"], idx, "Geo"))
        self.assertEqual(w.check_sentence("Low scores dragged it.", ["cause:g:t"], idx, "Geo"), w.CAUSE_FORM_REASON)
        self.assertEqual(w.fill_slots("The {item:cause:g:t} did most of the damage.", idx),
                         "The Unit Test (20/40) did most of the damage.")
        self.assertEqual(w.fill_slots("{class:pat:g} scores usually post Thursdays.", idx),
                         "Geo scores usually post Thursdays.")
        # A score the model writes after the slot is its own, and checked.
        self.assertEqual(w.fill_slots("The {item:cause:g:t} (20/40) hurt.", idx), "The Unit Test (20/40) hurt.")
        self.assertEqual(w.check_sentence("The {item:cause:g:t} (21/40) hurt.", ["cause:g:t"], idx),
                         "score 21/40 isn't in the cited facts")
        # The whitelist is empty: "the next test" without a slot is dropped.
        self.assertEqual(w.NOUN_WHITELIST, ())
        self.assertEqual(w.work_noun_outside_slot("A 74 or better on the next test gets it to a C."), "test")
        self.assertEqual(w.work_noun_outside_slot('The "Final Exam" hurt.'), "Final")

    def test_physics_bar_sentences_pass_the_names_check(self):
        phys = [{"id": "move:p", "kind": "grade_move", "class": "Physics & Eng", "since": "2026-10-14",
                 "from_pct": 98.1, "to_pct": 96.5, "delta": -1.6, "explained": -1.73, "coverage": 1.08,
                 "coverage_trimmed": False, "causes": [
                     {"id": "cause:p:packet", "title": "Unit 2 Packet", "bucket": "Coursework", "bucket_weight_pct": 5.0,
                      "change": "new", "earned": 4.0, "possible": 10.0, "points": -0.79, "share": -1.36},
                     {"id": "cause:p:warmups", "title": "Unit 2 Warm Ups (wk9-10)", "bucket": "Coursework",
                      "bucket_weight_pct": 5.0, "change": "new", "earned": 1.0, "possible": 3.0, "points": -0.21,
                      "share": -0.75},
                     {"id": "cause:p:lab", "title": "Balloon Rocket Lab", "bucket": "Coursework", "bucket_weight_pct": 5.0,
                      "change": "new", "earned": 9.0, "possible": 10.0, "points": 0.77, "share": 0.38}]},
                {"id": "whatif:p:assessments:10", "kind": "what_if", "class": "Physics & Eng", "bucket": "Assessments",
                 "bucket_weight_pct": 95.0, "points": 10.0, "target": 90.0, "needed_pct": 74.6, "reachable": True}]
        idx = w.fact_index(phys)
        for text, cites in (("Still an A, down 1.6 since Oct 14.", ["move:p"]),
                            ("The Unit 2 Packet (4/10) did most of the damage.", ["cause:p:packet"]),
                            ("Mostly the Unit 2 Packet (4/10) and the Unit 2 Warm Ups (1/3).",
                             ["cause:p:packet", "cause:p:warmups"]),
                            ("The Balloon Rocket Lab (9/10) helped.", ["cause:p:lab"]),
                            ("Physics & Eng scores dropped 1.6.", ["move:p"])):
            self.assertIsNone(check(text, cites, idx, "Physics & Eng"), text)
        draft = [{"text": "Still an A, down 1.6 since Oct 14.", "cites": ["move:p"]},
                 {"text": "The {item:cause:p:packet} did most of the damage.", "cites": ["cause:p:packet"]},
                 {"text": "The {item:cause:p:lab} helped.", "cites": ["cause:p:lab"]}]
        self.assertEqual(filled(draft, idx), "Still an A, down 1.6 since Oct 14. The Unit 2 Packet (4/10) did most "
                                             "of the damage. The Balloon Rocket Lab (9/10) helped.")
        self.assertEqual(w.fill_slots("A 75 or better on the {next:whatif:p:assessments:10}.", idx),
                         "A 75 or better on the next assessment.")

    def test_review_nits_scores_quarters_and_dashes(self):
        clay = [{"id": "move:c", "kind": "grade_move", "class": "Clay", "since": "2026-10-14", "from_pct": 71.0,
                 "to_pct": 69.0, "delta": -2.0, "explained": -2.0, "coverage": 1.0, "coverage_trimmed": False,
                 "causes": [{"id": "cause:c:glaze", "title": "Glaze Test", "bucket": "Projects", "bucket_weight_pct": 40.0,
                             "earned": 11.0, "possible": 20.0, "points": -2.0, "share": -2.0}]}]
        idx = w.fact_index(clay)
        # "(11/20)" is a score in parentheses, never Nov 20.
        self.assertIsNone(check("The Glaze Test (11/20) did most of the damage.", ["cause:c:glaze"], idx))
        self.assertEqual(w.check_sentence("The Glaze Test (12/20) did most of the damage.", ["cause:c:glaze"], idx),
                         "score 12/20 isn't in the cited facts")
        self.assertEqual(w.spelled_to_digits("three-quarters of a point"), "0.75 point")
        near = [{"id": "next:q", "kind": "next_letter", "class": "Q", "pct": 69.25, "letter": "D", "next_letter": "C",
                 "threshold": 70.0, "gap": 0.75}]
        self.assertIsNone(check("Still a D, three-quarters of a point from a C.", ["next:q"], w.fact_index(near)))
        dash = [dict(near[0], pct=68.6, gap=1.4)]
        for text in ("Still a D, 1.4 points from a C--one more score.", "Still a D, 1.4 points from a C–one more score."):
            self.assertIsNone(check(text, ["next:q"], w.fact_index(dash)), text)
        self.assertEqual(w.spelled_to_digits("one and three quarters points"), "1.75 points")
        quarters = [dict(near[0], pct=68.25, gap=1.75)]
        self.assertIsNone(check("Still a D, one and three quarters points from a C.", ["next:q"], w.fact_index(quarters)))
        # "down 2.5 (10/14)": a pair that is the cited since date reads as that date, not a score.
        moved = clay[0]
        self.assertIsNone(check("Still a D, down 2 (10/14).", ["move:c"], w.fact_index([moved])))
        # Any other pair in parentheses is read as a score, and no fact backs it.
        self.assertEqual(w.check_sentence("Still a D, down 2 (10/15).", ["move:c"], w.fact_index([moved])),
                         "score 10/15 isn't in the cited facts")
        self.assertIn("whole-letter", w.check_sentence("Still a D, 1.4 points from a C-.", ["next:q"], w.fact_index(dash)))

    def test_example_wording_on_real_facts_passes(self):
        # Review repros: the example-copy check is gone, so real lines that match the example pass.
        facts = [{"id": "move:art:2026-09-09", "kind": "grade_move", "class": "Art", "since": "2026-09-09",
                  "from_pct": 71.1, "to_pct": 68.8, "delta": -2.3, "explained": -2.3, "coverage": 1.0,
                  "coverage_trimmed": False, "causes": [
                      {"id": "cause:art:2026-09-09:mural", "title": "Mural", "category": "Projects", "bucket": "Projects",
                       "bucket_weight_pct": 50.0, "change": "new", "earned": 11.0, "possible": 20.0, "points": -2.3,
                       "share": -2.3}]},
                 {"id": "whatif:art:projects:20", "kind": "what_if", "class": "Art", "bucket": "Projects",
                  "bucket_weight_pct": 50.0, "points": 20.0, "target": 70.0, "needed_pct": 73.4, "reachable": True},
                 {"id": "pattern:posting_day:art", "kind": "posting_day", "class": "Art", "weekday": "Tuesday",
                  "share": 0.8, "scores": 10, "window_days": 28}]
        idx = w.fact_index(facts)
        self.assertEqual(w.model_facts(facts)[0]["say_opening"], "Slipped to a D since Sep 9, down 2.3.")
        self.assertIsNone(check("Slipped to a D since Sep 9, down 2.3.", ["move:art:2026-09-09"], idx, "Art"))
        self.assertIsNone(check("Scores usually post Tuesdays.", ["pattern:posting_day:art"], idx, "Art"))
        self.assertIsNone(check("A 74 or better on the next project gets it to a C.",
                                           ["whatif:art:projects:20"], idx, "Art"))
        draft = [{"text": "Slipped to a D since Sep 9, down 2.3.", "cites": ["move:art:2026-09-09"]},
                 {"text": "Mostly the Mural (11/20).", "cites": ["cause:art:2026-09-09:mural"]},
                 {"text": "Scores usually post Tuesdays.", "cites": ["pattern:posting_day:art"]},
                 {"text": "A 74 or better on the next project gets it to a C.", "cites": ["whatif:art:projects:20"]}]
        story = w.write_class(ScriptedModel({"Art": slot_all(draft, idx)}), facts, "Art")
        self.assertEqual(story["story"], " ".join(s["text"] for s in draft))
        self.assertEqual(story["dropped"], [])
        # A needed 73.0 is said "73 or better" (round up only), and that full story passes too.
        exact = [dict(f, needed_pct=73.0) if f["kind"] == "what_if" else f for f in facts]
        self.assertEqual(w.model_facts(exact)[1]["say_needed"], "73 or better")
        draft73 = draft[:3] + [{"text": "A 73 or better on the next project gets it to a C.",
                                "cites": ["whatif:art:projects:20"]}]
        story = w.write_class(ScriptedModel({"Art": slot_all(draft73, w.fact_index(exact))}), exact, "Art")
        self.assertEqual((story["story"], story["dropped"]), (" ".join(s["text"] for s in draft73), []))

    def test_example_numbers_and_dates_are_never_markers(self):
        # Review repro: a needed 73.2 says "74 or better", the same words as the example.
        facts = [{"id": "move:geo:2026-10-14", "kind": "grade_move", "class": "Geo", "since": "2026-10-14",
                  "from_pct": 71.5, "to_pct": 69.0, "delta": -2.5, "explained": -2.5, "coverage": 1.0,
                  "coverage_trimmed": False, "causes": [
                      {"id": "cause:geo:2026-10-14:unittest", "title": "Unit Test", "category": "Tests", "bucket": "Tests",
                       "bucket_weight_pct": 50.0, "change": "new", "earned": 20.0, "possible": 40.0, "points": -2.5,
                       "share": -2.5}]},
                 {"id": "whatif:geo:tests:40", "kind": "what_if", "class": "Geo", "bucket": "Tests",
                  "bucket_weight_pct": 50.0, "points": 40.0, "target": 70.0, "needed_pct": 73.2, "reachable": True}]
        mf = w.model_facts(facts)
        self.assertEqual(mf[1]["say_needed"], "74 or better")
        draft = [{"text": mf[0]["say_opening"], "cites": ["move:geo:2026-10-14"]},
                 {"text": "Mostly the Unit Test (20/40).", "cites": ["cause:geo:2026-10-14:unittest"]},
                 {"text": "A 74 or better on the next test gets it to a C.", "cites": ["whatif:geo:tests:40"]}]
        story = w.write_class(ScriptedModel({"Geo": slot_all(draft, w.fact_index(facts))}), facts, "Geo")
        self.assertEqual(story["story"], "Slipped to a D since Oct 14, down 2.5. Mostly the Unit Test (20/40). "
                                         "A 74 or better on the next test gets it to a C.")
        # Review repro: a real move since Sep 9 gets the code's own opener, which must pass.
        sep = [dict(facts[0], id="move:geo:2026-09-09", since="2026-09-09", causes=[])]
        opener = w.model_facts(sep)[0]["say_opening"]
        self.assertEqual(opener, "Slipped to a D since Sep 9, down 2.5.")
        self.assertIsNone(check(opener, ["move:geo:2026-09-09"], w.fact_index(sep), "Geo"))
        for number in ("68.8", "71.1", "73.4"):
            self.assertNotIn("copies", w.check_sentence(f"An {number}% on the next test gets it to a C.",
                                                        ["whatif:geo:tests:40"], w.fact_index(facts)) or "")

    def test_spoken_gaps_are_checked_against_the_real_gap(self):
        bio = [{"id": "next:bio", "kind": "next_letter", "class": "Bio", "pct": 78.6, "letter": "C",
                "next_letter": "B", "threshold": 80.0, "gap": 1.4}]
        idx = w.fact_index(bio)
        for text in ("Still a C, one and a half points from a B.", "Still a C, a point and a half from a B.",
                     "Still a C, a half point from a B.", "Still a C, two and a half points from a B."):
            self.assertIn("isn't in the cited facts", w.check_sentence(text, ["next:bio"], idx) or "", text)
        self.assertEqual(w.spelled_to_digits("one and a half points, a half point, two and a half, 3 and a half"),
                         "1.5 points, 0.5 point, 2.5, 3.5")
        half = [dict(bio[0], pct=79.5, gap=0.5)]
        for text in ("Still a C, a half point from a B.", "Still a C, half a point from a B."):
            self.assertIsNone(check(text, ["next:bio"], w.fact_index(half)), text)
        onehalf = [dict(bio[0], pct=78.5, gap=1.5)]
        self.assertIsNone(check("Still a C, one and a half points from a B.", ["next:bio"],
                                           w.fact_index(onehalf)))
        # Hyphenated spoken numbers are read the same way.
        self.assertEqual(w.spelled_to_digits("one-and-a-half points, a half-point, twenty-one"),
                         "1.5 points, 0.5 point, 21")
        self.assertIn("isn't in the cited facts",
                      w.check_sentence("Still a C, one-and-a-half points from a B.", ["next:bio"], idx) or "")
        self.assertIsNone(check("Still a C, one-and-a-half points from a B.", ["next:bio"],
                                           w.fact_index(onehalf)))
        self.assertIn("isn't in the cited facts",
                      w.check_sentence("Still a C, a half-point from a B.", ["next:bio"], idx) or "")
        self.assertIsNone(check("Still a C, a half-point from a B.", ["next:bio"], w.fact_index(half)))

    def test_review_nits(self):
        geo = w.fact_index(geometry_facts())
        self.assertIn("echoes the prompt", w.check_sentence("Moved from a C to a C.", [GEO_NEXT], geo))
        self.assertEqual(w.check_sentence("Mainly the Unit 2 Test.", [GEO_NEXT], geo),
                         "states a cause without citing a grade_move or cause")
        self.assertIsNone(check("Scores mostly post on Fridays.", [PATTERN], index()))
        # "Moved to" is checked against the move the sentence cites, not any move in the class.
        two = geometry_facts() + [
            {"id": "move:geometry:2026-10-14", "kind": "grade_move", "class": "Geometry", "since": "2026-10-14",
             "from_pct": 81.0, "to_pct": 79.5, "delta": -1.5, "explained": None, "coverage": None,
             "coverage_trimmed": False, "causes": []},
            {"id": "move:geometry:2026-10-20", "kind": "grade_move", "class": "Geometry", "since": "2026-10-20",
             "from_pct": 79.9, "to_pct": 79.5, "delta": -0.4, "explained": None, "coverage": None,
             "coverage_trimmed": False, "causes": []}]
        idx = w.fact_index(two)
        self.assertIsNone(check("Slipped to a C since Oct 14.", ["move:geometry:2026-10-14"], idx))
        self.assertEqual(w.check_sentence("Slipped to a C since Oct 20.", ["move:geometry:2026-10-20"], idx),
                         "says the class moved to C but no move crossed into that letter")
        # An unclosed <think> holds no answer.
        self.assertIsNone(w._json('<think>unclosed {"a": 2}'))
        self.assertEqual(w._json('{"a": 1}<think>more {"b": 2}'), {"a": 1})

    def test_prompt_echo_and_log_wording_are_dropped(self):
        idx = w.fact_index(geometry_facts())
        for text in ("The letter is a C, and the distance to the next letter is 0.5.",
                     "To reach the next letter, the student needs 84.2% on the next assessment.",
                     "The what-if score needed on the next test is 84.2%.",
                     "An 84.2% on the next item of that size gets the B."):
            self.assertIn("echoes the prompt", w.check_sentence(text, [GEO_NEXT, GEO_WHATIF], idx), text)
        chem = w.fact_index(chem_facts())
        self.assertIn("echoes the prompt",
                      w.check_sentence("The grade moved from A to A, a drop of 1.6.", [CHEM_MOVE], chem))


class NoteStyleTests(unittest.TestCase):
    """The good wording the prompt asks for passes the checker."""

    def test_spoken_small_gaps_are_numbers(self):
        self.assertIn("0.5 point", w.spelled_to_digits("Half a point from a B."))
        self.assertIn("0.25 point", w.spelled_to_digits("a quarter point from a B"))
        self.assertIn("1 point", w.spelled_to_digits("a point from a B"))
        self.assertIn("2.5 points", w.spelled_to_digits("two and a half points from a B"))
        self.assertIn("1.5 points", w.spelled_to_digits("a point and a half from a B"))
        idx = w.fact_index(geometry_facts())
        self.assertIsNone(check("Half a point from a B.", [GEO_NEXT], idx))
        self.assertEqual(w.check_sentence("A point and a half from a B.", [GEO_NEXT], idx),
                         "number 1.5 isn't in the cited facts")
        quarter = [dict(geometry_facts()[0], gap=0.25, pct=79.75)]
        self.assertIsNone(check("A quarter point from a B.", [GEO_NEXT], w.fact_index(quarter)))

    def test_the_good_notes_pass(self):
        geo = w.fact_index(geometry_facts())
        self.assertIsNone(check("Half a point from a B.", [GEO_NEXT], geo))
        self.assertIsNone(check("An 85 or better on the next test gets there.", [GEO_WHATIF], geo))
        self.assertIsNone(w.eval_story([{"text": "Half a point from a B.", "cites": [GEO_NEXT]},
                                        {"text": "An 85 or better on the next test gets there.",
                                         "cites": [GEO_WHATIF]}], geo))
        chem = w.fact_index(chem_facts())
        line = "Still an A, down 1.6 since Oct 14, mostly from the Lab 3 Packet (4/10)."
        self.assertIsNone(check(line, [CHEM_MOVE, CHEM_C1], chem))
        self.assertEqual(w.check_sentence("Still an A, down 1.6 since Oct 13.", [CHEM_MOVE], chem), "names a date")
        self.assertEqual(w.model_facts(chem_facts())[0]["since_label"], "Oct 14")


class PolishPassTests(unittest.TestCase):
    """The Oct 22 polish pass: the opening, the one next step, a/an, and concrete headlines."""

    def test_a_story_opens_with_the_letter_and_the_gap_or_move(self):
        geo = w.fact_index(geometry_facts())
        step = {"text": "An 85 or better on the next test gets there.", "cites": [GEO_WHATIF]}
        self.assertEqual(w.eval_story([dict(step)], geo), "doesn't open with the letter and the gap or move")
        self.assertIsNone(w.eval_story([{"text": "Half a point from a B.", "cites": [GEO_NEXT]}, dict(step)], geo))
        self.assertIsNone(w.eval_story([{"text": "Still a C, 0.5 from a B.", "cites": [GEO_NEXT]}, dict(step)], geo))
        # A letter with no gap, or a gap with no letter, isn't the opening.
        self.assertEqual(w.eval_story([{"text": "Still a C.", "cites": [GEO_NEXT]}, dict(step)], geo),
                         "doesn't open with the letter and the gap or move")
        chem = w.fact_index(chem_facts())
        self.assertEqual(w.eval_story([{"text": "Slipped to a 96.3% since Oct 14, down 1.6 points from a 97.9%.",
                                        "cites": [CHEM_MOVE]}], chem),
                         "doesn't open with the letter and the gap or move")
        self.assertIsNone(w.eval_story([{"text": "Still an A, down 1.6 since Oct 14.", "cites": [CHEM_MOVE]}], chem))
        # The opening is backed by a next_letter or the move itself, not by a cause.
        self.assertEqual(w.eval_story([{"text": "Still an A, down 1.6, mostly from the Lab 3 Packet (4/10).",
                                        "cites": [CHEM_C1]}], chem),
                         "doesn't open with the letter and the gap or move")

    def test_a_drop_story_ends_on_its_what_if_step_and_never_invents_one(self):
        self.assertEqual(w.eval_story([dict(GOOD[0]), dict(GOOD[1])], index()),
                         "a drop story must end on its what-if step")
        self.assertIsNone(w.eval_story([dict(s) for s in GOOD], index()))
        # An A has no what-if: the story ends after the cause, with no advice.
        chem = w.fact_index(chem_facts())
        self.assertIsNone(w.eval_story([{"text": "Still an A, down 1.6 since Oct 14, mostly from the Lab 3 Packet (4/10).",
                                         "cites": [CHEM_MOVE, CHEM_C1]}], chem))
        for advice in ("Redoing the Lab 3 Packet would help.", "Focus on the next lab.",
                       "It should come back with steady work.", "Kid A needs to turn in cleaner Warm Ups."):
            self.assertEqual(w.check_sentence(advice, [CHEM_C1], chem), "gives advice no what_if backs", advice)
        self.assertIsNone(check("Aim for an 88% on the next test to get the A back.", [WHATIF], index()))

    def test_talk_of_a_move_needs_one_and_nothing_is_said_about_what_isnt_there(self):
        geo = w.fact_index(geometry_facts())
        self.assertEqual(w.check_sentence("No major assignments explained the move.", [GEO_NEXT], geo),
                         "says what isn't there")
        self.assertEqual(w.check_sentence("Nothing big explains the change.", [GEO_NEXT], geo),
                         "talks about a move with no grade_move cited")
        for line in ("No pattern data available.", "No major cause identified.", "Scores: none identified."):
            self.assertEqual(w.check_sentence(line, [GEO_NEXT], geo), "says what isn't there", line)
        chem = w.fact_index(chem_facts())
        self.assertIsNone(check("The Lab 3 Packet (4/10) drove the drop.", [CHEM_C1], chem))

    def test_the_model_gets_a_ready_opening_and_each_causes_effect(self):
        geo = w.model_facts(geometry_facts())
        self.assertEqual(geo[0]["say_opening"], "Still a C, 0.5 points from a B.")
        chem = w.model_facts(chem_facts())
        self.assertEqual(chem[0]["say_opening"], "Still an A, down 1.6 since Oct 14.")
        self.assertEqual([c["effect"] for c in chem[0]["causes"]], ["hurt", "hurt"])
        crossed = dict(chem_facts()[0], from_pct=80.4, to_pct=78.8)
        self.assertEqual(w.say_opening(w._say_fields(crossed)), "Slipped to a C since Oct 14, down 1.6.")
        risen = dict(chem_facts()[0], from_pct=78.8, to_pct=80.4, delta=1.6, causes=[])
        self.assertEqual(w.say_opening(w._say_fields(risen)), "Up to a B since Oct 14, up 1.6.")
        # The ready openings pass the checker and the opening rule as written.
        gidx, cidx = w.fact_index(geometry_facts()), w.fact_index(chem_facts())
        self.assertIsNone(check(geo[0]["say_opening"], [GEO_NEXT], gidx))
        self.assertIsNone(w.eval_story([{"text": geo[0]["say_opening"], "cites": [GEO_NEXT]},
                                        {"text": "An 85 or better on the next test gets there.", "cites": [GEO_WHATIF]}], gidx))
        self.assertIsNone(check(chem[0]["say_opening"], [CHEM_MOVE], cidx))
        self.assertIsNone(w.eval_story([{"text": chem[0]["say_opening"], "cites": [CHEM_MOVE]}], cidx))
        lab = dict(chem_facts()[0])
        lab["causes"] = lab["causes"] + [{"id": "cause:chem:2026-10-14:rocketlab", "title": "Rocket Lab", "share": 0.4,
                                          "points": 0.8, "earned": 9.0, "possible": 10.0}]
        self.assertEqual(w._say_fields(lab)["causes"][-1]["effect"], "helped")

    def test_a_and_an_follow_how_the_number_or_letter_is_said(self):
        cases = {
            "A 85 or better on the next test gets there.": "An 85 or better on the next test gets there.",
            "It needs a 80 on the quiz.": "It needs an 80 on the quiz.",
            "a 8/10, a 11/20 and a 18.5% so far": "an 8/10, an 11/20 and an 18.5% so far",
            "Still a A, an 97 gets a B back.": "Still an A, a 97 gets a B back.",
            "A F on the quiz.": "An F on the quiz.",
            "a 110 and a 1.5 and a 4": "a 110 and a 1.5 and a 4",
            "Down 1.6. A 11 on the lab helps.": "Down 1.6. An 11 on the lab helps.",
            # The letter A mid-sentence is never read as the article.
            "Still an A 1.6 points down.": "Still an A 1.6 points down.",
            "A B again with an 85 or better.": "A B again with an 85 or better.",
        }
        for raw, fixed in cases.items():
            self.assertEqual(w.fix_articles(raw), fixed, raw)
        drafted = w._sentences(json.dumps({"sentences": [
            {"text": "A 85 or better on the next test gets there.", "cites": [GEO_WHATIF]}]}))
        self.assertEqual(drafted[0]["text"], "An 85 or better on the next test gets there.")
        geo = w.fact_index(geometry_facts())
        self.assertIsNone(check(drafted[0]["text"], drafted[0]["cites"], geo))
        self.assertEqual(w.check_sentence(w.fix_articles("A 84 or better on the next test gets there."), [GEO_WHATIF], geo),
                         "number 84 isn't the needed score or a round-up of it")

    def test_a_headline_cites_a_fact_and_says_something_concrete_or_there_is_none(self):
        vague = ({"text": "Next test scores impact Math significantly.", "cites": [WHATIF]},
                 {"text": "Math: B, 2.1 pts from an A.", "cites": []},
                 {"text": "Math needs the Ch.4 Quiz improved.", "cites": [CAUSE]})
        for headline in vague:
            up = w.write_up(ScriptedModel({"Math": GOOD}, headline=headline), insight_facts())
            self.assertIsNone(up["headline"], headline["text"])
            self.assertEqual(w.apply_write_up({"headline": "Grok headline"}, up)["headline"], "")
        for text, cites, out in (("Math: an 88 or better on the {next:" + WHATIF + "} gets it to an A.", [WHATIF],
                                  "Math: an 88 or better on the next test gets it to an A."),
                                 ("An 88 or better on the {next:" + WHATIF + "} gets it to an A.", [WHATIF],
                                  "An 88 or better on the next test gets it to an A."),
                                 ("{class:" + WHATIF + "} is 2.1 pts from an A; an 88 or better on the {next:" + WHATIF
                                  + "} gets it to an A.", [NEXT, WHATIF],
                                  "Math is 2.1 pts from an A; an 88 or better on the next test gets it to an A.")):
            up = w.write_up(ScriptedModel({"Math": GOOD}, headline={"text": text, "cites": cites}), insight_facts())
            self.assertEqual(up["headline"], {"text": out, "cites": cites})
        # Only the headline forms: a cause line, or the step reworded, is no headline.
        for text, cites in (("The {item:" + CAUSE + "} did most of the damage.", [CAUSE]),
                            ("Math: an 88 or better on the {next:" + WHATIF + "} gets the A back.", [WHATIF])):
            up = w.write_up(ScriptedModel({"Math": GOOD}, headline={"text": text, "cites": cites}), insight_facts())
            self.assertIsNone(up["headline"], text)
        fixed = w.write_up(ScriptedModel({"Math": GOOD}, headline={
            "text": "Math: a 88 or better on the {next:" + WHATIF + "} gets it to an A.", "cites": [WHATIF]}), insight_facts())
        self.assertEqual(fixed["headline"]["text"], "Math: an 88 or better on the next test gets it to an A.")


class CtoRoundFourTests(unittest.TestCase):
    """Headline round-up wording, singular bucket fills, and the next-slot prompt."""

    ALG = "whatif:algllbusmang:assessment:10"

    def alg_facts(self):
        return [{"id": "next:algllbusmang", "kind": "next_letter", "class": "Alg ll Bus Mang", "pct": 79.5, "letter": "C",
                 "next_letter": "B", "threshold": 80.0, "gap": 0.5},
                {"id": self.ALG, "kind": "what_if", "class": "Alg ll Bus Mang", "bucket": "Assessment",
                 "bucket_weight_pct": 50.0, "points": 10.0, "target": 80.0, "needed_pct": 84.3, "reachable": True}]

    def headline(self, text):
        facts = self.alg_facts()
        story = [{"class": "Alg ll Bus Mang", "story": "x", "cites": ["next:algllbusmang", self.ALG]}]
        return w.write_headline(ScriptedModel({}, headline={"text": text, "cites": [self.ALG]}), facts, story)

    def test_a_headline_needed_score_is_the_round_up_or_better(self):
        # GE's v5 survivor, as the model wrote it (slotted) and as it read after the fill.
        self.assertIsNone(self.headline("Alg ll Bus Mang: 85 on {next:%s} for a B." % self.ALG))
        self.assertIsNone(self.headline("Alg ll Bus Mang: 85 on next assessment item for a B."))
        self.assertIsNone(self.headline("Alg ll Bus Mang: 84.3 on the {next:%s} gets a B." % self.ALG))
        self.assertIsNone(self.headline("Alg ll Bus Mang: an 86 or better on the {next:%s} gets it to a B." % self.ALG))
        self.assertIsNone(self.headline("Alg ll Bus Mang: an 85 or better on the {next:%s} gets a B." % self.ALG))
        ok = self.headline("Alg ll Bus Mang: an 85 or better on the {next:%s} gets it to a B." % self.ALG)
        self.assertEqual(ok, {"text": "Alg ll Bus Mang: an 85 or better on the next assessment gets it to a B.",
                              "cites": [self.ALG]})

    def test_next_fills_for_singular_plural_and_mass_buckets(self):
        cases = {"Assessment": "assessment", "Assessments": "assessment", "Summative": "summative", "Tests": "test",
                 "Test": "test", "Quizzes": "quiz", "Labs": "lab", "Projects": "project", "Labs & Activities": "lab or activity",
                 "Coursework": "coursework assignment", "Homework": "homework assignment",
                 "Math Fluency": "math fluency assignment", "Participation": "participation score",
                 "Classwork": "classwork assignment", "Performance": "performance item",
                 "Labs & Projects": "lab or project", "Labs/Activities": "lab or activity",
                 "Homework & Classwork": "homework or classwork assignment",
                 None: "test"}
        for bucket, noun in cases.items():
            self.assertEqual(w._bucket_noun(bucket), noun, bucket)
        idx = w.fact_index(self.alg_facts())
        self.assertEqual(w.fill_slots("An 85 or better on the {next:%s} gets it to a B." % self.ALG, idx),
                         "An 85 or better on the next assessment gets it to a B.")

    def test_the_prompt_puts_what_if_and_keep_ids_in_next_slots(self):
        self.assertIn("{next:", w.DRAFT_SYSTEM)
        self.assertRegex(w.DRAFT_SYSTEM, r"never in an \{item:")
        self.assertIn("{next:whatif:", w.DRAFT_SYSTEM)
        idx = w.fact_index(self.alg_facts())
        self.assertIn("has no title", w.check_sentence("An 85 or better on the {item:%s} gets it to a B." % self.ALG,
                                                       [self.ALG], idx))


KEEP_LAB, KEEP_TEST = "keep:chem:coursework:10", "keep:chem:tests:50"


def chem_keep_facts(cannot_lose=False, keep_id=KEEP_LAB, named=True):
    """Chem (an A that dropped 1.6, mostly on Coursework) plus the engine's one keep_letter fact.

    A can't-drop fact is about a named upcoming item at its posted points (attach_next_items names it);
    named=False leaves it at a typical size with no name, which the writer must not say."""
    bucket, points, needed = ("Coursework", 10.0, 61.3) if keep_id == KEEP_LAB else ("Tests", 50.0, 72.0)
    keep = {"id": keep_id, "kind": "keep_letter", "class": "Chem", "letter": "A", "bucket": bucket,
            "points": points, "needed_pct": needed, "cannot_lose": False}
    if cannot_lose:
        keep.update(needed_pct=None, cannot_lose=True)
        if named:
            keep.update(next_title="Lab 9", next_due="2026-10-23", next_due_label="Fri Oct 23")
    return chem_facts() + [keep]


def biology_facts():
    """GE's repro: Biology at 85.2 (a B), down since Oct 14, a keep_letter fact for the B and next:biology."""
    return [{"id": "next:biology", "kind": "next_letter", "class": "Biology", "pct": 85.2, "letter": "B",
             "next_letter": "A", "threshold": 90.0, "gap": 4.8},
            {"id": "move:biology:2026-10-14", "kind": "grade_move", "class": "Biology", "since": "2026-10-14",
             "from_pct": 87.0, "to_pct": 85.2, "delta": -1.8, "explained": -1.8, "coverage": 1.0,
             "coverage_trimmed": False, "causes": []},
            {"id": "keep:biology:tests:50", "kind": "keep_letter", "class": "Biology", "letter": "B",
             "bucket": "Tests", "points": 50.0, "needed_pct": 53.2, "cannot_lose": False}]


class KeepTheLetterWriterTests(unittest.TestCase):
    """The engine's keep_letter fact on a drop story, on made-up Chem data."""

    OPEN = {"text": "Still an A, down 1.6 since Oct 14.", "cites": [CHEM_MOVE]}
    CAUSE = {"text": "Mostly the {item:%s}." % CHEM_C1, "cites": [CHEM_C1]}
    KEEP = {"text": "A 62 or better on the {next:%s} keeps the A." % KEEP_LAB, "cites": [KEEP_LAB]}

    def test_keep_sentences_are_not_cause_sentences(self):
        # #52's cause-sentence gate leaves keep wording alone: it names no cause and cites no move.
        idx = w.fact_index(chem_keep_facts())
        self.assertIsNone(w.cause_form_reason(self.KEEP["text"], [KEEP_LAB], idx))
        self.assertIsNone(w.check_sentence(self.KEEP["text"], [KEEP_LAB], idx, "Chem"))
        safe = w.fact_index(chem_keep_facts(cannot_lose=True))
        line = "The {next:%s} can't drop it below an A." % KEEP_LAB
        self.assertIsNone(w.cause_form_reason(line, [KEEP_LAB], safe))
        self.assertIsNone(w.check_sentence(line, [KEEP_LAB], safe, "Chem"))
        story = w.write_class(ScriptedModel({"Chem": [self.OPEN, self.CAUSE, self.KEEP]}), chem_keep_facts(), "Chem")
        self.assertEqual(story["dropped"], [])

    def test_the_model_sees_the_engines_keep_fact_with_its_wording(self):
        seen = [f for f in w.model_facts(chem_keep_facts()) if w.is_keep(f)]
        self.assertEqual([f["id"] for f in seen], [KEEP_LAB])
        # The keep wording names the item only through a {next:} slot; code fills it after the checks.
        self.assertEqual(seen[0]["say_keep"], "A 62 or better on the {next:%s} keeps the A." % KEEP_LAB)
        idx = w.fact_index(chem_keep_facts())
        self.assertEqual(w.fill_slots(seen[0]["say_keep"], idx), "A 62 or better on the next coursework assignment keeps the A.")
        safe = [f for f in w.model_facts(chem_keep_facts(cannot_lose=True)) if w.is_keep(f)]
        self.assertEqual(safe[0]["say_keep"], "The {next:%s} can't drop it below an A." % KEEP_LAB)
        self.assertEqual(w.fill_slots(safe[0]["say_keep"], w.fact_index(chem_keep_facts(cannot_lose=True))),
                         "The Lab 9 can't drop it below an A.")
        tests = [f for f in w.model_facts(chem_keep_facts(keep_id=KEEP_TEST)) if w.is_keep(f)]
        self.assertEqual(w.fill_slots(tests[0]["say_keep"], w.fact_index(chem_keep_facts(keep_id=KEEP_TEST))),
                         "A 72 or better on the next test keeps the A.")
        # A named next item of the same bucket and size gives the keep step its title.
        upcoming = [{"class": "Chem", "bucket": "Tests", "title": "Unit 4 Test", "due": "2026-10-28", "points": 50.0}]
        named = w.attach_next_items(chem_keep_facts(keep_id=KEEP_TEST), upcoming)
        pick = w.pick_keep(named)
        self.assertEqual((pick["id"], w.fill_slots(w.say_keep(pick), w.fact_index(named))),
                         (KEEP_TEST, "A 72 or better on the Unit 4 Test keeps the A."))
        # The old what_if-with-target-keep shape is no longer a keep fact.
        self.assertFalse(w.is_keep({"kind": "what_if", "target": "keep"}))

    def test_a_drop_story_with_a_keep_fact_ends_on_it(self):
        idx = w.fact_index(chem_keep_facts())
        self.assertEqual(w.eval_story([dict(self.OPEN), dict(self.CAUSE)], idx), "a drop story must end on its what-if step")
        self.assertIsNone(w.eval_story([dict(self.OPEN), dict(self.CAUSE), dict(self.KEEP)], idx))
        story = w.write_class(ScriptedModel({"Chem": [self.OPEN, self.CAUSE, self.KEEP]}), chem_keep_facts(), "Chem")
        self.assertEqual(story["story"], "Still an A, down 1.6 since Oct 14. Mostly the Lab 3 Packet (4/10). "
                                         "A 62 or better on the next coursework assignment keeps the A.")

    def test_the_checker_takes_a_keep_claim_only_from_a_cited_keep_fact(self):
        idx = w.fact_index(chem_keep_facts())
        self.assertIsNone(w.check_sentence(self.KEEP["text"], [KEEP_LAB], idx, "Chem"))
        self.assertEqual(w.check_sentence("A 62 or better on the next coursework assignment keeps the A.", [CHEM_MOVE], idx, "Chem"),
                         "says the letter holds with no keep_letter fact cited")
        self.assertIn("doesn't cite", w.check_sentence(self.KEEP["text"], [CHEM_MOVE], idx, "Chem"))
        # Free text naming the item is dropped even with the keep fact cited.
        self.assertIn("outside a slot", w.check_sentence("A 62 or better on the next coursework assignment keeps the A.",
                                                         [KEEP_LAB], idx, "Chem"))
        self.assertEqual(w.check_sentence("It stays an A with a 62 or better on the next lab.", [CHEM_C1], idx, "Chem"),
                         "says the letter holds with no keep_letter fact cited")
        # Round up only: the exact ceiling, "or better".
        self.assertEqual(w.check_sentence("A 61 or better on the next coursework assignment keeps the A.", [KEEP_LAB], idx, "Chem"),
                         "number 61 isn't the needed score or a round-up of it")
        self.assertEqual(w.check_sentence("A 63 or better on the next coursework assignment keeps the A.", [KEEP_LAB], idx, "Chem"),
                         "number 63 isn't the needed score or a round-up of it")
        # A keep fact never backs a gets-it-to claim.
        self.assertEqual(w.check_sentence("A 62 or better on the next lab gets it up to an A.", [KEEP_LAB], idx, "Chem"),
                         "a keep_letter fact never backs a gets-it-to claim")
        self.assertEqual(w.check_sentence("The next coursework assignment can't drop it below an A.", [KEEP_LAB], idx, "Chem"),
                         "says the next item can't cost the letter, but the keep_letter fact needs a score")
        # The letter kept is the keep fact's, the class's current letter.
        self.assertIn("current letter", w.check_sentence("A 62 or better on the next coursework assignment keeps the B.",
                                                         [KEEP_LAB], idx, "Chem"))

    def test_ge_biology_repro_keep_never_backs_gets_it_to(self):
        idx = w.fact_index(biology_facts())
        self.assertEqual(w.check_sentence("A 54 or better on the next test gets it to an A.",
                                          ["keep:biology:tests:50", "next:biology"], idx, "Biology"),
                         "a keep_letter fact never backs a gets-it-to claim")
        self.assertIsNone(w.check_sentence("A 54 or better on the {next:keep:biology:tests:50} keeps the B.",
                                           ["keep:biology:tests:50"], idx, "Biology"))
        self.assertIn("current letter", w.check_sentence("A 54 or better on the next test keeps the A.",
                                                         ["keep:biology:tests:50"], idx, "Biology"))
        self.assertEqual(w.fill_slots(w.model_facts(biology_facts())[2]["say_keep"], idx),
                         "A 54 or better on the next test keeps the B.")

    def test_physics_bar_story_passes_on_synthetic_facts(self):
        phys = [{"id": "move:p", "kind": "grade_move", "class": "Physics & Eng", "since": "2026-10-14",
                 "from_pct": 98.1, "to_pct": 96.5, "delta": -1.6, "explained": -1.73, "coverage": 1.08,
                 "coverage_trimmed": False, "causes": [
                     {"id": "cause:p:packet", "title": "Unit 2 Packet", "bucket": "Coursework", "bucket_weight_pct": 5.0,
                      "change": "new", "earned": 4.0, "possible": 10.0, "points": -0.79, "share": -1.36},
                     {"id": "cause:p:lab", "title": "Balloon Rocket Lab", "bucket": "Coursework", "bucket_weight_pct": 5.0,
                      "change": "new", "earned": 9.0, "possible": 10.0, "points": 0.77, "share": 0.38}]},
                {"id": "keep:p:assessments:10", "kind": "keep_letter", "class": "Physics & Eng", "letter": "A",
                 "bucket": "Assessments", "points": 10.0, "needed_pct": 74.6, "cannot_lose": False}]
        mf = w.model_facts(phys)
        self.assertEqual(mf[1]["say_keep"], "A 75 or better on the {next:keep:p:assessments:10} keeps the A.")
        draft = [{"text": mf[0]["say_opening"], "cites": ["move:p"]},
                 {"text": "The {item:cause:p:packet} did most of the damage.", "cites": ["cause:p:packet"]},
                 {"text": "The {item:cause:p:lab} helped.", "cites": ["cause:p:lab"]},
                 {"text": mf[1]["say_keep"], "cites": ["keep:p:assessments:10"]}]
        story = w.write_class(ScriptedModel({"Physics & Eng": draft}), phys, "Physics & Eng")
        self.assertEqual(story["story"], "Still an A, down 1.6 since Oct 14. The Unit 2 Packet (4/10) did most of the damage. "
                                         "The Balloon Rocket Lab (9/10) helped. A 75 or better on the next assessment keeps the A.")
        self.assertEqual(story["dropped"], [])
        # It must end on the keep step, which names a score.
        self.assertEqual(w.eval_story([dict(s) for s in draft[:3]], w.fact_index(phys)),
                         "a drop story must end on its what-if step")

    def test_a_next_item_that_cannot_cost_the_letter_is_optional_and_plain(self):
        idx = w.fact_index(chem_keep_facts(cannot_lose=True))
        line = {"text": "The {next:%s} can't drop it below an A." % KEEP_LAB, "cites": [KEEP_LAB]}
        self.assertIsNone(w.check_sentence(line["text"], line["cites"], idx, "Chem"))
        self.assertEqual(w.check_sentence("The next coursework assignment can't drop it below an A.", [CHEM_MOVE], idx, "Chem"),
                         "says the letter holds with no keep_letter fact cited")
        self.assertEqual(w.check_sentence("A 62 or better on the next coursework assignment keeps the A.", [KEEP_LAB], idx, "Chem"),
                         "says a score keeps the letter, but the keep_letter fact has no needed score")
        # No item can cost the A, so no step is owed.
        self.assertIsNone(w.eval_story([dict(self.OPEN), dict(self.CAUSE)], idx))
        self.assertIsNone(w.eval_story([dict(self.OPEN), dict(self.CAUSE), dict(line)], idx))

    def test_an_a_with_no_keep_fact_ends_on_the_cause_and_keep_alone_is_no_story(self):
        self.assertIsNone(w.eval_story([dict(self.OPEN), dict(self.CAUSE)], w.fact_index(chem_facts())))
        steady = [f for f in chem_keep_facts() if f.get("kind") != "grade_move"]
        self.assertFalse(w.worth_saying(steady))
        self.assertIsNone(w.pick_keep(steady))
        self.assertEqual([f for f in w.model_facts(steady) if w.is_keep(f)], [])
        only_keep = [f for f in chem_keep_facts() if w.is_keep(f)]
        self.assertFalse(w.worth_saying(only_keep))
        self.assertEqual(w.eval_story([dict(self.KEEP)], w.fact_index(only_keep)),
                         "cites no grade_move, what_if or pattern fact")

def geo_facts(title="Unit Test", bucket="Tests", cls="Geo"):
    """GE's Geo repro: a D after a 2.5 drop, all on one cause; a what-if and a Thursday pattern."""
    return [{"id": "next:geo", "kind": "next_letter", "class": cls, "pct": 69.0, "letter": "D", "next_letter": "C",
             "threshold": 70.0, "gap": 1.4},
            {"id": "move:geo", "kind": "grade_move", "class": cls, "since": "2026-10-14", "from_pct": 71.5,
             "to_pct": 69.0, "delta": -2.5, "explained": -2.5, "coverage": 1.0, "coverage_trimmed": False,
             "causes": [{"id": "cause:geo:t", "title": title, "bucket": bucket, "bucket_weight_pct": 50.0,
                         "earned": 20.0, "possible": 40.0, "points": -2.5, "share": -2.5}]},
            {"id": "whatif:geo", "kind": "what_if", "class": cls, "bucket": bucket, "bucket_weight_pct": 50.0,
             "points": 40.0, "target": 70.0, "needed_pct": 73.2, "reachable": True},
            {"id": "pattern:posting_day:geo", "kind": "posting_day", "class": cls, "weekday": "Thursday", "share": 0.8,
             "scores": 10, "window_days": 28}]


class SlotRoundThreeTests(unittest.TestCase):
    """{bucket:} slots, class names from the student's own classes, neutral-token checks, id leaks, hygiene."""

    def idx(self, **kw):
        return w.fact_index(geo_facts(**kw))

    def test_bucket_slot_is_cited_carries_a_bucket_and_is_cause_wording_like_any_other(self):
        idx = self.idx(bucket="Homework")
        line = "{bucket:cause:geo:t} scores did most of the damage."
        self.assertIsNone(w.check_sentence(line, ["cause:geo:t"], idx, "Geo"))
        self.assertEqual(w.fill_slots(line, idx), "Homework scores did most of the damage.")
        self.assertIn("doesn't cite", w.check_sentence(line, ["move:geo"], idx, "Geo"))
        self.assertEqual(w.check_sentence("Mostly {bucket:move:geo}.", ["move:geo"], idx, "Geo"),
                         "bucket slot move:geo has no bucket")
        # Cause wording on a what_if's bucket still needs a grade_move or cause.
        self.assertEqual(w.check_sentence("{bucket:whatif:geo} did most of the damage.", ["whatif:geo"], idx, "Geo"),
                         "states a cause without citing a grade_move or cause")
        # Off the say_step's words ("the next {bucket:} score" for "the {next:}"): not an allowed form.
        self.assertEqual(w.check_sentence("A 74 or better on the next {bucket:whatif:geo} score gets it to a C.",
                                          ["whatif:geo"], idx, "Geo"), w.FORM_REASON)
        self.assertEqual(w.fill_slots("A 74 or better on the next {bucket:whatif:geo} score gets it to a C.", idx),
                         "A 74 or better on the next homework score gets it to a C.")
        # Share order: "mostly" on a bucket that isn't the top bucket by net share is dropped (by the gate).
        facts = geo_facts(bucket="Homework")
        facts[1]["causes"].append({"id": "cause:geo:q", "title": "Quiz 2", "bucket": "Quizzes", "bucket_weight_pct": 20.0,
                                   "earned": 5.0, "possible": 10.0, "points": -0.5, "share": -0.4})
        idx2 = w.fact_index(facts)
        self.assertEqual(w.check_sentence("Mostly {bucket:cause:geo:q} scores.", ["cause:geo:q"], idx2, "Geo"),
                         w.CAUSE_FORM_REASON)
        self.assertIsNone(w.check_sentence("Mostly {bucket:cause:geo:t} scores.", ["cause:geo:t"], idx2, "Geo"))
        self.assertIn("outside a slot", w.check_sentence("Homework scores did most of the damage.", ["cause:geo:t"], idx, "Geo"))

    def test_subject_check_uses_the_students_own_classes(self):
        idx = self.idx()
        classes = ["Geo", "Physics & Eng", "English 10"]
        self.assertEqual(w.check_sentence("Physics scores usually post Thursdays.", ["pattern:posting_day:geo"], idx, "Geo",
                                          classes), "names another class (Physics)")
        self.assertEqual(w.check_sentence("English 10 scores usually post Thursdays.", ["pattern:posting_day:geo"], idx,
                                          "Geo", classes), "names another class (English 10)")
        for text, cites in (("Still a D, 1.4 points from a C, by the numbers in plain English.", ["next:geo"]),
                            ("Scores usually post Thursdays, state of the art.", ["pattern:posting_day:geo"]),
                            ("The grade health: still a D, 1.4 points from a C.", ["next:geo"]),
                            ("Its posting history says Thursdays.", ["pattern:posting_day:geo"]),
                            ("Still a D, 1.4 points from a C, in the low band.", ["next:geo"])):
            self.assertIsNone(claims(text, cites, idx, "Geo", classes + ["Art & Design", "Band"]), text)

    def test_own_class_name_and_unambiguous_short_form_pass(self):
        cases = (("Geometry H", "Geo scores usually post Thursdays.", ["Geometry H", "Physics"]),
                 ("AP Calculus AB", "Calc scores usually post Thursdays.", ["AP Calculus AB"]),
                 ("Biology Lab", "Biology Lab scores usually post Thursdays.", ["Biology Lab"]),
                 ("Art History", "Art History scores usually post Thursdays.", ["Art History"]),
                 ("Geo", "In Geo, scores usually post Thursdays.", ["Geo"]))
        for cls, text, classes in cases:
            idx = self.idx(cls=cls)
            self.assertIsNone(claims(text, ["pattern:posting_day:geo"], idx, cls, classes), text)
        # "Eng" is short for both Int Eng&Marktg and English 10: ambiguous, so dropped.
        idx = self.idx(cls="Int Eng&Marktg")
        self.assertEqual(w.check_sentence("Eng scores usually post Thursdays.", ["pattern:posting_day:geo"], idx,
                                          "Int Eng&Marktg", ["Int Eng&Marktg", "English 10"]),
                         "names a class by an ambiguous short form (Eng)")
        self.assertIsNone(claims("Int Eng&Marktg scores usually post Thursdays.", ["pattern:posting_day:geo"],
                                 idx, "Int Eng&Marktg", ["Int Eng&Marktg", "English 10"]))

    def test_checks_read_slots_as_neutral_tokens(self):
        for title in ("Final Exam - B+ curve", "Ch 5 Test due 10/30", "Make-up for 11/20", "Unit 3: Part 2", "Quiz 3 (20 pts)"):
            idx = self.idx(title=title)
            line = "The {item:cause:geo:t} did most of the damage."
            self.assertIsNone(w.check_sentence(line, ["cause:geo:t"], idx, "Geo"), title)
        idx = self.idx(title="Final Exam - B+ curve")
        self.assertEqual(w.neutral_slots("The {item:cause:geo:t} did most of the damage.", idx),
                         "The Zitem (20/40) did most of the damage.")
        # What the model writes outside the slots is still checked.
        self.assertIsNotNone(w.check_sentence("The {item:cause:geo:t} cost a B+.", ["cause:geo:t"], idx, "Geo"))
        self.assertEqual(w.check_sentence("The {item:cause:geo:t} (21/40) hurt.", ["cause:geo:t"], idx, "Geo"),
                         "score 21/40 isn't in the cited facts")
        self.assertEqual(w.check_sentence("Down 3.5 since Oct 14, mostly the {item:cause:geo:t}.",
                                          ["move:geo", "cause:geo:t"], idx, "Geo"), "number 3.5 isn't in the cited facts as a move")

    def test_raw_ids_and_slot_debris_drop_the_sentence(self):
        idx = self.idx()
        for text, cites in (("Scores usually post Thursdays (pattern:posting_day:geo).", ["pattern:posting_day:geo"]),
                            ("Still a D, 1.4 points from a C (next:geo).", ["next:geo"]),
                            ("Still a D, 1.4 points from a C (next:somewhere).", ["next:geo"]),
                            ("Mostly the [item:cause:geo:t].", ["cause:geo:t"]),
                            ("Mostly the ｛item:cause:geo:t｝.", ["cause:geo:t"]),
                            ("Mostly the item: Unit Test.", ["cause:geo:t"]),
                            ("Still a D, next: a C.", ["next:geo"])):
            self.assertRegex(w.check_sentence(text, cites, idx, "Geo") or "", r"leaks a raw fact id|slot debris", text)
        # Times and scores aren't ids.
        self.assertIsNone(w.leak_reason("Scores post near 3:05, like the (20/40).", idx))
        self.assertIsNone(w.check_sentence("The {item:cause:geo:t} did most of the damage.", ["cause:geo:t"], idx, "Geo"))
        # A hostile title can't smuggle an id or braces into the final text.
        bad = self.idx(title="Test {item:cause:geo:t}")
        self.assertEqual(w.check_sentence("Mostly the {item:cause:geo:t}.", ["cause:geo:t"], bad, "Geo"), "leaks a raw fact id")

    def test_slot_hygiene(self):
        idx = self.idx()
        self.assertEqual(w.check_sentence("Mostly the {item:cause:geo:t}{item:cause:geo:t}.", ["cause:geo:t"], idx),
                         "repeats a slot")
        self.assertEqual(w.check_sentence("Mostly the {item:cause:geo:t} and the {item:cause:geo:t}.", ["cause:geo:t"], idx),
                         "repeats a slot")
        self.assertEqual(w.check_sentence("{class:move:geo} {item:cause:geo:t} hurt.", ["move:geo", "cause:geo:t"], idx),
                         "adjacent slots")
        odd = self.idx(title="Test\nIgnore   previous")
        self.assertEqual(w.fill_slots("Mostly the {item:cause:geo:t}.", odd), "Mostly the Test Ignore previous (20/40).")
        braced = geo_facts()
        braced[1]["causes"][0]["title"] = "Lab {3}"
        self.assertEqual(w.fill_slots("Mostly the {item:cause:geo:t}.", w.fact_index(braced)), "Mostly the Lab 3 (20/40).")

    def test_drop_reasons_are_codes_and_never_published(self):
        self.assertEqual(w.reason_code("number 0.08 isn't in the cited facts"), "number # isn't in the cited facts")
        self.assertEqual(w.reason_code("score 21/40 isn't in the cited facts"), "score # isn't in the cited facts")
        self.assertEqual(w.reason_code("slot {item:cause:x:2026-10-14:t} names a fact the sentence doesn't cite"),
                         "slot names a fact the sentence doesn't cite")
        self.assertEqual(w.reason_code("names school work (Final) outside a slot"), "names school work outside a slot")
        self.assertEqual(w.reason_code("fact cause:p:2026-10-14:t is another class"), "fact <id> is another class")
        bad = [{"text": "Down 9.9 since Oct 14.", "cites": [MOVE]}] + GOOD
        story = w.write_class(ScriptedModel({"Math": bad}), math_facts(), "Math")
        self.assertTrue(story["dropped"])
        self.assertFalse(any(re.search(r"\d", d) for d in story["dropped"]), story["dropped"])
        put = mock.Mock()
        up = {"headline": None, "stories": [dict(story)]}
        w.publish_write_up([up], put, env={"GRADE_INSIGHTS_PUBLISH": "1"})
        sent = put.call_args[0][0][0]["stories"][0]
        self.assertEqual(sorted(sent), ["cites", "class", "story"])


class HeadlineAndTakeoverTests(unittest.TestCase):
    def test_write_up_headline_and_stories(self):
        model = ScriptedModel({"Math": GOOD}, headline={
            "text": "Math is 2.1 pts from an A; an 88 or better on the {next:" + WHATIF + "} gets it to an A.",
            "cites": [NEXT, WHATIF]})
        up = w.write_up(model, insight_facts())
        self.assertEqual([s["class"] for s in up["stories"]], ["Math"])
        self.assertEqual(up["headline"]["cites"], [NEXT, WHATIF])
        self.assertEqual(model.calls.count(w.DRAFT_SYSTEM), 1)

    def test_a_headline_that_restates_or_cites_outside_the_stories_is_dropped(self):
        for headline in ({"text": "Math has 2 missing assignments.", "cites": [WHATIF]},
                         {"text": "Biology: B, 6 pts from an A.", "cites": ["next:biology"]},
                         {"text": "Math: B, 2.1 pts from an A.", "cites": [NEXT]}):
            up = w.write_up(ScriptedModel({"Math": GOOD}, headline=headline), insight_facts())
            self.assertIsNone(up["headline"], headline["text"])

    def test_the_write_up_takes_the_headline_and_every_story_slot(self):
        summary = {"headline": "Grok headline", "focus_tonight": "Math: Worksheet", "classes": [
            {"class_name": "Math", "story": "Grok story", "snap": "s"},
            {"class_name": "Biology", "story": "Grok filler", "snap": "t"},
        ]}
        local = {"headline": {"text": "Local headline", "cites": [NEXT]},
                 "stories": [{"class": "Math", "story": "Local story", "cites": [WHATIF]}]}
        out = w.apply_write_up(summary, local)
        self.assertEqual(out["headline"], "Local headline")
        self.assertEqual([c["story"] for c in out["classes"]], ["Local story", ""])
        self.assertEqual(out["focus_tonight"], "Math: Worksheet")
        self.assertEqual(summary["headline"], "Grok headline")
        # No local headline means no headline at all: never the model's, never its status line.
        bare = w.apply_write_up({**summary, "status": "Grok status"}, {"headline": None, "stories": []})
        self.assertEqual(bare["headline"], "")
        self.assertNotIn("status", bare)


class PatternFromLogTests(unittest.TestCase):
    def test_the_scored_log_adds_posting_day_patterns(self):
        log = [{"class": "Art", "first_seen": f"2026-10-{d:02d}", "scrape": "evening", "baseline": False}
               for d in (2, 9, 16, 2, 9, 16, 9, 16, 13)]
        facts = w.merged_facts({"as_of": "2026-10-22", "facts": []}, log)
        self.assertEqual([(f["id"], f["weekday"]) for f in facts], [("pattern:posting_day:art", "Friday")])
        self.assertEqual(w.merged_facts({"as_of": "2026-10-22", "facts": facts}, log), facts)


class PublisherTests(unittest.TestCase):
    def test_off_unless_the_flag_is_set(self):
        put = mock.Mock()
        self.assertFalse(w.publish_write_up({"stories": []}, put, env={}))
        self.assertFalse(w.publish_write_up({"stories": []}, put, env={"GRADE_INSIGHTS_PUBLISH": "true"}))
        put.assert_not_called()
        self.assertTrue(w.publish_write_up({"stories": []}, put, env={"GRADE_INSIGHTS_PUBLISH": "1"}))
        put.assert_called_once()


class WriterWorkTests(unittest.TestCase):
    def test_steps_are_saved_and_never_redone(self):
        model = ScriptedModel({"Math": GOOD})
        put = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            work = w.WriterWork(lambda: [{"insight_facts": insight_facts()}], model, tmp, put=put, env={})
            steps = work.plan("2026-10-21")
            self.assertEqual(steps, ["student:0", "publish"])
            for step in steps:
                work.run_step("2026-10-21", step)
            calls = len(model.calls)
            work.run_step("2026-10-21", "student:0")
            self.assertEqual(len(model.calls), calls)
            saved = json.loads((Path(tmp) / "2026-10-21" / "student-0.json").read_text())
            self.assertEqual(saved["stories"][0]["class"], "Math")
            self.assertEqual(json.loads((Path(tmp) / "2026-10-21" / "publish.json").read_text()),
                             {"published": False})
        put.assert_not_called()


class FoldedCausesAndKeepFactTests(unittest.TestCase):
    """#54's fields: other_share/other_causes are never a number to say; keep_letter facts stay hidden here."""

    def folded(self, other_share):
        move = {**math_facts()[1], "other_causes": 3, "other_share": other_share}
        return [math_facts()[0], move] + math_facts()[2:]

    def test_other_share_and_other_causes_are_never_a_claim(self):
        idx = w.fact_index(self.folded(6.44))
        self.assertIn("isn't in the cited facts",
                      w.check_sentence("The other 3 scores account for 6.44 of it.", [MOVE], idx))
        self.assertIn("isn't in the cited facts", w.check_sentence("Down 6.44 since Oct 15.", [MOVE], idx))
        # GE's repro on #54 e0573ab: one folded cause with a 0.08 share.
        move = {**math_facts()[1], "other_causes": 1, "other_share": 0.08}
        idx1 = w.fact_index([math_facts()[0], move] + math_facts()[2:])
        self.assertIsNotNone(w.check_sentence("The rest, 0.08, came from 1 other scores.", [MOVE], idx1))
        self.assertIsNotNone(w.check_sentence("The rest, 0.08, came from 1 other scores.", [MOVE], idx))
        # An all-new night: other_share is null and nothing breaks.
        facts = self.folded(None)
        sent = w.model_facts(facts)[1]
        self.assertEqual((sent["other_share"], sent["other_causes"]), (None, 3))
        story = w.write_class(ScriptedModel({"Math": GOOD}), facts, "Math")
        self.assertEqual(story["story"], GOOD_STORY)

    def test_keep_a_sends_the_keep_fact_only_on_a_drop_and_in_slot_form(self):
        """keep-a re-enables keep_letter for the model: pick_keep's one fact, on a drop story only."""
        keep = {"id": "keep:math:tests:50", "kind": "keep_letter", "class": "Math", "letter": "B", "bucket": "Tests",
                "points": 50.0, "needed_pct": 41.2, "cannot_lose": False}
        facts = math_facts() + [keep]
        sent = [f for f in w.model_facts(facts) if f["kind"] == "keep_letter"]
        self.assertEqual([f["id"] for f in sent], [keep["id"]] if w.pick_keep(facts) else [])
        self.assertFalse(w.worth_saying([keep]))
        steady = [f for f in facts if f.get("kind") != "grade_move"]
        self.assertEqual([f for f in w.model_facts(steady) if f["kind"] == "keep_letter"], [])

    @unittest.skipUnless(hasattr(insights, "keep_fact"), "needs #54's keep_letter engine")
    def test_engine_keep_fact_reaches_the_prompt_on_a_drop_with_slot_wording(self):
        from test_insights import POG_ROWS, POG_TOTALS, class_input, snapshot, TODAY

        prior = snapshot("2026-10-21", {"Biology": (91.8, "A")})
        cls = class_input("Biology", POG_TOTALS, POG_ROWS, 85.2, "B", new_keys=["unit2test"])
        facts = insights.class_facts(cls, [prior], prior, TODAY)
        sent = []

        class Spy:
            def complete(self, system, user):
                sent.append(user)
                return json.dumps({"sentences": [], "backed": []})

        w.write_class(Spy(), facts, "Biology")
        prompt = json.loads(sent[0])
        keeps = [f for f in prompt["facts"] if f["kind"] == "keep_letter"]
        self.assertEqual(len(keeps), 1)
        self.assertEqual(keeps[0]["say_keep"], "A 54 or better on the {next:%s} keeps the B." % keeps[0]["id"])
        idx = w.fact_index(w.facts_for(facts, "Biology"))
        self.assertIsNone(w.check_sentence(keeps[0]["say_keep"], [keeps[0]["id"]], idx, "Biology"))
        self.assertEqual(w.fill_slots(keeps[0]["say_keep"], idx), "A 54 or better on the next test keeps the B.")


def geo_rise_facts():
    """Geo up 2.5 since Oct 14, all on one cause that helped: still a D."""
    facts = geo_facts()
    facts[1].update({"from_pct": 66.5, "delta": 2.5, "explained": 2.5})
    facts[1]["causes"][0].update({"points": 2.5, "share": 2.5})
    return facts


def geo_mixed_facts():
    """Geo down 2.5: the Unit Test hurt (-3.0) and the Lab helped (+0.5)."""
    facts = geo_facts()
    facts[1]["causes"] = [
        {"id": "cause:geo:t", "title": "Unit Test", "bucket": "Tests", "bucket_weight_pct": 50.0,
         "earned": 14.0, "possible": 40.0, "points": -3.0, "share": -3.0},
        {"id": "cause:geo:l", "title": "Density Lab", "bucket": "Labs", "bucket_weight_pct": 20.0,
         "earned": 10.0, "possible": 10.0, "points": 0.5, "share": 0.5}]
    return facts


class DirectionTests(unittest.TestCase):
    """GE's must-fix on #52 8d2bd63: a direction verb must match the sign of the share on the cause it names."""

    WRONG = "says a cause pushed the grade the other way from its share"
    UP = ("The {item:cause:geo:t} helped.", "The {item:cause:geo:t} lifted it.", "{bucket:cause:geo:t} scores helped.",
          "{bucket:cause:geo:t} scores boosted it.", "The {item:cause:geo:t} raised it.", "The {item:cause:geo:t} saved it.",
          "Lifted by the {item:cause:geo:t}.", "The {item:cause:geo:t} pulled it up.")
    DOWN = ("The {item:cause:geo:t} hurt.", "The {item:cause:geo:t} dragged it down.",
            "{bucket:cause:geo:t} scores dragged it.", "The {item:cause:geo:t} did most of the damage.",
            "{bucket:cause:geo:t} did most of the damage.", "The {item:cause:geo:t} cost it 2.5.",
            "The {item:cause:geo:t} pulled it down.", "The {item:cause:geo:t} knocked it down.",
            "{bucket:cause:geo:t} scores sank it.")

    def check_all(self, facts, texts, expected, cites=("cause:geo:t",)):
        # The word checks back up the cause-sentence gate, so they're read with the gate set aside.
        idx = w.fact_index(w.model_facts(facts))
        for text in texts:
            got = claims(text, list(cites), idx, "Geo")
            if expected == self.WRONG:
                # "Pulled it up" on a drop is also a move-direction word the cited move doesn't back.
                self.assertIn(got, (self.WRONG, "says the grade moved a way no cited move did"), text)
            else:
                self.assertEqual(got, expected, text)

    def test_on_a_drop_a_cause_never_helped(self):
        self.check_all(geo_facts(), self.UP, self.WRONG)
        self.check_all(geo_facts(), self.DOWN, None)
        self.check_all(geo_facts(), ("Mostly the {item:cause:geo:t}.",), None, ("move:geo", "cause:geo:t"))

    def test_on_a_rise_a_cause_never_hurt(self):
        self.check_all(geo_rise_facts(), self.DOWN, self.WRONG)
        self.check_all(geo_rise_facts(), self.UP, None)

    def test_a_negated_verb_turns_around(self):
        self.check_all(geo_facts(), ("The {item:cause:geo:t} didn't help.",), None)
        self.check_all(geo_rise_facts(), ("The {item:cause:geo:t} didn't help.",), self.WRONG)

    def test_each_verb_is_read_against_its_own_slot(self):
        both = ("cause:geo:t", "cause:geo:l")
        self.check_all(geo_mixed_facts(), ("Mostly the {item:cause:geo:t}, but the {item:cause:geo:l} helped.",
                                           "The {item:cause:geo:t} hurt, while {bucket:cause:geo:l} scores helped."),
                       None, both)
        self.check_all(geo_mixed_facts(), ("The {item:cause:geo:l} hurt, while the {item:cause:geo:t} helped.",
                                           "The {item:cause:geo:t} hurt, while {bucket:cause:geo:l} scores dragged it."),
                       self.WRONG, both)

    def test_the_drop_reason_is_a_code(self):
        idx = w.fact_index(w.model_facts(geo_facts()))
        reason = w.check_sentence("The {item:cause:geo:t} helped.", ["cause:geo:t"], idx, "Geo")
        self.assertEqual(w.reason_code(reason), reason)
        story = w.write_class(ScriptedModel({"Geo": [
            {"text": "Slipped to a D since Oct 14, down 2.5.", "cites": ["move:geo"]},
            {"text": "The {item:cause:geo:t} helped.", "cites": ["cause:geo:t"]},
            {"text": "A 74 or better on the {next:whatif:geo} gets it to a C.", "cites": ["whatif:geo"]}]}),
            geo_facts(), "Geo")
        self.assertEqual(story["dropped"], [self.WRONG])
        self.assertNotIn("helped", story["story"])

    def test_the_made_up_example_still_passes(self):
        idx = w.fact_index(w.model_facts(w.EXAMPLE_FACTS))
        for sentence in w.EXAMPLE_NOTE:
            self.assertIsNone(w.check_sentence(sentence["text"], sentence["cites"], idx, "Ceramics"), sentence["text"])


def geo2_facts(delta=-2.5, neg=-3.0, pos=0.5, zero=False):
    """GE's dir958b fixture: Geo moves by delta; cause a (Unit Test) has share neg, b (Rocket Lab) share pos."""
    causes = [{"id": "cause:g:a", "title": "Unit Test", "bucket": "Tests", "bucket_weight_pct": 50.0, "earned": 20.0,
               "possible": 40.0, "points": neg, "share": neg},
              {"id": "cause:g:b", "title": "Rocket Lab", "bucket": "Labs", "bucket_weight_pct": 30.0, "earned": 9.0,
               "possible": 10.0, "points": pos, "share": pos}]
    if zero:
        causes.append({"id": "cause:g:z", "title": "Map Quiz", "bucket": "Quizzes", "bucket_weight_pct": 20.0,
                       "earned": 5.0, "possible": 10.0, "points": 0.0, "share": 0.0})
    return [{"id": "next:g", "kind": "next_letter", "class": "Geo", "pct": 69.0, "letter": "D", "next_letter": "C",
             "threshold": 70.0, "gap": 1.0},
            {"id": "move:g", "kind": "grade_move", "class": "Geo", "since": "2026-10-14", "from_pct": 69.0 - delta,
             "to_pct": 69.0, "delta": delta, "explained": delta, "coverage": 1.0, "causes": causes},
            {"id": "whatif:g", "kind": "what_if", "class": "Geo", "bucket": "Tests", "bucket_weight_pct": 50.0,
             "points": 40.0, "target": 70.0, "needed_pct": 73.2, "reachable": True},
            {"id": "pat:g", "kind": "posting_day", "class": "Geo", "weekday": "Thursday", "share": 0.8, "scores": 10,
             "window_days": 28}]


def gate_check(text, facts, classes=("Geo",)):
    """check_sentence as dir958b runs it: the move plus every cause the sentence slots."""
    idx = w.fact_index(w.model_facts(facts))
    cites = ["move:g"] + sorted(set(re.findall(r"\{(?:item|bucket):(cause:[^}]+)\}", text)))
    return w.check_sentence(text, cites, idx, "Geo", list(classes))


class CauseFormGateTests(unittest.TestCase):
    """GE's dir958b on 958eb47: wrong-way words off any list got through. Cause sentences now fail closed."""

    DROP_WRONG_WAY = (
        "The {item:cause:g:b} pushed it down.", "The {item:cause:g:a} pushed it up.", "The {item:cause:g:b} brought it down.",
        "The {item:cause:g:a} improved it.", "The {item:cause:g:b} tanked it.", "The {item:cause:g:b} cut it.",
        "The {item:cause:g:b} sunk it.", "The {item:cause:g:b} was behind the drop.", "The {item:cause:g:b} is why it fell.",
        "It fell thanks to the {item:cause:g:b}.", "Blame the {item:cause:g:b}.", "The {item:cause:g:a} carried it.",
        "The {item:cause:g:a} rescued it.", "The {item:cause:g:a} saved it.", "A strong {item:cause:g:a} kept it up.",
        "A weak {item:cause:g:b} did it.", "A low {item:cause:g:b} score dropped it.",
        "The {item:cause:g:a} helped but the {item:cause:g:b} hurt.", "The {item:cause:g:b} hurt but the {item:cause:g:a} helped.",
        "The {item:cause:g:a} didn't hurt.", "The {item:cause:g:b} didn't help.", "The {item:cause:g:a} did not drag it down.",
        "The {item:cause:g:a} never hurt.", "The {item:cause:g:a} wasn't what hurt; it helped.",
        "The {item:cause:g:b} and the {item:cause:g:a} both hurt.", "The {item:cause:g:a} and the {item:cause:g:b} both hurt.",
        "Both the {item:cause:g:b} and the {item:cause:g:a} dragged it.", "The {item:cause:g:b} did most of the damage.",
        "The {item:cause:g:b}, a big score after a long week, really ended up being what hurt.",
        "{bucket:cause:g:b} scores dragged it.", "{bucket:cause:g:a} scores lifted it.", "The {item:cause:g:b} cost it 0.5.",
        "The {item:cause:g:a} hardly hurt.", "It was lifted by nothing but the {item:cause:g:a}.",
        "The {item:cause:g:a} made the difference upward.", "Down 2.5, mostly the {item:cause:g:b}.",
        "Mostly the {item:cause:g:b}.", "The {item:cause:g:b} drove the drop.", "The {item:cause:g:b} was the drag.",
        "The {item:cause:g:a} gave it a boost.")
    RISE_WRONG_WAY = ("The {item:cause:g:a} lifted it.", "The {item:cause:g:b} hurt.", "The {item:cause:g:b} did the damage.",
                      "Rose 2.5, but the {item:cause:g:b} dragged.", "A low {item:cause:g:b} score helped.")

    def test_every_dir958b_wrong_way_sentence_drops(self):
        self.assertEqual(len(self.DROP_WRONG_WAY), 40)
        for text in self.DROP_WRONG_WAY:
            self.assertIsNotNone(gate_check(text, geo2_facts()), text)
        rise = geo2_facts(delta=2.5, neg=-0.5, pos=3.0)
        for text in self.RISE_WRONG_WAY:
            self.assertIsNotNone(gate_check(text, rise), text)
        zero = geo2_facts(zero=True)
        for text in ("The {item:cause:g:z} hurt.", "The {item:cause:g:z} helped.", "The {item:cause:g:z} didn't move it.",
                     "The {item:cause:g:z} came in."):
            self.assertIsNotNone(gate_check(text, zero), text)

    def test_the_wrong_way_words_drop_even_with_the_gate_aside(self):
        # Layer B on its own: each of GE's new words is read against the slot's share.
        idx = w.fact_index(w.model_facts(geo2_facts()))
        for text in ("The {item:cause:g:a} pushed it up.", "The {item:cause:g:a} carried it.", "The {item:cause:g:a} rescued it.",
                     "The {item:cause:g:a} improved it.", "The {item:cause:g:a} gave it a boost.",
                     "The {item:cause:g:a} made the difference upward.", "A strong {item:cause:g:a} kept it up.",
                     "The {item:cause:g:a} hardly hurt.", "The {item:cause:g:a} barely hurt.",
                     "The {item:cause:g:b} tanked it.", "The {item:cause:g:b} cut it.", "The {item:cause:g:b} dented it.",
                     "The {item:cause:g:b} slashed it.", "The {item:cause:g:b} dropped it.", "Blame the {item:cause:g:b}.",
                     "The {item:cause:g:b} was behind the drop.", "The {item:cause:g:b} pushed it down.",
                     "A weak {item:cause:g:b} did it."):
            cites = ["move:g"] + sorted(set(re.findall(r"\{(?:item|bucket):(cause:[^}]+)\}", text)))
            self.assertIsNotNone(claims(text, cites, idx, "Geo"), text)
        rise = w.fact_index(w.model_facts(geo2_facts(delta=2.5, neg=-0.5, pos=3.0)))
        self.assertIsNotNone(claims("A low {item:cause:g:b} score helped.", ["move:g", "cause:g:b"], rise, "Geo"))

    def test_the_allowed_forms_pass(self):
        drop = geo2_facts()
        for text in ("Mostly the {item:cause:g:a}.", "Mostly from the {item:cause:g:a}.", "The {item:cause:g:a} hurt.",
                     "The {item:cause:g:a} dragged it down.", "The {item:cause:g:a} did most of the damage.",
                     "The {item:cause:g:a} score hurt.", "The {item:cause:g:a} (20/40) hurt.", "The {item:cause:g:b} helped.",
                     "{bucket:cause:g:a} scores hurt.", "{bucket:cause:g:a} scores dragged it down.",
                     "{bucket:cause:g:a} scores did most of the damage.", "{bucket:cause:g:b} scores helped.",
                     "Mostly {bucket:cause:g:a} scores.", "Mostly one {bucket:cause:g:a} score.",
                     "Slipped to a D since Oct 14, down 2.5.", "{class:move:g} slipped to a D since Oct 14, down 2.5."):
            self.assertIsNone(gate_check(text, drop), text)
        rise = geo2_facts(delta=2.5, neg=-0.5, pos=3.0)
        for text in ("Mostly the {item:cause:g:b}.", "The {item:cause:g:b} helped.", "{bucket:cause:g:b} scores helped."):
            self.assertIsNone(gate_check(text, rise), text)

    def test_a_list_of_two_or_three_causes_shares_one_sign(self):
        facts = geo2_facts()
        facts[1]["causes"].append({"id": "cause:g:c", "title": "Map Quiz", "bucket": "Quizzes", "bucket_weight_pct": 20.0,
                                   "earned": 2.0, "possible": 10.0, "points": -0.5, "share": -0.5})
        facts[1]["causes"].append({"id": "cause:g:d", "title": "Unit 2 Packet", "bucket": "Homework",
                                   "bucket_weight_pct": 10.0, "earned": 3.0, "possible": 10.0, "points": -0.5,
                                   "share": -0.5})
        for text in ("The {item:cause:g:a} and the {item:cause:g:c} hurt.",
                     "The {item:cause:g:a}, the {item:cause:g:c} and the {item:cause:g:d} hurt.",
                     "The {item:cause:g:a} and {bucket:cause:g:c} scores dragged it down."):
            self.assertIsNone(gate_check(text, facts), text)
        self.assertIsNotNone(gate_check("The {item:cause:g:a} and the {item:cause:g:b} hurt.", facts))
        for text in ("The {item:cause:g:a} and the {item:cause:g:c} helped.",
                     "The {item:cause:g:a} and the {item:cause:g:c} did most of the damage.",
                     "Mostly the {item:cause:g:a} and the {item:cause:g:c}."):
            self.assertIsNotNone(gate_check(text, facts), text)

    def test_most_needs_the_top_share_in_the_moves_direction(self):
        facts = geo2_facts()
        facts[1]["causes"].append({"id": "cause:g:c", "title": "Map Quiz", "bucket": "Quizzes", "bucket_weight_pct": 20.0,
                                   "earned": 2.0, "possible": 10.0, "points": -0.5, "share": -0.5})
        self.assertIsNone(gate_check("The {item:cause:g:c} hurt.", facts))
        for text in ("Mostly the {item:cause:g:c}.", "The {item:cause:g:c} did most of the damage.",
                     "Mostly {bucket:cause:g:c} scores."):
            self.assertIsNotNone(gate_check(text, facts), text)

    def test_a_cause_sentence_off_the_forms_drops_with_a_code(self):
        drop = geo2_facts()
        for text in ("The {item:cause:g:a} pulled it down.", "The {item:cause:g:a} sank it.", "It was hurt by the {item:cause:g:a}.",
                     "Down 2.5 since Oct 14, mostly the {item:cause:g:a}."):
            self.assertEqual(gate_check(text, drop), w.CAUSE_FORM_REASON, text)
        self.assertIsNotNone(gate_check("The {item:cause:g:a} hurt; the {item:cause:g:b} helped.", drop))
        self.assertEqual(w.reason_code(w.CAUSE_FORM_REASON), w.CAUSE_FORM_REASON)
        idx = w.fact_index(w.model_facts(drop))
        # A cited cause with no slot, a cause named but not cited, a move sentence that isn't its opening.
        self.assertEqual(w.check_sentence("One low score did most of the damage.", ["cause:g:a"], idx, "Geo"),
                         w.CAUSE_FORM_REASON)
        self.assertIsNotNone(w.check_sentence("The {item:cause:g:a} hurt.", ["cause:g:a", "cause:g:b"], idx, "Geo"))
        self.assertEqual(w.check_sentence("Down 2.5 since Oct 14.", ["move:g"], idx, "Geo"), w.CAUSE_FORM_REASON)

    def test_sentences_that_are_no_cause_sentence_skip_the_gate(self):
        idx = w.fact_index(w.model_facts(geo2_facts()))
        for text, cites in (("Still a D, 1 points from a C.", ["next:g"]), ("Scores usually post Thursdays.", ["pat:g"]),
                            ("A 74 or better on the {next:whatif:g} gets it to a C.", ["whatif:g"]),
                            ("{bucket:whatif:g} is 50% of the grade.", ["whatif:g"])):
            self.assertIsNone(w.check_sentence(text, cites, idx, "Geo"), text)

    def test_the_v6_story_forms_pass_verbatim(self):
        # The sentence forms of the v6 bake-off stories, on synthetic facts.
        idx = w.fact_index(w.model_facts(chem_facts()))
        for text, cites in (("Still an A, down 1.6 since Oct 14.", [CHEM_MOVE]),
                            ("The {item:%s} did most of the damage." % CHEM_C1, [CHEM_C1]),
                            ("Mostly the {item:%s}." % CHEM_C1, [CHEM_C1]),
                            ("The {item:%s} hurt." % CHEM_C2, [CHEM_C2])):
            self.assertIsNone(w.check_sentence(text, cites, idx, "Chem"), text)
        helped = chem_facts()
        helped[0]["causes"].append({"id": "cause:chem:2026-10-14:rocket", "title": "Balloon Rocket Lab",
                                    "category": "Labs", "bucket": "Labs", "bucket_weight_pct": 20.0, "change": "new",
                                    "earned": 10.0, "possible": 10.0, "points": 0.1, "share": 0.1})
        hidx = w.fact_index(w.model_facts(helped))
        self.assertIsNone(w.check_sentence("The {item:cause:chem:2026-10-14:rocket} helped.",
                                           ["cause:chem:2026-10-14:rocket"], hidx, "Chem"))
        geo = w.fact_index(w.model_facts(geometry_facts()))
        for text, cites in (("Still a C, 0.5 points from a B.", [GEO_NEXT]),
                            ("An 85 or better on the {next:%s} gets it to a B." % GEO_WHATIF, [GEO_WHATIF])):
            self.assertIsNone(w.check_sentence(text, cites, geo, "Geometry"), text)

    def test_the_prompt_teaches_the_forms_and_its_right_example_passes(self):
        self.assertIn("Every sentence cites at least one fact and is one of these forms, word for word.", w.DRAFT_SYSTEM)
        for form in ('"Mostly the {item:<cause id>}."', '"The {item:<cause id>} did most of the damage."',
                     '"The {item:<cause id>} hurt."', '"The {item:<cause id>} helped."', "say_opening", "say_pattern",
                     "say_step", "of the grade.", "Every sentence cites at least one fact", "Right:", "Wrong:"):
            self.assertIn(form, w.DRAFT_SYSTEM)
        idx = w.fact_index(w.model_facts(w.EXAMPLE_FACTS))
        right = re.search(r'Right: "(The \{item:[^"]+)"', w.DRAFT_SYSTEM).group(1)
        wrong = re.search(r'Wrong: "(Still [^"]+)"', w.DRAFT_SYSTEM).group(1)
        fid = re.search(r"\{item:([^}]+)\}", right).group(1)
        self.assertIsNone(w.check_sentence(right, [fid], idx, "Ceramics"))
        self.assertIsNotNone(w.check_sentence(wrong, ["next:ceramics"], idx, "Ceramics"))
        self.assertEqual(w.check_sentence(wrong.replace(", and slipping", ", so far"), ["next:ceramics"], idx,
                                          "Ceramics"), w.FORM_REASON)
        # The example facts carry the say_ fields the model copies.
        model = {f["id"]: f for f in w.model_facts(w.EXAMPLE_FACTS)}
        example = {f["id"]: f for f in w.EXAMPLE_FACTS}
        for fid, field in (("whatif:ceramics:projects:20", "say_step"),):
            self.assertEqual(example[fid][field], model[fid][field])
        self.assertIn("Copy the slots as the story has them. Any other headline is thrown away.", w.HEADLINE_SYSTEM)
        for sentence in w.EXAMPLE_NOTE:
            self.assertIsNone(w.check_sentence(sentence["text"], sentence["cites"], idx, "Ceramics"), sentence["text"])


def bucket_net_facts(delta=-2.0, a=-1.0, c=2.0, b=-3.0):
    """GE's gate868 fixture: Tests holds a and c, Labs holds b; the move is delta."""
    facts = geo2_facts(delta=delta, neg=a, pos=b)
    facts[1]["causes"].append({"id": "cause:g:c", "title": "Map Quiz", "bucket": "Tests", "bucket_weight_pct": 50.0,
                               "earned": 10.0, "possible": 10.0, "points": c, "share": c})
    return facts


class GateReviewTests(unittest.TestCase):
    """GE's gate868 on 868098d and the CTO's rules: non-cause buckets, bucket nets, move-direction words."""

    MOVED = "says the grade moved a way no cited move did"

    def test_a_bucket_slot_on_a_what_if_never_carries_a_direction(self):
        for facts in (geo2_facts(), geo2_facts(delta=2.5, neg=-0.5, pos=3.0)):
            idx = w.fact_index(w.model_facts(facts))
            for text, cites in (("{bucket:whatif:g} scores helped.", ["whatif:g"]),
                                ("{bucket:whatif:g} scores helped.", ["whatif:g", "move:g"]),
                                ("{bucket:whatif:g} scores hurt.", ["whatif:g", "next:g"]),
                                ("{bucket:whatif:g} scores dragged it down.", ["whatif:g"]),
                                ("Mostly {bucket:whatif:g} scores.", ["whatif:g", "move:g"]),
                                ("{bucket:whatif:g} scores did most of the damage.", ["whatif:g"])):
                self.assertIsNotNone(w.check_sentence(text, cites, idx, "Geo"), text)
            # With no direction word a what-if bucket's weight is fine; other wording is off the forms.
            self.assertIsNone(w.check_sentence("{bucket:whatif:g} is 50% of the grade.", ["whatif:g"], idx, "Geo"))
            for text in ("A 74 or better on the next {bucket:whatif:g} score gets it to a C.",
                         "The next {bucket:whatif:g} score decides it."):
                self.assertEqual(w.check_sentence(text, ["whatif:g"], idx, "Geo"), w.FORM_REASON, text)

    def test_a_bucket_reads_its_net_share_in_the_move(self):
        # Drop of 2.0: Tests nets +1.0 (a -1.0, c +2.0), Labs -3.0.
        idx = w.fact_index(w.model_facts(bucket_net_facts()))
        for text in ("{bucket:cause:g:a} scores hurt.", "{bucket:cause:g:c} scores hurt.",
                     "{bucket:cause:g:a} scores did most of the damage.", "Mostly {bucket:cause:g:a} scores.",
                     "{bucket:cause:g:b} scores helped.", "{bucket:cause:g:a} and {bucket:cause:g:c} scores helped."):
            self.assertIsNotNone(gate_check(text, bucket_net_facts()), text)
        for text in ("{bucket:cause:g:a} scores helped.", "{bucket:cause:g:c} scores helped.",
                     "{bucket:cause:g:b} scores hurt.", "{bucket:cause:g:b} scores did most of the damage.",
                     "Mostly {bucket:cause:g:b} scores."):
            self.assertIsNone(gate_check(text, bucket_net_facts()), text)
        self.assertEqual(w._bucket_net(idx, idx["cause:g:a"]), 1.0)
        # Rise of 2.0: Tests nets -1.0 (a +1.0, c -2.0), Labs +3.0.
        rise = bucket_net_facts(delta=2.0, a=1.0, c=-2.0, b=3.0)
        for text in ("{bucket:cause:g:a} scores helped.", "Mostly {bucket:cause:g:a} scores.", "{bucket:cause:g:b} scores hurt."):
            self.assertIsNotNone(gate_check(text, rise), text)
        for text in ("{bucket:cause:g:a} scores hurt.", "{bucket:cause:g:b} scores helped.", "Mostly {bucket:cause:g:b} scores."):
            self.assertIsNone(gate_check(text, rise), text)

    def test_mostly_a_bucket_ranks_buckets_by_net(self):
        # Drop: Tests -2.5 (a -1.5, c -1.0) beats Labs -2.0, though Labs holds the largest single cause.
        facts = bucket_net_facts(delta=-4.5, a=-1.5, c=-1.0, b=-2.0)
        self.assertIsNone(gate_check("Mostly {bucket:cause:g:a} scores.", facts))
        self.assertIsNone(gate_check("Mostly {bucket:cause:g:c} scores.", facts))
        self.assertIsNotNone(gate_check("Mostly {bucket:cause:g:b} scores.", facts))
        self.assertIsNone(gate_check("Mostly the {item:cause:g:b}.", facts))
        rise = bucket_net_facts(delta=4.5, a=1.5, c=1.0, b=2.0)
        self.assertIsNone(gate_check("Mostly {bucket:cause:g:a} scores.", rise))
        self.assertIsNotNone(gate_check("Mostly {bucket:cause:g:b} scores.", rise))

    def test_a_bucket_net_under_a_tenth_claims_no_direction(self):
        # The edge is 0.1 either way: 0.3 - 0.2 nets 0.1 (to the float), 0.29 - 0.2 nets 0.09.
        for c, a, ok in ((0.3, -0.2, True), (0.29, -0.2, False), (0.1, 0.0, True), (0.0, 0.0, False)):
            facts = bucket_net_facts(delta=-2.9, a=a, c=c, b=-3.0)
            got = gate_check("{bucket:cause:g:c} scores helped.", facts)
            self.assertEqual(got is None, ok, (c, a, got))
        for c, a, ok in ((-0.3, 0.2, True), (-0.29, 0.2, False)):
            facts = bucket_net_facts(delta=2.9, a=a, c=c, b=3.0)
            got = gate_check("{bucket:cause:g:c} scores hurt.", facts)
            self.assertEqual(got is None, ok, (c, a, got))

    def test_move_direction_words_need_a_cited_move_of_that_sign(self):
        for facts, wrong, right in ((geo2_facts(), "rose", "slipped"), (geo2_facts(delta=2.5, neg=-0.5, pos=3.0), "fell", "rose")):
            idx = w.fact_index(w.model_facts(facts))
            for text, cites in (("Scores dropped.", ["next:g"]), ("It rose.", ["next:g"]),
                                ("It fell.", ["pat:g"]), ("Scores went up.", ["pat:g"]), ("It slipped.", ["next:g"])):
                self.assertEqual(w.check_sentence(text, cites, idx, "Geo"), self.MOVED, text)
            self.assertEqual(claims(f"It {wrong} since Oct 14.", ["move:g"], idx, "Geo"), self.MOVED)
            self.assertIsNone(claims(f"It {right} since Oct 14.", ["move:g"], idx, "Geo"))
            opening = w.model_facts(facts)[1]["say_opening"]
            self.assertIsNone(w.check_sentence(opening, ["move:g"], idx, "Geo"), opening)
            flipped = opening.replace("down", "up") if "down" in opening else opening.replace("up", "down")
            self.assertIsNotNone(w.check_sentence(flipped, ["move:g"], idx, "Geo"), flipped)

    def test_the_exact_contrast_forms_pass_both_ways(self):
        drop, rise = geo2_facts(), geo2_facts(delta=2.5, neg=-0.5, pos=3.0)
        self.assertIsNone(gate_check("The {item:cause:g:b} helped.", drop))
        self.assertIsNone(gate_check("The {item:cause:g:a} hurt.", rise))
        self.assertIsNone(gate_check("{bucket:cause:g:a} scores hurt.", rise))
        self.assertIsNotNone(gate_check("The {item:cause:g:a} hurt a little.", rise))
        self.assertIsNotNone(gate_check("The {item:cause:g:a} dragged it down.", rise))

    def test_a_bucket_weight_on_a_cause_is_no_direction_claim(self):
        idx = w.fact_index(w.model_facts(geo2_facts()))
        self.assertIsNone(w.check_sentence("{bucket:cause:g:a} is 50% of the grade.", ["cause:g:a"], idx, "Geo"))
        self.assertIsNone(w.check_sentence("{bucket:cause:g:a} scores are 50% of the grade.", ["cause:g:a"], idx, "Geo"))
        self.assertIsNotNone(w.check_sentence("{bucket:cause:g:a} is 40% of the grade.", ["cause:g:a"], idx, "Geo"))
        self.assertIsNotNone(w.check_sentence("{bucket:cause:g:a} is 50% of the grade and dragged it down.",
                                              ["cause:g:a"], idx, "Geo"))
        self.assertIsNotNone(w.check_sentence("{bucket:cause:g:b} is 30% of the grade, and helped.", ["cause:g:b"], idx, "Geo"))

    def test_bucket_fills_keep_their_capitals_but_not_after_an_article(self):
        idx = w.fact_index(w.model_facts(geo2_facts()))
        self.assertEqual(w.fill_slots("Mostly {bucket:cause:g:a} scores.", idx), "Mostly Tests scores.")
        self.assertEqual(w.fill_slots("{bucket:cause:g:b} scores helped.", idx), "Labs scores helped.")
        self.assertEqual(w.fill_slots("The {item:cause:g:a} and {bucket:cause:g:b} scores hurt.", idx),
                         "The Unit Test (20/40) and Labs scores hurt.")
        self.assertEqual(w.fill_slots("The next {bucket:whatif:g} score decides it.", idx),
                         "The next tests score decides it.")
        self.assertEqual(w.fill_slots("Mostly one {bucket:cause:g:a} score.", idx), "Mostly one tests score.")


def fold_facts(folded=True, other_share=-0.5):
    """GE's rules6d fold repro: seven Labs at -0.5, Tests t1 +0.3, Tests t2 and t3 at -0.25 each.

    Folded (as #54 keeps the top 8), Tests' kept causes net +0.3 though its real net is -0.2.
    """
    def cause(i, bucket, share):
        return {"id": f"cause:g:{i}", "title": f"Item {i}", "bucket": bucket, "bucket_weight_pct": 30.0,
                "earned": 5.0, "possible": 10.0, "points": share, "share": share}
    causes = [cause(f"l{i}", "Labs", -0.5) for i in range(7)] + [cause("t1", "Tests", 0.3), cause("t2", "Tests", -0.25),
                                                                cause("t3", "Tests", -0.25)]
    move = {"id": "move:g", "kind": "grade_move", "class": "Geo", "since": "2026-10-14", "from_pct": 72.2,
            "to_pct": 69.0, "delta": -3.2, "explained": -3.2, "coverage": 1.0, "causes": causes}
    if folded:
        move = {**move, "causes": causes[:8], "other_causes": 2, "other_share": other_share}
    return geo2_facts()[:1] + [move]


class FoldedMoveTests(unittest.TestCase):
    """GE's rules6d on 6d1eb2e: a bucket net can't be known once #54 folds causes away, so none is claimed."""

    BUCKET_CLAIMS = ("{bucket:cause:g:t1} scores helped.", "{bucket:cause:g:t1} scores hurt.",
                     "{bucket:cause:g:l0} scores hurt.", "{bucket:cause:g:l0} scores dragged it down.",
                     "Mostly {bucket:cause:g:l0} scores.", "Mostly one {bucket:cause:g:l0} score.",
                     "{bucket:cause:g:l0} scores did most of the damage.",
                     "{bucket:cause:g:l0} and {bucket:cause:g:t1} scores hurt.")
    ITEM_CLAIMS = ("The {item:cause:g:t1} helped.", "The {item:cause:g:l0} hurt.",
                   "The {item:cause:g:l0} dragged it down.")

    def assert_fails_closed(self, facts):
        for text in self.BUCKET_CLAIMS:
            self.assertIsNotNone(gate_check(text, facts), text)
        for text in self.ITEM_CLAIMS:
            self.assertIsNone(gate_check(text, facts), text)
        idx = w.fact_index(w.model_facts(facts))
        self.assertIsNone(w.check_sentence("{bucket:cause:g:t1} is 30% of the grade.", ["cause:g:t1"], idx, "Geo"))

    def test_a_hand_built_folded_move_claims_no_bucket_direction(self):
        self.assert_fails_closed(fold_facts())
        # other_causes alone folds it, even with no exact other_share; so does a non-zero other_share alone.
        self.assert_fails_closed(fold_facts(other_share=None))
        facts = fold_facts(folded=False)
        facts[1] = {**facts[1], "other_share": -0.5}
        self.assert_fails_closed(facts)

    def test_an_unfolded_move_still_reads_bucket_nets(self):
        facts = fold_facts(folded=False)
        self.assertIsNotNone(gate_check("{bucket:cause:g:t1} scores helped.", facts))
        self.assertIsNone(gate_check("{bucket:cause:g:t1} scores hurt.", facts))
        self.assertIsNone(gate_check("Mostly {bucket:cause:g:l0} scores.", facts))
        # No fold: other_causes 0 and other_share 0 or None.
        for share in (0, 0.0, None):
            facts = fold_facts(folded=False)
            facts[1] = {**facts[1], "other_causes": 0, "other_share": share}
            self.assertIsNone(gate_check("{bucket:cause:g:t1} scores hurt.", facts), share)

    @unittest.skipUnless(hasattr(insights, "fold_causes"), "needs #54's fold_causes")
    def test_a_move_folded_by_fold_causes_claims_no_bucket_direction(self):
        facts = fold_facts(folded=False)
        folded = insights.fold_causes(facts[1])
        self.assertEqual((len(folded["causes"]), folded["other_causes"], folded["other_share"]), (8, 2, -0.5))
        facts[1] = folded
        self.assertIsNotNone(gate_check("{bucket:cause:g:t1} scores helped.", facts))
        self.assert_fails_closed(facts)


class TopTieTests(unittest.TestCase):
    """No "most of" claim for a bucket or an item that ties for the top (within 1e-6)."""

    def test_buckets_that_tie_for_the_top_claim_no_most(self):
        for delta, a, b in ((-2.0, -1.0, -1.0), (-2.0, -1.0, -1.0000005), (2.0, 1.0, 1.0)):
            facts = geo2_facts(delta=delta, neg=a, pos=b)
            for text in ("Mostly {bucket:cause:g:a} scores.", "Mostly {bucket:cause:g:b} scores."):
                self.assertIsNotNone(gate_check(text, facts), (text, a, b))
            if delta < 0:
                for text in ("{bucket:cause:g:a} scores did most of the damage.",
                             "{bucket:cause:g:b} scores did most of the damage."):
                    self.assertIsNotNone(gate_check(text, facts), (text, a, b))
                self.assertIsNone(gate_check("{bucket:cause:g:a} scores hurt.", facts))
        facts = geo2_facts(delta=-2.01, neg=-1.0, pos=-1.01)
        self.assertIsNone(gate_check("Mostly {bucket:cause:g:b} scores.", facts))
        self.assertIsNone(gate_check("{bucket:cause:g:b} scores did most of the damage.", facts))
        self.assertIsNotNone(gate_check("Mostly {bucket:cause:g:a} scores.", facts))

    def test_items_that_tie_for_the_top_claim_no_most(self):
        for delta, a, b in ((-2.0, -1.0, -1.0), (-2.0, -1.0, -1.0000005), (2.0, 1.0, 1.0)):
            facts = geo2_facts(delta=delta, neg=a, pos=b)
            facts[1]["causes"][1]["bucket"] = "Tests"
            for text in ("Mostly the {item:cause:g:a}.", "Mostly from the {item:cause:g:b}."):
                self.assertIsNotNone(gate_check(text, facts), (text, a, b))
            if delta < 0:
                for text in ("The {item:cause:g:a} did most of the damage.",
                             "The {item:cause:g:b} did most of the damage."):
                    self.assertIsNotNone(gate_check(text, facts), (text, a, b))
                self.assertIsNone(gate_check("The {item:cause:g:a} hurt.", facts))
        for delta, a, b in ((-2.01, -1.0, -1.01), (2.01, 1.0, 1.01)):
            facts = geo2_facts(delta=delta, neg=a, pos=b)
            self.assertIsNone(gate_check("Mostly the {item:cause:g:b}.", facts))
            self.assertIsNotNone(gate_check("Mostly the {item:cause:g:a}.", facts))
        self.assertIsNone(gate_check("The {item:cause:g:b} did most of the damage.",
                                     geo2_facts(delta=-2.01, neg=-1.0, pos=-1.01)))


class MoveIdiomTests(unittest.TestCase):
    """Every up/down is a direction word, idioms too (fail closed); the move verbs need a matching move."""

    IDIOMS = (("Scores usually post Thursdays, up to date.", ["pat:g"]),
              ("Scores usually show up Thursdays.", ["pat:g"]),
              ("Scores usually showed up on Thursdays.", ["pat:g"]),
              ("Still a D, down to the wire, 1.0 points from a C.", ["next:g"]),
              ("Still a D, 1 point from a C; it comes down to the next score.", ["next:g"]),
              ("It comes down to a vote.", ["next:g"]),
              ("A 74 or better on the next {bucket:whatif:g} score gets it up to a C.", ["whatif:g"]),
              ("A 74 or better on the {next:whatif:g} brings it up to a C.", ["whatif:g"]))
    # GE's leaks on 13cbf50, wrong way for each move: up on the fall (C to D), down on the rise (a D).
    LEAKS_ON_FALL = ("Up to a solid C since Oct 14.", "Up to the C range since Oct 14.", "It's up to a c now.",
                     'Up to a "C" since Oct 14.', "Up to passing since Oct 14.", "It's up to the grade it had.")
    LEAKS_ON_RISE = ("Down to the D range since Oct 14.", "Now down to a weak D.", "It came down to a d.",
                     "Down to 'D' since Oct 14.", "Down to failing since Oct 14.")

    def test_idioms_drop_with_no_matching_move(self):
        for facts in (geo2_facts(), geo2_facts(delta=2.5, neg=-0.5, pos=3.0)):
            idx = w.fact_index(w.model_facts(facts))
            for text, cites in self.IDIOMS:
                self.assertIsNotNone(w.check_sentence(text, cites, idx, "Geo"), text)
                if "{" not in text:
                    self.assertEqual(claims(text, cites, idx, "Geo"), GateReviewTests.MOVED, text)
            self.assertEqual(w.check_sentence("Still a D, down to the D range.", ["next:g"], idx, "Geo"),
                             GateReviewTests.MOVED)
            # The step's own form, with no up or down in it, passes.
            self.assertIsNone(w.check_sentence("A 74 or better on the {next:whatif:g} gets it to a C.", ["whatif:g"],
                                               idx, "Geo"))
            self.assertIsNone(w.check_sentence("Scores usually post Thursdays.", ["pat:g"], idx, "Geo"))

    def test_up_to_or_down_to_anything_on_the_wrong_way_move_drops(self):
        moved = GateReviewTests.MOVED
        fall, rise = (w.fact_index(w.model_facts(f)) for f in (geo2_facts(), geo2_facts(delta=2.5, neg=-0.5, pos=3.0)))
        self.assertEqual(len(self.LEAKS_ON_FALL + self.LEAKS_ON_RISE), 11)
        for idx, texts in ((fall, self.LEAKS_ON_FALL), (rise, self.LEAKS_ON_RISE)):
            for text in texts:
                self.assertIsNotNone(w.check_sentence(text, ["move:g"], idx, "Geo"), text)
                self.assertEqual(claims(text, ["move:g"], idx, "Geo"), moved, text)

    def test_up_to_or_down_to_a_grade_is_a_direction(self):
        # GE's repros on e677998, citing the move: a fall from a C to a D, and a rise from 66.5 to 69 (a D).
        moved = GateReviewTests.MOVED
        fall, rise = (w.fact_index(w.model_facts(f)) for f in (geo2_facts(), geo2_facts(delta=2.5, neg=-0.5, pos=3.0)))
        for text in ("Up to a C since Oct 14.", "It's up to a C now.", "Up to 71.5 since Oct 14."):
            self.assertIsNotNone(w.check_sentence(text, ["move:g"], fall, "Geo"), text)
            self.assertEqual(claims(text, ["move:g"], fall, "Geo"), moved, text)
        for text in ("Now down to a D.", "Down to a D since Oct 14.", "It came down to a D.",
                     "Down to 69 since Oct 14.", "Down to 69% now."):
            self.assertIsNotNone(w.check_sentence(text, ["move:g"], rise, "Geo"), text)
            self.assertEqual(claims(text, ["move:g"], rise, "Geo"), moved, text)
        # The matching sign passes the move check (with the gate set aside, as only the say_opening passes it).
        for text in ("Down to a D since Oct 14.", "It's down to a D now.", "Now down to a D.", "It came down to a D.",
                     "Down to 69 since Oct 14."):
            self.assertIsNone(claims(text, ["move:g"], fall, "Geo"), text)
            self.assertEqual(w.check_sentence(text, ["move:g"], fall, "Geo"), w.CAUSE_FORM_REASON, text)
        for text in ("Now up to a D.", "Up to a D since Oct 14.", "It came up to a D.", "It's up to a D now.",
                     "Up to 69 since Oct 14."):
            self.assertIsNone(claims(text, ["move:g"], rise, "Geo"), text)
        # Whatever follows, up and down are direction words.
        for text in ("Now up to a B+.", "Up to an A- now.", "It is up to 90%.", "Up to A.", "It is up to date.",
                     "Up to the teacher.", "They show up Thursdays.", "It showed up as a C."):
            self.assertTrue(w._MOVED_UP_RE.search(text), text)
        for text in ("Down to a C-.", "It is down to 69.", "Down to D.", "It is down to a vote.", "Down to the wire."):
            self.assertTrue(w._MOVED_DOWN_RE.search(text), text)
        # Where the letter check disagrees, it still drops: a rise that stayed a D isn't "up to a C".
        self.assertIsNotNone(claims("Up to a C since Oct 14.", ["move:g"], rise, "Geo"))

    def test_plain_up_down_and_move_verbs_still_need_a_matching_move(self):
        moved = GateReviewTests.MOVED
        drop, rise = (w.fact_index(w.model_facts(f)) for f in (geo2_facts(), geo2_facts(delta=2.5, neg=-0.5, pos=3.0)))
        for text in ("Scores rose up to now.", "Scores went up.", "It is up.", "Scores moved up."):
            self.assertEqual(claims(text, ["move:g"], drop, "Geo"), moved, text)
            self.assertEqual(claims(text, ["next:g"], rise, "Geo"), moved, text)
        for text in ("Scores fell down to now.", "Scores went down.", "It is down.", "Scores moved down."):
            self.assertEqual(claims(text, ["move:g"], rise, "Geo"), moved, text)
            self.assertEqual(claims(text, ["next:g"], drop, "Geo"), moved, text)
        # "Rose up to a C" also claims a letter, so it drops on either count.
        self.assertIsNotNone(claims("It rose up to a C.", ["move:g"], drop, "Geo"))
        self.assertIsNotNone(claims("It rose up to a C.", ["next:g"], rise, "Geo"))
        self.assertNotEqual(claims("It rose up to a C.", ["move:g"], rise, "Geo"), moved)
        self.assertIsNone(claims("Scores went up.", ["move:g"], rise, "Geo"))
        self.assertIsNone(claims("Scores went down.", ["move:g"], drop, "Geo"))


class LetterFormTests(unittest.TestCase):
    """GE's updown4d on 4d3d1fc: a sentence citing only the letter fact is its say_opening, word for word."""

    DIRECTION = ("Still a D, 1.0 points from a C, and slipping.", "No longer a C.", "From a C to a D.",
                 # comparatives
                 "Still a D, but higher than before.", "Still a D, worse than before.",
                 "Still a D, 1.0 points from a C, closer than before.",
                 # -ing forms
                 "Still a D, 1.0 points from a C, and rising.", "Still a D, 1.0 points from a C, losing ground.",
                 "Still a D, 1.0 points from a C, trending lower.",
                 # nouns
                 "Still a D after a dip.", "Still a D, 1.0 points from a C, after a slump.",
                 "Still a D, 1.0 points from a C, a step back.",
                 # letter changes
                 "It went from a C to a D.", "Now a D, was a C.", "It's a D now, not a C.")

    def facts(self):
        return {"fall": geo2_facts(), "rise": geo2_facts(delta=2.5, neg=-0.5, pos=3.0)}

    def test_direction_on_the_letter_fact_alone_drops(self):
        for label, facts in self.facts().items():
            idx = w.fact_index(w.model_facts(facts))
            for text in self.DIRECTION:
                for cites in (["next:g"], ["next:g", "pat:g"]):
                    self.assertIsNotNone(w.check_sentence(text, cites, idx, "Geo", ["Geo"]), (label, text, cites))
            for text in ("Still a D, 1.0 points from a C, and slipping.", "No longer a C.", "From a C to a D."):
                self.assertEqual(w.check_sentence(text, ["next:g"], idx, "Geo", ["Geo"]), w.FORM_REASON, text)
            # Off the opening's words, even with no direction: the short form drops too (fail closed).
            for text in ("Geo: D, 1 pts from a C.", "Still a D.", "Still a D, 1 point from a C."):
                self.assertEqual(w.check_sentence(text, ["next:g"], idx, "Geo", ["Geo"]), w.FORM_REASON, text)
            self.assertEqual(w.reason_code(w.FORM_REASON), w.FORM_REASON)

    def test_the_exact_opening_and_the_step_pass(self):
        for label, facts in self.facts().items():
            model = {f["id"]: f for f in w.model_facts(facts)}
            idx = w.fact_index(list(model.values()))
            opening = model["next:g"]["say_opening"]
            self.assertTrue(opening.startswith("Still a D, 1"), opening)
            for text in (opening, "{class:next:g} " + opening[0].lower() + opening[1:]):
                self.assertIsNone(w.check_sentence(text, ["next:g"], idx, "Geo", ["Geo"]), (label, text))
            step = model["whatif:g"]["say_step"]
            self.assertIsNone(w.check_sentence(step, ["whatif:g"], idx, "Geo", ["Geo"]), (label, step))
            # The what-if keeps its own form; a pattern or the move sentence is untouched.
            self.assertIsNone(w.check_sentence("Scores usually post Thursdays.", ["pat:g"], idx, "Geo", ["Geo"]))
            move_opening = model["move:g"]["say_opening"]
            self.assertIsNone(w.check_sentence(move_opening, ["move:g"], idx, "Geo", ["Geo"]), move_opening)


class ReviewNitTests(unittest.TestCase):
    """GE's nits on #52 8d2bd63: codes without letters or weekdays, natural next fills, no keep ids in {next:}."""

    def test_drop_codes_carry_no_letters_or_weekdays(self):
        self.assertEqual(w.reason_code("letter B isn't in the cited facts"), "letter <letter> isn't in the cited facts")
        self.assertEqual(w.reason_code("letter C+ isn't on the whole-letter scale"),
                         "letter <letter> isn't on the whole-letter scale")
        self.assertEqual(w.reason_code("says the class moved to D but no move crossed into that letter"),
                         "says the class moved to <letter> but no move crossed into that letter")
        self.assertEqual(w.reason_code("pattern on Thursdays isn't cited"), "pattern on <day> isn't cited")
        self.assertEqual(w.reason_code("names a weekday (Monday) no pattern backs"), "names a weekday no pattern backs")
        self.assertEqual(w.reason_code("gives advice no what_if backs"), "gives advice no what_if backs")
        story = w.write_class(ScriptedModel({"Geo": [
            {"text": "Slipped to a D since Oct 14, down 2.5.", "cites": ["move:geo"]},
            {"text": "Still a D, 1.4 points from a B.", "cites": ["next:geo"]},
            {"text": "Scores usually post Mondays.", "cites": ["pattern:posting_day:geo"]},
            {"text": "A 74 or better on the {next:whatif:geo} gets it to a C.", "cites": ["whatif:geo"]}]}),
            geo_facts(), "Geo")
        self.assertEqual(len(story["dropped"]), 2)
        for code in story["dropped"]:
            self.assertNotRegex(code, r"(?<![\w'’])[A-F](?![\w'’])|(?i:monday)", code)

    def test_next_fills_read_naturally(self):
        for bucket, said in (("Homework", "the next homework assignment"), ("Labs & Projects", "the next lab or project"),
                             ("Labs/Activities", "the next lab or activity"),
                             ("Participation", "the next participation score"), ("Tests", "the next test")):
            idx = w.fact_index(geo_facts(bucket=bucket))
            self.assertEqual(w.fill_slots("A 74 or better on the {next:whatif:geo} gets it to a C.", idx),
                             f"A 74 or better on {said} gets it to a C.", bucket)
            self.assertIsNone(w.check_sentence("A 74 or better on the {next:whatif:geo} gets it to a C.",
                                               ["whatif:geo"], idx, "Geo"), bucket)

    def test_keep_a_offers_keep_ids_in_next_slots(self):
        # #52 drops this line until keep-A lands; keep-A puts it back.
        self.assertIn("{next:<keep_letter id>}", w.DRAFT_SYSTEM)
        self.assertIn("A what_if or keep_letter id goes in a {next:} slot", w.DRAFT_SYSTEM)


class OtherClassNameTests(unittest.TestCase):
    """GE's nits 1 and 2 on #52 8d2bd63: lowercase class names, and the extra fixed subject words."""

    CLASSES = ["Geo", "English 10", "Physics", "Art", "Band", "Health"]

    def check(self, text, cites=("cause:geo:t",), classes=None, cls="Geo"):
        """The names check (fixed forms aside: these sentences are about class names)."""
        return claims(text, list(cites), w.fact_index(w.model_facts(geo_facts(cls=cls))), cls,
                      self.CLASSES if classes is None else classes)

    def test_another_class_in_lowercase_is_dropped(self):
        for text in ("physics scores dragged it.", "Down 2.5; physics grades dragged it.", "physics dragged it.",
                     "{bucket:cause:geo:t} scores, not physics class, dragged it.", "health scores dragged it.",
                     "Down 2.5, mostly from physics.", "Mostly physics.", "It slipped on physics."):
            self.assertEqual(self.check(text), "names another class (%s)" % re.search(r"physics|health", text).group(0), text)

    def test_lowercase_common_words_still_pass(self):
        for text, cites in (("Scores usually post Thursdays, state of the art.", ["pattern:posting_day:geo"]),
                            ("Still a D, 1.4 points from a C; the band for a C starts at 70.", ["next:geo"]),
                            ("Still a D, 1.4 points from a C, in good health.", ["next:geo"]),
                            ("Its posting history says Thursdays.", ["pattern:posting_day:geo"]),
                            ("geo scores usually post Thursdays.", ["pattern:posting_day:geo"]),
                            ("{bucket:cause:geo:t} scores dragged it.", ["cause:geo:t"])):
            # The letter sentences are off the fixed opening (LetterFormTests); here only the name check counts.
            reason = self.check(text, cites)
            self.assertIn(reason, (None, w.FORM_REASON) if cites == ["next:geo"] else (None,), text)

    def test_the_extra_subject_words_are_another_class(self):
        for word in ("Chem", "Bio", "APUSH", "P.E.", "Yearbook"):
            self.assertEqual(self.check(f"{word} scores dragged it."), f"names another class ({word})", word)
            self.assertEqual(self.check(f"{word} scores dragged it.", classes=["Geo"]), f"names another class ({word})")

    def test_the_students_own_class_may_use_its_short_form(self):
        pattern = ["pattern:posting_day:geo"]
        for cls, text in (("Chemistry H", "Chem scores usually post Thursdays."),
                          ("Biology", "Bio scores usually post Thursdays."),
                          ("Yearbook", "Yearbook scores usually post Thursdays."),
                          ("Physical Education", "P.E. scores usually post Thursdays."),
                          ("AP US History", "APUSH scores usually post Thursdays.")):
            self.assertIsNone(self.check(text, pattern, classes=[cls], cls=cls), cls)


class EngineFactsTests(unittest.TestCase):
    """Facts from the real engine (synthetic gradebook) go through the checker unchanged."""

    def test_engine_ids_and_numbers_check_out(self):
        from test_insights import POG_ROWS, POG_TOTALS, class_input, snapshot, TODAY

        prior = snapshot("2026-10-21", {"Biology": (91.8, "A")})
        cls = class_input("Biology", POG_TOTALS, POG_ROWS, 85.2, "B", new_keys=["unit2test"])
        facts = insights.class_facts(cls, [prior], prior, TODAY)
        idx = w.fact_index(facts)
        step = next(f for f in facts if f["kind"] == "next_letter")
        move = next(f for f in facts if f["kind"] == "grade_move")
        line = f"Biology: {step['letter']}, {step['gap']:g} pts from an {step['next_letter']}."
        self.assertIsNone(check(line, [step["id"]], idx, "Biology"))
        cause = move["causes"][0]
        said = f"Down mainly from a {cause['earned'] / cause['possible'] * 100:.0f}% on the {cause['title']}."
        expected = None if w.cause_allowed(move) else "states a cause the move's causes don't explain"
        self.assertEqual(check(said, [cause["id"]], idx, "Biology"), expected)



class CtoRulingFiveTests(unittest.TestCase):
    """The CTO's ruling on #52 5bc7884: every sentence cites a fact, headline steps with or without the letter,
    weekdays only from a computed pattern, and one parser leniency (extra trailing braces)."""

    def idx(self, delta=-2.5):
        facts = geo2_facts() if delta < 0 else geo2_facts(delta=2.5, neg=-0.5, pos=3.0)
        return w.fact_index(w.model_facts(facts))

    def test_every_uncited_sentence_drops(self):
        idx = self.idx()
        for text in ("Heading the wrong way.", "Its posted scale changed.", "Keep it going.", "Nice work this week.",
                     "Mostly steady.", "Still a D.", "Scores usually post Thursdays.",
                     "A 74 or better on the next test gets it to a C."):
            self.assertEqual(w.check_sentence(text, [], idx, "Geo", ["Geo"]), w.UNCITED_REASON, text)
            self.assertEqual(w.check_sentence(text, [], idx, headline=True), w.UNCITED_REASON, text)
        self.assertFalse(hasattr(w, "free_reason"))
        self.assertFalse(hasattr(w, "FREE_REASON"))
        self.assertIn("Every sentence cites at least one fact", w.DRAFT_SYSTEM)

    def test_headline_steps_pass_with_or_without_the_letter(self):
        idx = self.idx()
        step = w.model_facts(geo2_facts())[2]["say_step"]
        lower = step[0].lower() + step[1:]
        for cites in (["whatif:g"], ["next:g", "whatif:g"]):
            for text in (step, "Geo: " + lower, "{class:whatif:g}: " + lower):
                self.assertIsNone(w.check_sentence(text, cites, idx, headline=True), (text, cites))
        gap = "{class:whatif:g} is 1 pts from a C; " + lower
        self.assertIsNone(w.check_sentence(gap, ["next:g", "whatif:g"], idx, headline=True))
        self.assertIsNotNone(w.check_sentence(gap, ["whatif:g"], idx, headline=True))
        # Anything beside the step and its own class's letter still drops.
        self.assertEqual(w.check_sentence(step, ["whatif:g", "pat:g"], idx, headline=True), w.FORM_REASON)
        self.assertEqual(w.check_sentence(step, ["whatif:g", "move:g"], idx, headline=True), w.FORM_REASON)
        other = {**idx, "next:o": {"id": "next:o", "kind": "next_letter", "class": "Art", "letter": "B",
                                   "next_letter": "A", "gap": 1.0, "pct": 89.0, "threshold": 90.0}}
        self.assertEqual(w.check_sentence(step, ["next:o", "whatif:g"], other, headline=True), w.FORM_REASON)
        self.assertEqual(w.check_sentence("Geo: " + lower.replace("gets it to", "brings it to"), ["next:g", "whatif:g"],
                                          idx, headline=True), w.FORM_REASON)

    def test_a_weekday_only_from_the_cited_computed_pattern(self):
        idx = self.idx()
        self.assertIsNone(w.check_sentence("Scores usually post Thursdays.", ["pat:g"], idx, "Geo"))
        for text, cites in (("Scores usually post Fridays.", ["pat:g"]), ("Scores usually post Tuesdays.", ["pat:g"]),
                            ("Scores usually post Thursdays.", ["next:g"]), ("Scores usually post Thursdays.", ["whatif:g"]),
                            ("Scores usually post Thursdays.", ["pat:g", "next:g"]),
                            ("Scores usually post on Thursdays.", ["pat:g"]), ("Thursday scores hurt.", ["move:g"]),
                            ("Still a D, 1 points from a C, Thursday.", ["next:g"]),
                            ("Scores usually post Thursdays.", ["pattern:posting_day:ceramics"])):
            self.assertIsNotNone(w.check_sentence(text, cites, idx, "Geo"), (text, cites))
        # The pattern fact comes from the engine, which counts real post dates (insights.find_patterns).
        log = [{"class": "Geo", "first_seen": f"2026-10-{d:02d}", "scrape": "day"} for d in (1, 8, 15, 22, 1, 8, 15, 22, 9)]
        # Eight Thursday posts and one Friday overnight scrape (counted to Thursday the 8th).
        log[-1]["scrape"] = "overnight"
        found = insights.find_patterns(log, insights.date(2026, 10, 23))
        self.assertEqual([f["weekday"] for f in found], ["Thursday"])
        self.assertEqual(w.say_pattern(found[0]), "Scores usually post Thursdays.")

    def test_the_prompt_shows_no_weekday_to_copy(self):
        for day in w.WEEKDAY_NAMES:
            if day == "friday":  # only inside the wrong example "next Friday"
                self.assertEqual(w.DRAFT_SYSTEM.lower().count(day), 1)
                continue
            self.assertNotIn(day, w.DRAFT_SYSTEM.lower(), day)
            self.assertNotIn(day, w.HEADLINE_SYSTEM.lower(), day)
        self.assertFalse([f for f in w.EXAMPLE_FACTS if f.get("kind") == "posting_day"])

    def test_the_parser_strips_extra_trailing_braces_only(self):
        good = '{"sentences": [{"text": "Still a D.", "cites": ["next:g"]}]}'
        for raw in (good + "}", good + "}}", good + "}\n", "```json\n" + good + "}\n```"):
            self.assertEqual(w._json(raw), json.loads(good), raw)
        self.assertEqual(w._sentences(good + "}")[0]["text"], "Still a D.")
        for raw in (good[:-1], good[:-2] + "}}}", '{"sentences": [{"text": "x", "cites": []}}}', good + "} x",
                    good + "}{", "{" + good, '{"sentences": [}', good.replace(":", "", 1) + "}"):
            self.assertIsNone(w._json(raw), raw)



def art_facts():
    """Art at a C, 1.0 from a B, with a what-if on Projects (GE's headline_xclass fixture)."""
    return [{"id": "next:a", "kind": "next_letter", "class": "Art", "pct": 79.0, "letter": "C", "next_letter": "B",
             "threshold": 80.0, "gap": 1.0},
            {"id": "whatif:a", "kind": "what_if", "class": "Art", "bucket": "Projects", "bucket_weight_pct": 40.0,
             "points": 20.0, "target": 80.0, "needed_pct": 84.1, "reachable": True}]


class CrossClassHeadlineTests(unittest.TestCase):
    """GE's must-fix on #52 a458198: a headline put one class's {class:} lead on another class's step."""

    def setUp(self):
        self.facts = geo2_facts() + art_facts()
        model = {f["id"]: f for f in w.model_facts(self.facts)}
        self.geo_step, self.art_step = model["whatif:g"]["say_step"], model["whatif:a"]["say_step"]
        self.stories = [{"class": "Geo", "story": "...", "slotted": self.geo_step, "cites": ["next:g", "whatif:g"]},
                        {"class": "Art", "story": "...", "slotted": self.art_step, "cites": ["next:a", "whatif:a"]}]

    def headline(self, text, cites):
        class Stub:
            def complete(self, system, user):
                return json.dumps({"text": text, "cites": cites})
        return w.write_headline(Stub(), self.facts, self.stories)

    def forms(self, step, lead_id, letter_id, gap):
        lower = step[0].lower() + step[1:]
        return [(step, []), ("{class:%s}: %s" % (lead_id, lower), []),
                ("{class:%s} is %s; %s" % (lead_id, gap, lower), [letter_id])]

    def test_two_what_ifs_from_different_classes_drop_in_every_form_and_order(self):
        geo_gap, art_gap = "1 pts from a C", "1 pts from a B"
        for step, own, other, letter, gap in ((self.geo_step, "whatif:g", "whatif:a", "next:g", geo_gap),
                                              (self.art_step, "whatif:a", "whatif:g", "next:a", art_gap)):
            for lead in (own, other):
                for text, extra in self.forms(step, lead, letter, gap):
                    for cites in ([own, other] + extra, [other, own] + extra, extra + [other, own]):
                        self.assertIsNone(self.headline(text, cites), (text, cites))
                        self.assertEqual(w.check_sentence(text, cites, w.fact_index(self.facts), headline=True),
                                         w.FORM_REASON, (text, cites))

    def test_ges_repro_drops(self):
        for text, cites in (("{class:whatif:a}: a 74 or better on the {next:whatif:g} gets it to a C.", ["whatif:g", "whatif:a"]),
                            ("{class:whatif:a} is 1.4 pts from a C; a 74 or better on the {next:whatif:g} gets it to a C.",
                             ["next:g", "whatif:g", "whatif:a"]),
                            ("{class:whatif:g}: an 85 or better on the {next:whatif:a} gets it to a B.", ["whatif:a", "whatif:g"])):
            self.assertIsNone(self.headline(text, cites), text)

    def test_a_class_lead_or_gap_from_another_class_drops(self):
        lower = self.geo_step[0].lower() + self.geo_step[1:]
        for text, cites in (("{class:next:a}: " + lower, ["next:a", "whatif:g"]),
                            ("{class:whatif:g} is 1 pts from a B; " + lower, ["next:a", "whatif:g"]),
                            ("{class:whatif:g} is 1 pts from a C; " + lower, ["next:a", "whatif:g"]),
                            ("{class:next:a} is 1 pts from a B; " + lower, ["next:a", "whatif:g"]),
                            (self.geo_step, ["next:a", "whatif:g"]), ("Art: " + lower, ["whatif:g"]),
                            ("Art: " + lower, ["next:a", "whatif:g"])):
            self.assertIsNone(self.headline(text, cites), (text, cites))

    def test_the_same_class_headlines_pass(self):
        for step, wid, letter, gap, name in ((self.geo_step, "whatif:g", "next:g", "1 pts from a C", "Geo"),
                                             (self.art_step, "whatif:a", "next:a", "1 pts from a B", "Art")):
            lower = step[0].lower() + step[1:]
            for text, cites in ((step, [wid]), (step, [letter, wid]), ("{class:%s}: %s" % (wid, lower), [wid]),
                                ("{class:%s}: %s" % (letter, lower), [letter, wid]), ("%s: %s" % (name, lower), [wid]),
                                ("{class:%s} is %s; %s" % (wid, gap, lower), [letter, wid]),
                                ("%s is %s; %s" % (name, gap, lower), [wid, letter])):
                got = self.headline(text, cites)
                self.assertIsNotNone(got, (text, cites))
                self.assertNotIn("{", got["text"])
        self.assertEqual(self.headline("{class:whatif:g}: " + self.geo_step[0].lower() + self.geo_step[1:], ["whatif:g"])["text"],
                         "Geo: a 74 or better on the next test gets it to a C.")

    def test_a_story_class_prefix_is_its_own_facts_class(self):
        # GE: the story prefix is safe. A {class:} slot may only be the cited fact's own; another class's drops.
        idx = w.fact_index(w.model_facts(self.facts))
        model = {f["id"]: f for f in w.model_facts(self.facts)}
        def low(t):
            return t[0].lower() + t[1:]
        for own, text in (("next:g", model["next:g"]["say_opening"]), ("move:g", model["move:g"]["say_opening"]),
                          ("pat:g", "Scores usually post Thursdays."), ("whatif:g", self.geo_step)):
            prefixed = "{class:%s} %s" % (own, low(text))
            if own != "whatif:g":
                self.assertIsNone(w.check_sentence(prefixed, [own], idx, "Geo", ["Geo", "Art"]), prefixed)
            for other in ("next:a", "whatif:a"):
                swapped = "{class:%s} %s" % (other, low(text))
                for cites in ([own], [own, other], [other, own]):
                    self.assertIsNotNone(w.check_sentence(swapped, cites, idx, "Geo", ["Geo", "Art"]), (swapped, cites))
                    self.assertIsNotNone(w.check_sentence(swapped, cites, idx, "Art", ["Geo", "Art"]), (swapped, cites))
            # A same-class {class:} slot from a different fact is off the form too.
            for same in ("next:g", "move:g", "pat:g", "whatif:g"):
                if same != own:
                    self.assertIsNotNone(w.check_sentence("{class:%s} %s" % (same, low(text)), [own, same], idx, "Geo"))


class HostileTitleFillTests(unittest.TestCase):
    """GE's info note on a458198: a title with a newline or one that looks like a slot."""

    def check(self, title):
        facts = geo2_facts()
        facts[1]["causes"][0]["title"] = title
        idx = w.fact_index(w.model_facts(facts))
        text = "Mostly the {item:cause:g:a}."
        reason = w.check_sentence(text, ["move:g", "cause:g:a"], idx, "Geo", ["Geo"])
        return reason, (w.fill_slots(text, w.fact_index(facts)) if reason is None else None)

    def test_fills_are_one_line_without_braces(self):
        self.assertEqual(self.check("Quiz\n  3\tPart 2"), (None, "Mostly the Quiz 3 Part 2 (20/40)."))
        self.assertEqual(self.check("Lab {x}"), (None, "Mostly the Lab x (20/40)."))
        self.assertEqual(self.check("Test}"), (None, "Mostly the Test (20/40)."))
        self.assertEqual(self.check("Unit 3: Part 2"), (None, "Mostly the Unit 3: Part 2 (20/40)."))

    def test_a_title_that_looks_like_a_slot_or_an_id_drops(self):
        for title in ("{item:cause:g:b}", "{item:cause:g:zz}", "item:cause:g:b", "{next:whatif:g}", "[class: Geo]",
                      "cause:g:b", "move:g", "pattern:posting_day:geo"):
            reason, out = self.check(title)
            self.assertIsNotNone(reason, (title, out))



class CannotDropWriterTests(unittest.TestCase):
    """GE's #55 finding, writer side: the can't-drop form only for a named next item at its posted points."""

    LINE = "The {next:%s} can't drop it below an A." % KEEP_LAB

    def test_an_unnamed_cannot_drop_fact_is_never_said(self):
        facts = chem_keep_facts(cannot_lose=True, named=False)
        keep = [f for f in facts if w.is_keep(f)][0]
        self.assertIsNone(w.say_keep(keep))
        self.assertIsNone(w.pick_keep(facts))
        self.assertEqual([f for f in w.model_facts(facts) if w.is_keep(f)], [])
        idx = w.fact_index(facts)
        self.assertEqual(w.check_sentence(self.LINE, [KEEP_LAB], idx, "Chem"), w.FORM_REASON)
        story = w.write_class(ScriptedModel({"Chem": [KeepTheLetterWriterTests.OPEN, KeepTheLetterWriterTests.CAUSE,
                                                       {"text": self.LINE, "cites": [KEEP_LAB]}]}), facts, "Chem")
        self.assertEqual(len(story["dropped"]), 1)

    def test_the_cannot_drop_form_on_a_needed_score_fact_drops(self):
        idx = w.fact_index(chem_keep_facts())
        self.assertIsNotNone(w.check_sentence(self.LINE, [KEEP_LAB], idx, "Chem"))
        named = chem_keep_facts()
        named[-1] = {**named[-1], "next_title": "Lab 9"}
        self.assertIsNotNone(w.check_sentence(self.LINE, [KEEP_LAB], w.fact_index(named), "Chem"))

    def test_a_named_cannot_drop_fact_passes_and_the_score_form_stays(self):
        facts = chem_keep_facts(cannot_lose=True)
        idx = w.fact_index(facts)
        self.assertIsNone(w.check_sentence(self.LINE, [KEEP_LAB], idx, "Chem"))
        self.assertEqual(w.fill_slots(self.LINE, idx), "The Lab 9 can't drop it below an A.")
        self.assertIsNone(w.check_sentence(KeepTheLetterWriterTests.KEEP["text"], [KEEP_LAB], w.fact_index(chem_keep_facts()), "Chem"))

    def test_every_emitted_cannot_drop_sentence_is_true(self):
        # Brute force through the engine and the writer: random A-to-D classes, a next item of the usual size or
        # bigger, named on the page or not. Every can't-drop sentence the writer would allow must hold at the
        # lowest score the class allows, on the points of the item it names.
        rng = random.Random(55)
        emitted = 0
        for _ in range(1500):
            buckets = []
            for i in range(rng.choice([1, 2, 3])):
                rows = []
                for j in range(rng.randint(1, 10)):
                    p = float(rng.choice([5, 10, 20, 25, 40, 50, 100]))
                    rows.append(insights.Score(f"B{i}{j}", "", f"B{i}", round(rng.uniform(0.8, 1.0) * p, 1), p))
                buckets.append(insights.Bucket(f"B{i}", rng.randint(5, 70), tuple(rows)))
            m = insights.ClassModel("X", "percent_of_grade", tuple(buckets), None, rng.choice([None, 100.0]))
            g = insights.truncated_grade(m)
            if g is None or insights.letter_floor(g) is None:
                continue
            floor = insights.letter_floor(g)[1]
            b = rng.choice(m.buckets)
            real = sorted(s.possible for s in b.scores)[len(b.scores) // 2] * rng.choice([1, 1, 2, 3, 5])
            named = rng.random() < 0.5
            ups = [(date(2026, 10, 27), "Next One", b.name, float(real))] if named else []
            keep = insights.keep_fact(m, g, ups, {"delta": -1.0, "causes": []})
            if not keep:
                continue
            move = {"id": "move:x", "kind": "grade_move", "class": "X", "delta": -1.0, "from_pct": g + 1, "to_pct": g,
                    "causes": []}
            facts = w.attach_next_items([move, keep], [{"class": "X", "bucket": b.name, "points": float(real),
                                                        "title": "Next One", "due": "2026-10-27"}] if named else [])
            line = "The {next:%s} can't drop it below %s %s." % (keep["id"], "an" if keep["letter"] in "AF" else "a",
                                                                   keep["letter"])
            if w.check_sentence(line, [keep["id"]], w.fact_index(facts), "X", ["X"]) is not None:
                continue
            emitted += 1
            self.assertEqual(facts[1].get("next_title"), "Next One")
            self.assertTrue(insights.holds(m, keep["bucket"], insights._score_range(m)[0], float(real), floor),
                            (keep, real))
        self.assertGreater(emitted, 50)


if __name__ == "__main__":
    unittest.main()
