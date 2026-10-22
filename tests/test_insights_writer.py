"""The local write-up: synthetic facts only, and a scripted model in place of a real one."""

import json
import re
import sys
import tempfile
import unittest
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
    {"text": "Math: B, 2.1 pts from an A.", "cites": [NEXT]},
    {"text": "Down mainly from a 62% on the Ch.4 Quiz (tests are 60% of the grade).", "cites": [CAUSE]},
    {"text": "Scores mostly post on Fridays.", "cites": [PATTERN]},
    {"text": "An 88% on the next test gets the A back.", "cites": [WHATIF]},
]


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


class AcceptanceBarTests(unittest.TestCase):
    def test_a_good_story_passes(self):
        story = w.write_class(ScriptedModel({"Math": GOOD}), math_facts(), "Math")
        self.assertEqual(story["story"], " ".join(s["text"] for s in GOOD))
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
        self.assertIsNone(w.check_sentence(GOOD[0]["text"], [NEXT], index()))
        self.assertEqual(w.check_sentence("Math: B, 3.4 pts from an A.", [NEXT], index()),
                         "number 3.4 isn't in the cited facts")
        self.assertEqual(w.check_sentence("Math: C, 2.1 pts from an A.", [NEXT], index()),
                         "letter C isn't in the cited facts")
        self.assertEqual(w.check_sentence("A 95% on the next test gets the A back.", [NEXT], index()),
                         "number 95 isn't in the cited facts")

    def test_cites_must_exist_and_belong_to_the_class(self):
        self.assertEqual(w.check_sentence("Math: B.", [], index()), "cites nothing")
        self.assertEqual(w.check_sentence("Math: B.", ["next:art"], index()), "unknown fact next:art")
        both = w.fact_index(math_facts() + quiet_facts())
        self.assertEqual(w.check_sentence("Biology: B.", ["next:biology"], both, "Math"),
                         "fact next:biology is another class")

    def test_a_weekday_needs_a_pattern_naming_it(self):
        self.assertIsNone(w.check_sentence("Scores mostly post on Fridays.", [PATTERN], index()))
        self.assertIsNotNone(w.check_sentence("Scores mostly post on Mondays.", [PATTERN], index()))

    def test_aeries_truncates_rather_than_rounds(self):
        facts = math_facts()
        facts[0] = {**facts[0], "pct": 87.96, "gap": 2.04}
        self.assertIsNone(w.check_sentence("Math: B at 87.9%.", [NEXT], w.fact_index(facts)))
        self.assertIsNone(w.check_sentence("Math: B at 88%.", [NEXT], w.fact_index(facts)))

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
        self.assertIsNone(w.check_sentence("Down 1.6 pts since the last check.", [MOVE], idx))

    def test_no_explained_allows_no_cause(self):
        move = self.move(None)
        self.assertFalse(w.cause_allowed(move))
        self.assertIsNotNone(w.check_sentence(self.CAUSE_LINE, [CAUSE], w.fact_index([move])))

    def test_explained_close_to_delta_allows_the_cause(self):
        move = self.move(-1.5)
        self.assertTrue(w.cause_allowed(move))
        self.assertIsNone(w.check_sentence(self.CAUSE_LINE, [CAUSE], w.fact_index([move])))

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
        self.assertIsNone(w.check_sentence("An eighty-eight on the next test gets the A back.", [WHATIF], index()))
        self.assertIsNone(w.check_sentence("Math: B, two point one pts from an A.", [NEXT], index()))
        self.assertEqual(w.spelled_to_digits("no one, one of them, twenty one points"),
                         "no one, one of them, 21 points")

    def test_a_plus_or_minus_letter_is_rejected(self):
        for text in ("Math is a C+.", "Math is a B-.", "Math is a B+ now.", "Math is a B plus."):
            self.assertIn("whole-letter scale", w.check_sentence(text, [NEXT], index()), text)
        self.assertIsNone(w.check_sentence("Math is a B.", [NEXT], index()))

    def test_cause_wording_must_cite_a_move_or_cause(self):
        self.assertEqual(w.check_sentence("Down mainly because of the Ch.4 Quiz.", [WHATIF], index()),
                         "states a cause without citing a grade_move or cause")
        for text in ("Math slipped due to the Ch.4 Quiz.", "The drop was driven by the Ch.4 Quiz."):
            self.assertEqual(w.check_sentence(text, [NEXT], index()),
                             "states a cause without citing a grade_move or cause", text)
        self.assertIsNone(w.check_sentence("Down mainly because of the Ch.4 Quiz.", [CAUSE], index()))

    def test_digits_after_a_period_are_checked(self):
        self.assertEqual(w.check_sentence("Down mainly from the Ch.9 quiz.", [CAUSE], index()),
                         "number 9 isn't in the cited facts")
        self.assertIsNone(w.check_sentence("Down mainly from the Ch.4 Quiz.", [CAUSE], index()))

    def test_a_number_must_match_the_field_the_claim_is_about(self):
        self.assertEqual(w.check_sentence("Math is up 60 percent after the Ch.4 Quiz.", [CAUSE], index()),
                         "number 60 isn't in the cited facts as a move")
        self.assertEqual(w.check_sentence("Math: B, 60 pts from an A.", [NEXT, WHATIF], index()),
                         "number 60 isn't in the cited facts")
        self.assertEqual(w.check_sentence("Tests are 88% of the grade.", [WHATIF], index()),
                         "number 88 isn't in the cited facts as a weight")
        self.assertIsNone(w.check_sentence("Tests are 60% of the grade.", [WHATIF], index()))
        self.assertIsNone(w.check_sentence("Down 2.5 pts since the last check.", [MOVE], index()))

    def test_a_score_fraction_is_not_a_date(self):
        self.assertIsNone(w.check_sentence("Down mainly from a 31/50 on the Ch.4 Quiz.", [CAUSE], index()))
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
        self.assertIsNone(w.check_sentence(line, [WHATIF], idx))
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
        self.assertIsNone(w.check_sentence("An 88% on the next test gets the A back.", [WHATIF], idx))
        self.assertEqual(w.check_sentence("An 88% on the Ch. 5 Test, due Oct 30, gets the A back.", [WHATIF], idx),
                         "names a date")

    def test_an_item_of_another_size_or_bucket_is_not_attached(self):
        for item in (ch5(points=100.0), ch5(bucket="Quizzes")):
            self.assertNotIn("next_title", self.facts_with_item([item])[WHATIF])

    def test_a_posting_day_weekday_needs_the_pattern(self):
        self.assertIsNone(w.check_sentence("Scores usually post Fridays.", [PATTERN], index()))
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
            self.assertIsNone(w._claims_reason(sentence["text"], cited, idx), sentence["text"])
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
        self.assertIsNone(w.check_sentence(self.LINE, [CAUSE], w.fact_index([self.move(coverage=0.96)])))

    def test_a_trimmed_history_allows_no_cause(self):
        move = self.move(coverage=0.96, coverage_trimmed=True)
        self.assertFalse(w.cause_allowed(move))
        idx = w.fact_index([move])
        self.assertEqual(w.check_sentence(self.LINE, [CAUSE], idx), "states a cause the move's causes don't explain")
        self.assertEqual(w.check_sentence("Down 2.5 pts, mainly from quizzes.", [MOVE], idx),
                         "states a cause the move's causes don't explain")
        self.assertIsNone(w.check_sentence("Down 2.5 pts since the last check.", [MOVE], idx))
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
        self.assertIsNone(w.check_sentence(self.LINE, [CAUSE], idx))
        self.assertIsNone(w.check_sentence("Down from the Ch.4 Quiz, then HW 3.", [CAUSE, second], idx))
        self.assertEqual(w.check_sentence("Down mainly from HW 3.", [second], idx), "names causes out of share order")
        self.assertEqual(w.check_sentence("Down from HW 3 and the Ch.4 Quiz.", [second, CAUSE], idx),
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
        self.assertIsNone(w.check_sentence("An 85 or better on the next test gets there.", [GEO_WHATIF], idx))
        self.assertIsNone(w.check_sentence("An 84.2% on the next test gets the B.", [GEO_WHATIF], idx))
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
        self.assertIsNone(w.check_sentence("Scores mostly post on Fridays.", [PATTERN], index()))

    def test_back_to_a_letter_needs_a_move_from_that_letter(self):
        line = "An 85 or better on the next test gets it back to a B."
        self.assertEqual(w.check_sentence(line, [GEO_WHATIF], w.fact_index(geometry_facts())),
                         "says back to or again with a letter no move shows the class held")
        moved = geometry_facts() + [{"id": "move:geometry:2026-10-14", "kind": "grade_move", "class": "Geometry",
                                     "since": "2026-10-14", "from_pct": 81.0, "to_pct": 79.5, "delta": -1.5,
                                     "explained": None, "coverage": None, "coverage_trimmed": False, "causes": []}]
        self.assertIsNone(w.check_sentence(line, [GEO_WHATIF], w.fact_index(moved)))
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
        self.assertIsNone(w.check_sentence(line, [geo_next], w.fact_index(crossed)))
        held = crossed[:-1] + [dict(crossed[-1], from_pct=79.9)]
        self.assertEqual(w.check_sentence(line, [geo_next], w.fact_index(held)),
                         "says the class moved to C but no move crossed into that letter")
        self.assertEqual(w.check_sentence("Slipped to a C this term.", [geo_next], w.fact_index(geometry_facts())),
                         "says the class moved to C but no move crossed into that letter")
        self.assertIsNone(w.check_sentence("Still a C, 0.5 points from a B.", [geo_next], w.fact_index(geometry_facts())))

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
        self.assertIsNone(w.check_sentence("Down 1.6, mainly due to a 40% on the Lab 3 Packet.", [CHEM_MOVE, CHEM_C1], chem))
        self.assertIsNone(w.check_sentence("Down 1.6, mainly due to a 4/10 on the Lab 3 Packet.", [CHEM_MOVE, CHEM_C1], chem))

    def test_a_title_written_without_its_parenthetical_is_still_a_name(self):
        facts = chem_facts()
        facts[0]["causes"][1]["title"] = "Warm Ups (wk9-10)"
        idx = w.fact_index(facts)
        self.assertIsNone(w.check_sentence("The Lab 3 Packet (4/10) and Warm Ups (1/3) hurt the grade most.",
                                           [CHEM_C1, CHEM_C2], idx))

    def test_an_offsetting_cause_may_be_named_but_not_as_the_reason(self):
        facts = chem_facts()
        facts[0]["causes"].append({"id": "cause:chem:2026-10-14:rocketlab", "title": "Rocket Lab",
                                   "category": "Coursework", "bucket": "Coursework", "bucket_weight_pct": 10.0,
                                   "change": "new", "earned": 9.0, "possible": 10.0, "points": 0.8, "share": 0.4})
        idx = w.fact_index(facts)
        self.assertIsNone(w.check_sentence("The Rocket Lab (9/10) helped a little.",
                                           ["cause:chem:2026-10-14:rocketlab"], idx))
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
        self.assertIsNone(w.check_sentence("The Lab 3 Packet (4/10) hurt the grade, while the Rocket Lab (9/10) helped.",
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

    def test_a_sentence_copied_from_the_example_is_dropped(self):
        idx = w.fact_index(geometry_facts())
        for text in ("Slipped to a C since Sep 9, 0.5 points from a B.", "The Glaze Test (11/20) did most of the damage.",
                     "A 74 or better on the next test gets there."):
            self.assertIn("copies the made-up example", w.check_sentence(text, [GEO_NEXT, GEO_WHATIF], idx), text)

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
        self.assertIsNone(w.check_sentence("Half a point from a B.", [GEO_NEXT], idx))
        self.assertEqual(w.check_sentence("A point and a half from a B.", [GEO_NEXT], idx),
                         "number 1.5 isn't in the cited facts")
        quarter = [dict(geometry_facts()[0], gap=0.25, pct=79.75)]
        self.assertIsNone(w.check_sentence("A quarter point from a B.", [GEO_NEXT], w.fact_index(quarter)))

    def test_the_good_notes_pass(self):
        geo = w.fact_index(geometry_facts())
        self.assertIsNone(w.check_sentence("Half a point from a B.", [GEO_NEXT], geo))
        self.assertIsNone(w.check_sentence("An 85 or better on the next test gets there.", [GEO_WHATIF], geo))
        self.assertIsNone(w.eval_story([{"text": "Half a point from a B.", "cites": [GEO_NEXT]},
                                        {"text": "An 85 or better on the next test gets there.",
                                         "cites": [GEO_WHATIF]}], geo))
        chem = w.fact_index(chem_facts())
        line = "Still an A, down 1.6 since Oct 14, mostly from the Lab 3 Packet (4/10)."
        self.assertIsNone(w.check_sentence(line, [CHEM_MOVE, CHEM_C1], chem))
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
        self.assertIsNone(w.check_sentence("Aim for an 88% on the next test to get the A back.", [WHATIF], index()))

    def test_talk_of_a_move_needs_one_and_nothing_is_said_about_what_isnt_there(self):
        geo = w.fact_index(geometry_facts())
        self.assertEqual(w.check_sentence("No major assignments explained the move.", [GEO_NEXT], geo),
                         "says what isn't there")
        self.assertEqual(w.check_sentence("Nothing big explains the change.", [GEO_NEXT], geo),
                         "talks about a move with no grade_move cited")
        for line in ("No pattern data available.", "No major cause identified.", "Scores: none identified."):
            self.assertEqual(w.check_sentence(line, [GEO_NEXT], geo), "says what isn't there", line)
        chem = w.fact_index(chem_facts())
        self.assertIsNone(w.check_sentence("The Lab 3 Packet (4/10) drove the drop.", [CHEM_C1], chem))

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
        self.assertIsNone(w.check_sentence(drafted[0]["text"], drafted[0]["cites"], geo))
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
        for text, cites in (("Math: an 88% on the next test gets the A back.", [WHATIF]),
                            ("The Ch.4 Quiz pulled Math down.", [CAUSE])):
            up = w.write_up(ScriptedModel({"Math": GOOD}, headline={"text": text, "cites": cites}), insight_facts())
            self.assertEqual(up["headline"], {"text": text, "cites": cites})
        fixed = w.write_up(ScriptedModel({"Math": GOOD}, headline={
            "text": "Math: a 88% on the next test gets the A back.", "cites": [WHATIF]}), insight_facts())
        self.assertEqual(fixed["headline"]["text"], "Math: an 88% on the next test gets the A back.")


class HeadlineAndTakeoverTests(unittest.TestCase):
    def test_write_up_headline_and_stories(self):
        model = ScriptedModel({"Math": GOOD}, headline={
            "text": "Math is 2.1 pts from an A; an 88% on the next test gets it back.", "cites": [NEXT, WHATIF]})
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
        self.assertIsNone(w.check_sentence(line, [step["id"]], idx, "Biology"))
        cause = move["causes"][0]
        said = f"Down mainly from a {cause['earned'] / cause['possible'] * 100:.0f}% on the {cause['title']}."
        expected = None if w.cause_allowed(move) else "states a cause the move's causes don't explain"
        self.assertEqual(w.check_sentence(said, [cause["id"]], idx, "Biology"), expected)


if __name__ == "__main__":
    unittest.main()
