"""The local write-up: synthetic facts only; code writes every sentence."""

import copy
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
import insights_runner as runner  # noqa: E402
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
# GOOD's own step is about an unnamed next test, so code says it instead, stating the size it assumes (the
# what_if's 50 points): 88% of 50 is 44.
ASSUMED_STEP = "If the next test is worth about 50 points like the last ones, a 44 or better gets it to an A."
# The code-only story for math_facts: the move's opening, then the assumed step.
MATH_CODE_STORY = "Slipped to a B since Oct 15, down 2.5. " + ASSUMED_STEP


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


def filled(sentences, idx):
    """The story text the writer gives after filling each sentence's slots."""
    return " ".join(w.fix_articles(w.fill_slots(s["text"], idx)) for s in sentences)


class AcceptanceBarTests(unittest.TestCase):
    def test_math_gets_code_s_story(self):
        story = w.write_class(math_facts(), "Math")
        self.assertEqual(story["story"], MATH_CODE_STORY)
        self.assertEqual(story["cites"], [MOVE, WHATIF])
        self.assertNotIn("dropped", story)

    def test_a_past_due_only_story_fails(self):
        past_due = [{"text": "Math has 2 missing assignments past due.", "cites": [NEXT]}]
        self.assertEqual(w.eval_story(past_due, index()), "restates past-due or missing work")

    def test_code_s_story_needs_no_insight_kind(self):
        # Every class gets a story: one that cites only its next letter still passes.
        self.assertIsNone(w.eval_story([{"text": "Still a B, 2.1 points from an A.", "cites": [NEXT]}], index()))

    def test_restating_is_a_fail_even_beside_an_insight(self):
        mixed = GOOD[:1] + [{"text": "The Ch.4 Quiz retake is still missing.", "cites": [WHATIF]}]
        self.assertEqual(w.eval_story([dict(s) for s in mixed], index()), "restates past-due or missing work")

    def test_the_insight_comes_in_order(self):
        backwards = [GOOD[0], GOOD[3], GOOD[2]]
        self.assertEqual(w.eval_story([dict(s) for s in backwards], index()),
                         "out of order (letter, cause, pattern, next step)")

    def test_a_quiet_class_gets_code_s_story(self):
        # Every class gets a story, even with nothing to say past its letter.
        story = w.write_class(quiet_facts(), "Biology")
        self.assertEqual((story["story"], story["cites"]), ("Still a B, 6 points from an A.", ["next:biology"]))


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

    def test_a_contains_story_goes_to_at_most_one_class(self):
        # Two classes both contain the one "Biology" story: it could fit either, so it lands on neither.
        out = w.apply_write_up({"classes": [{"class_name": "P3 Biology"}, {"class_name": "P5 Biology Lab"}]},
                               {"stories": [{"class": "Biology", "story": "bio"}]})
        self.assertEqual([c["story"] for c in out["classes"]], ["", ""])
        # Once one class has it by exact name, the other gets nothing.
        out = w.apply_write_up({"classes": [{"class_name": "Biology Lab"}, {"class_name": "Biology"}]},
                               {"stories": [{"class": "Biology", "story": "bio"}]})
        self.assertEqual([c["story"] for c in out["classes"]], ["", "bio"])
        # Two classes with the same name: the story goes to the first only.
        picks = w.match_stories([{"class": "Art", "story": "art"}], ["Art", "ART"])
        self.assertEqual([p and p["story"] for p in picks], ["art", None])


class PublishLimitTests(unittest.TestCase):
    """Cites are deduped; a story over family-data's limits (or with no cites) is dropped alone."""

    def test_duplicate_cites_are_deduped_in_order(self):
        self.assertEqual(w._unique(["c", "a", "b", "a", "c"]), ["c", "a", "b"])
        story = w.write_class(math_facts(), "Math")
        self.assertEqual(story["cites"], [MOVE, WHATIF])
        self.assertTrue(w.story_fits(story))

    def test_five_sentences_of_three_duplicate_cites_pass(self):
        cites = w._unique([c for i in range(5) for c in [f"fact:{i}"] * 3])
        self.assertEqual(len(cites), 5)
        self.assertTrue(w.within_publish_limits("Five short sentences. " * 5, cites, "Math"))

    def test_an_oversize_story_is_dropped_and_the_others_stay(self):
        ok = {"class": "Math", "story": "Still a B.", "cites": ["a"]}
        edge = {"class": "Art", "story": "z" * 600, "cites": [f"f{i}" for i in range(12)]}
        many = {"class": "Biology", "story": "Fine text.", "cites": [f"f{i}" for i in range(13)]}
        long = {"class": "Chem", "story": "x" * 601, "cites": ["a"]}
        bare = {"class": "Spanish", "story": "No cites.", "cites": []}
        stories = iter([ok, edge, many, long, bare])
        with mock.patch.object(w, "class_names", return_value=["Math", "Art", "Biology", "Chem", "Spanish"]), \
                mock.patch.object(w, "write_class", side_effect=lambda *a, **k: next(stories)):
            up = w.write_up({"as_of": "2026-10-22", "facts": []})
        # Published in class-name order (story_order), whatever order the classes came in.
        self.assertEqual([s["class"] for s in up["stories"]], ["Art", "Math"])
        public = w.public_write_up({**up, "stories": [ok, edge, many, long, bare]})
        self.assertEqual([s["class"] for s in public["stories"]], ["Math", "Art"])

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
        self.assertEqual(w._say_fields(geometry_facts()[1])["say_needed"], "85 or better")
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
            self.assertRegex(claims(text, cites, idx, "Geo") or "", r"outside a slot|another class", text)
        # Slots: the id must be cited, and an item slot needs a fact with a title.
        self.assertIn("doesn't cite", claims("The {item:cause:g:t} did most of the damage.", ["move:g"], idx))
        self.assertEqual(claims("Mostly the {item:move:g}.", ["move:g"], idx), "item slot move:g has no title")
        self.assertEqual(claims("Mostly the {item:cause:g:t.", ["cause:g:t"], idx), "malformed slot")
        self.assertIn("isn't a what_if", claims("A 74 or better on the {next:cause:g:t}.", ["cause:g:t"], idx))
        for text, cites in (("The {item:cause:g:t} did most of the damage.", ["cause:g:t"]),
                            ("Mostly the {item:cause:g:t}.", ["cause:g:t"]),
                            ("{class:pat:g} scores usually post Thursdays.", ["pat:g"]),
                            ("Scores usually post Thursdays.", ["pat:g"]),
                            ):
            self.assertIsNone(claims(text, cites, idx, "Geo"), text)
        # A cause named without a slot passes the claim checks but not the form check (no cause sentence passes it).
        self.assertIsNone(claims("Low scores dragged it.", ["cause:g:t"], idx, "Geo"))
        self.assertEqual(w.check_sentence("Low scores dragged it.", ["cause:g:t"], idx, "Geo"), w.FORM_REASON)
        self.assertEqual(w.fill_slots("The {item:cause:g:t} did most of the damage.", idx),
                         "The Unit Test (20/40) did most of the damage.")
        self.assertEqual(w.fill_slots("{class:pat:g} scores usually post Thursdays.", idx),
                         "Geo scores usually post Thursdays.")
        # A score the model writes after the slot is its own, and checked.
        self.assertEqual(w.fill_slots("The {item:cause:g:t} (20/40) hurt.", idx), "The Unit Test (20/40) hurt.")
        self.assertEqual(claims("The {item:cause:g:t} (21/40) hurt.", ["cause:g:t"], idx),
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
        self.assertIsNone(check("Slipped to a D, down 2 (10/14).", ["move:c"], w.fact_index([moved])))
        # Any other pair in parentheses is read as a score, and no fact backs it.
        self.assertEqual(w.check_sentence("Slipped to a D, down 2 (10/15).", ["move:c"], w.fact_index([moved])),
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
        self.assertEqual(w.say_opening(w._say_fields(facts[0])), "Slipped to a D since Sep 9, down 2.3.")
        self.assertIsNone(check("Slipped to a D since Sep 9, down 2.3.", ["move:art:2026-09-09"], idx, "Art"))
        self.assertIsNone(check("Scores usually post Tuesdays.", ["pattern:posting_day:art"], idx, "Art"))
        self.assertIsNone(check("A 74 or better on the next project gets it to a C.",
                                           ["whatif:art:projects:20"], idx, "Art"))
        # A needed 73.0 is said "73 or better" (round up only).
        exact = [dict(f, needed_pct=73.0) if f["kind"] == "what_if" else f for f in facts]
        self.assertEqual(w._say_fields(exact[1])["say_needed"], "73 or better")
        self.assertIsNone(check("A 73 or better on the next project gets it to a C.", ["whatif:art:projects:20"],
                                w.fact_index(exact), "Art"))

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
        self.assertEqual(w._say_fields(facts[1])["say_needed"], "74 or better")
        self.assertEqual(w.write_class(facts, "Geo")["story"], "Slipped to a D since Oct 14, down 2.5.")
        # Review repro: a real move since Sep 9 gets the code's own opener, which must pass.
        sep = [dict(facts[0], id="move:geo:2026-09-09", since="2026-09-09", causes=[])]
        opener = w.say_opening(w._say_fields(sep[0]))
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
        self.assertEqual(w._say_fields(chem_facts()[0])["since_label"], "Oct 14")


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
        # An A has no what-if, and no advice is said.
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

    def test_code_s_ready_openings(self):
        geo = w.say_opening(w._say_fields(geometry_facts()[0]))
        self.assertEqual(geo, "Still a C, 0.5 points from a B.")
        chem = w.say_opening(w._say_fields(chem_facts()[0]))
        self.assertEqual(chem, "Still an A, down 1.6 since Oct 14.")
        crossed = dict(chem_facts()[0], from_pct=80.4, to_pct=78.8)
        self.assertEqual(w.say_opening(w._say_fields(crossed)), "Slipped to a C since Oct 14, down 1.6.")
        risen = dict(chem_facts()[0], from_pct=78.8, to_pct=80.4, delta=1.6, causes=[])
        self.assertEqual(w.say_opening(w._say_fields(risen)), "Up to a B since Oct 14, up 1.6.")
        # The ready openings pass the checker and the opening rule as written.
        gidx, cidx = w.fact_index(geometry_facts()), w.fact_index(chem_facts())
        self.assertIsNone(check(geo, [GEO_NEXT], gidx))
        self.assertIsNone(w.eval_story([{"text": geo, "cites": [GEO_NEXT]},
                                        {"text": "An 85 or better on the next test gets there.", "cites": [GEO_WHATIF]}], gidx))
        self.assertIsNone(check(chem, [CHEM_MOVE], cidx))
        self.assertIsNone(w.eval_story([{"text": chem, "cites": [CHEM_MOVE]}], cidx))

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
        fixed = w.fix_articles("A 85 or better on the next test gets there.")
        self.assertEqual(fixed, "An 85 or better on the next test gets there.")
        geo = w.fact_index(geometry_facts())
        self.assertIsNone(check(fixed, [GEO_WHATIF], geo))
        self.assertEqual(w.check_sentence(w.fix_articles("A 84 or better on the next test gets there."), [GEO_WHATIF], geo),
                         "number 84 isn't the needed score or a round-up of it")

class CtoRoundFourTests(unittest.TestCase):
    """Singular bucket fills and the next-slot prompt."""

    ALG = "whatif:algllbusmang:assessment:10"

    def alg_facts(self):
        return [{"id": "next:algllbusmang", "kind": "next_letter", "class": "Alg ll Bus Mang", "pct": 79.5, "letter": "C",
                 "next_letter": "B", "threshold": 80.0, "gap": 0.5},
                {"id": self.ALG, "kind": "what_if", "class": "Alg ll Bus Mang", "bucket": "Assessment",
                 "bucket_weight_pct": 50.0, "points": 10.0, "target": 80.0, "needed_pct": 84.3, "reachable": True}]

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

    def test_a_what_if_in_an_item_slot_has_no_title(self):
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


LAB9 = [{"class": "Chem", "bucket": "Coursework", "title": "Lab 9", "due": "2026-10-23", "points": 10.0}]


def chem_named_keep_facts(**kw):
    """chem_keep_facts with Lab 9 named at the keep fact's posted points, so code says its keep sentence."""
    return w.attach_next_items(chem_keep_facts(**kw), LAB9)


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
    """The engine's keep_letter fact, said by code (code_lines), on made-up Chem data."""

    OPEN = {"text": "Still an A, down 1.6 since Oct 14.", "cites": [CHEM_MOVE]}
    CAUSE = {"text": "Mostly the {item:%s}." % CHEM_C1, "cites": [CHEM_C1]}
    KEEP = {"text": "A 62 or better on the {next:%s} keeps the A." % KEEP_LAB, "cites": [KEEP_LAB]}

    def test_keep_sentences_pass_the_checks(self):
        # Keep wording names no cause and cites no move: the keep form passes.
        idx = w.fact_index(chem_keep_facts())
        self.assertIsNone(w.check_sentence(self.KEEP["text"], [KEEP_LAB], idx, "Chem"))
        safe = w.fact_index(chem_keep_facts(cannot_lose=True))
        line = "The {next:%s} can't drop it below an A." % KEEP_LAB
        self.assertIsNone(w.check_sentence(line, [KEEP_LAB], safe, "Chem"))
        # Code writes the keep sentence last.
        story = w.write_class(chem_named_keep_facts(), "Chem")
        self.assertEqual(story["keep_sentence"], "A 62 or better on the Lab 9 keeps the A.")
        self.assertTrue(story["story"].endswith(story["keep_sentence"]))

    def test_code_says_the_keep_fact(self):
        # The keep wording names the item only through a {next:} slot; code fills it after the checks.
        keep = w.pick_keep(chem_named_keep_facts())
        self.assertEqual(w.say_keep(keep), "A 62 or better on the {next:%s} keeps the A." % KEEP_LAB)
        self.assertEqual(w.fill_slots(w.say_keep(keep), w.fact_index(chem_named_keep_facts())),
                         "A 62 or better on the Lab 9 keeps the A.")
        safe = w.pick_keep(chem_keep_facts(cannot_lose=True))
        self.assertEqual(w.say_keep(safe), "The {next:%s} can't drop it below an A." % KEEP_LAB)
        self.assertEqual(w.fill_slots(w.say_keep(safe), w.fact_index(chem_keep_facts(cannot_lose=True))),
                         "The Lab 9 can't drop it below an A.")
        # At a typical size, with no item named, code says nothing.
        self.assertIsNone(w.pick_keep(chem_keep_facts()))
        self.assertIsNone(w.pick_keep(chem_keep_facts(keep_id=KEEP_TEST)))
        self.assertIsNone(w.pick_keep(chem_keep_facts(cannot_lose=True, named=False)))
        # A named next item of the same bucket and size gives the keep step its title.
        upcoming = [{"class": "Chem", "bucket": "Tests", "title": "Unit 4 Test", "due": "2026-10-28", "points": 50.0}]
        named = w.attach_next_items(chem_keep_facts(keep_id=KEEP_TEST), upcoming)
        pick = w.pick_keep(named)
        self.assertEqual((pick["id"], w.fill_slots(w.say_keep(pick), w.fact_index(named))),
                         (KEEP_TEST, "A 72 or better on the Unit 4 Test keeps the A."))
        # The old what_if-with-target-keep shape is no longer a keep fact.
        self.assertFalse(w.is_keep({"kind": "what_if", "target": "keep"}))

    def test_a_drop_story_with_a_keep_fact_ends_on_it(self):
        idx = w.fact_index(chem_named_keep_facts())
        self.assertIsNone(w.eval_story([dict(self.OPEN), dict(self.CAUSE), dict(self.KEEP)], idx))
        # Code writes the keep sentence last.
        story = w.write_class(chem_named_keep_facts(), "Chem")
        self.assertEqual(story["story"], "Still an A, down 1.6 since Oct 14. A 62 or better on the Lab 9 keeps the A.")
        # With no item named, code says it at the size it assumes.
        story = w.write_class(chem_keep_facts(), "Chem")
        assumed = ("If the next coursework assignment is worth about 10 points like the last ones, a 7 or better "
                   "keeps the A.")
        self.assertEqual(story["story"], "Still an A, down 1.6 since Oct 14. " + assumed)
        self.assertEqual(story["keep_sentence"], assumed)

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
        self.assertEqual(w.fill_slots(w.say_keep(biology_facts()[2]), idx), "A 54 or better on the next test keeps the B.")

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
        phys = w.attach_next_items(phys, [{"class": "Physics & Eng", "bucket": "Assessments", "title": "Quiz 4",
                                           "due": "2026-10-23", "points": 10.0}])
        story = w.write_class(phys, "Physics & Eng")
        self.assertEqual(story["story"], "Still an A, down 1.6 since Oct 14. A 75 or better on the Quiz 4 keeps the A.")
        self.assertIsNone(check("The Unit 2 Packet (4/10) did most of the damage.", ["cause:p:packet"],
                                w.fact_index(phys)))

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

    def test_an_a_with_no_keep_fact_ends_on_the_cause(self):
        self.assertIsNone(w.eval_story([dict(self.OPEN), dict(self.CAUSE)], w.fact_index(chem_facts())))
        steady = [f for f in chem_keep_facts() if f.get("kind") != "grade_move"]
        self.assertIsNone(w.pick_keep(steady))

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
        self.assertIsNone(claims(line, ["cause:geo:t"], idx, "Geo"))
        self.assertEqual(w.fill_slots(line, idx), "Homework scores did most of the damage.")
        self.assertIn("doesn't cite", claims(line, ["move:geo"], idx, "Geo"))
        self.assertEqual(claims("Mostly {bucket:move:geo}.", ["move:geo"], idx, "Geo"),
                         "bucket slot move:geo has no bucket")
        # Cause wording on a what_if's bucket still needs a grade_move or cause.
        self.assertEqual(claims("{bucket:whatif:geo} did most of the damage.", ["whatif:geo"], idx, "Geo"),
                         "states a cause without citing a grade_move or cause")
        # Off the say_step's words ("the next {bucket:} score" for "the {next:}"): not an allowed form.
        self.assertEqual(w.check_sentence("A 74 or better on the next {bucket:whatif:geo} score gets it to a C.",
                                          ["whatif:geo"], idx, "Geo"), w.FORM_REASON)
        self.assertEqual(w.fill_slots("A 74 or better on the next {bucket:whatif:geo} score gets it to a C.", idx),
                         "A 74 or better on the next homework score gets it to a C.")
        # A cause sentence passes the claim checks at most; the form check drops every one (code writes none).
        facts = geo_facts(bucket="Homework")
        facts[1]["causes"].append({"id": "cause:geo:q", "title": "Quiz 2", "bucket": "Quizzes", "bucket_weight_pct": 20.0,
                                   "earned": 5.0, "possible": 10.0, "points": -0.5, "share": -0.4})
        idx2 = w.fact_index(facts)
        self.assertEqual(w.check_sentence("Mostly {bucket:cause:geo:q} scores.", ["cause:geo:q"], idx2, "Geo"),
                         w.FORM_REASON)
        self.assertIsNone(claims("Mostly {bucket:cause:geo:t} scores.", ["cause:geo:t"], idx2, "Geo"))
        self.assertIn("outside a slot", claims("Homework scores did most of the damage.", ["cause:geo:t"], idx, "Geo"))

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
            self.assertIsNone(claims(line, ["cause:geo:t"], idx, "Geo"), title)
        idx = self.idx(title="Final Exam - B+ curve")
        self.assertEqual(w.neutral_slots("The {item:cause:geo:t} did most of the damage.", idx),
                         "The Zitem (20/40) did most of the damage.")
        # What the model writes outside the slots is still checked.
        self.assertIsNotNone(w.check_sentence("The {item:cause:geo:t} cost a B+.", ["cause:geo:t"], idx, "Geo"))
        self.assertEqual(claims("The {item:cause:geo:t} (21/40) hurt.", ["cause:geo:t"], idx, "Geo"),
                         "score 21/40 isn't in the cited facts")
        self.assertEqual(claims("Down 3.5 since Oct 14, mostly the {item:cause:geo:t}.",
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
            self.assertRegex(claims(text, cites, idx, "Geo") or "", r"leaks a raw fact id|slot debris", text)
        # Times and scores aren't ids.
        self.assertIsNone(w.leak_reason("Scores post near 3:05, like the (20/40).", idx))
        self.assertIsNone(claims("The {item:cause:geo:t} did most of the damage.", ["cause:geo:t"], idx, "Geo"))
        # A hostile title can't smuggle an id or braces into the final text.
        bad = self.idx(title="Test {item:cause:geo:t}")
        self.assertEqual(w.leak_reason(w.fill_slots("Mostly the {item:cause:geo:t}.", bad), bad, filled=True),
                         "leaks a raw fact id")

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


class TakeoverTests(unittest.TestCase):
    def test_the_write_up_takes_every_story_slot_and_clears_the_model_headline(self):
        summary = {"headline": "Grok headline", "status": "Grok status", "focus_tonight": "Math: Worksheet", "classes": [
            {"class_name": "Math", "story": "Grok story", "snap": "s"},
            {"class_name": "Biology", "story": "Grok filler", "snap": "t"},
        ]}
        # An old stored write-up with a headline: it is never shown, rebuilt or led by its class.
        local = {"headline": {"class": "Geo", "text": "1.4 pts from a C; a 74 or better on the next test gets it to a C.",
                              "cites": [NEXT, WHATIF]},
                 "stories": [{"class": "Math", "story": "Local story", "cites": [WHATIF]}]}
        out = w.apply_write_up(summary, local)
        self.assertEqual(out["headline"], "")
        self.assertNotIn("status", out)
        self.assertEqual([c["story"] for c in out["classes"]], ["Local story", ""])
        self.assertEqual(out["focus_tonight"], "Math: Worksheet")
        self.assertEqual(summary["headline"], "Grok headline")
        self.assertEqual(w.apply_write_up(summary, {"stories": []})["headline"], "")

    def test_the_writer_has_no_headline_step(self):
        up = w.write_up(insight_facts())
        self.assertEqual(sorted(up), ["as_of", "stories"])
        for name in ("write_headline", "HEADLINE_SYSTEM", "headline_fits", "_headline_forms", "_headline_parts"):
            self.assertFalse(hasattr(w, name), name)


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
        put = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            work = w.WriterWork(lambda: [{"insight_facts": insight_facts()}], tmp, put=put, env={})
            steps = work.plan("2026-10-21")
            self.assertEqual(steps, ["student:0", "publish"])
            for step in steps:
                work.run_step("2026-10-21", step)
            with mock.patch.object(w, "write_up", side_effect=AssertionError("rewritten")):
                work.run_step("2026-10-21", "student:0")
            saved = json.loads((Path(tmp) / "2026-10-21" / "student-0.json").read_text())
            # Every class gets a story: Biology's is code's alone.
            self.assertEqual([s["class"] for s in saved["stories"]], ["Biology", "Math"])
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
        self.assertEqual((facts[1]["other_share"], facts[1]["other_causes"]), (None, 3))
        self.assertIsNotNone(w.write_class(facts, "Math"))

    @unittest.skipUnless(hasattr(insights, "keep_fact"), "needs #54's keep_letter engine")
    def test_engine_keep_fact_is_said_in_slot_wording(self):
        from test_insights import POG_ROWS, POG_TOTALS, class_input, snapshot, TODAY

        prior = snapshot("2026-10-21", {"Biology": (91.8, "A")})
        cls = class_input("Biology", POG_TOTALS, POG_ROWS, 85.2, "B", new_keys=["unit2test"])
        facts = insights.class_facts(cls, [prior], prior, TODAY)
        story = w.write_class(facts, "Biology")
        self.assertEqual(story["story"], "Slipped to a B since Oct 21, down 6.6. If the next test is worth about 50 "
                                         "points like the last ones, a 27 or better keeps the B.")
        keeps = [f for f in facts if f["kind"] == "keep_letter"]
        # keep_fact's first, then a typical keep for the other heavy bucket.
        self.assertEqual(len(keeps), 2)
        self.assertEqual(w.say_keep(keeps[0]), "A 54 or better on the {next:%s} keeps the B." % keeps[0]["id"])
        idx = w.fact_index(w.facts_for(facts, "Biology"))
        self.assertIsNone(w.check_sentence(w.say_keep(keeps[0]), [keeps[0]["id"]], idx, "Biology"))
        self.assertEqual(w.fill_slots(w.say_keep(keeps[0]), idx), "A 54 or better on the next test keeps the B.")


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
        idx = w.fact_index(facts)
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
    idx = w.fact_index(facts)
    cites = ["move:g"] + sorted(set(re.findall(r"\{(?:item|bucket):(cause:[^}]+)\}", text)))
    return w.check_sentence(text, cites, idx, "Geo", list(classes))


def cause_claims(text, facts, classes=("Geo",)):
    """gate_check with the fixed-form gate set aside (see claims): the claim checks alone."""
    reason = gate_check(text, facts, classes)
    return None if reason == w.FORM_REASON else reason


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
        idx = w.fact_index(geo2_facts())
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
        rise = w.fact_index(geo2_facts(delta=2.5, neg=-0.5, pos=3.0))
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
            self.assertIsNone(cause_claims(text, drop), text)
        rise = geo2_facts(delta=2.5, neg=-0.5, pos=3.0)
        for text in ("Mostly the {item:cause:g:b}.", "The {item:cause:g:b} helped.", "{bucket:cause:g:b} scores helped."):
            self.assertIsNone(cause_claims(text, rise), text)

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
            self.assertIsNone(cause_claims(text, facts), text)
        self.assertIsNotNone(gate_check("The {item:cause:g:a} and the {item:cause:g:b} hurt.", facts))
        for text in ("The {item:cause:g:a} and the {item:cause:g:c} helped.",
                     "The {item:cause:g:a} and the {item:cause:g:c} did most of the damage.",
                     "Mostly the {item:cause:g:a} and the {item:cause:g:c}."):
            self.assertIsNotNone(gate_check(text, facts), text)

    def test_most_needs_the_top_share_in_the_moves_direction(self):
        facts = geo2_facts()
        facts[1]["causes"].append({"id": "cause:g:c", "title": "Map Quiz", "bucket": "Quizzes", "bucket_weight_pct": 20.0,
                                   "earned": 2.0, "possible": 10.0, "points": -0.5, "share": -0.5})
        self.assertIsNone(cause_claims("The {item:cause:g:c} hurt.", facts))
        for text in ("Mostly the {item:cause:g:c}.", "The {item:cause:g:c} did most of the damage.",
                     "Mostly {bucket:cause:g:c} scores."):
            self.assertIsNotNone(gate_check(text, facts), text)

    def test_a_cause_sentence_drops_on_the_form_check(self):
        drop = geo2_facts()
        for text in ("The {item:cause:g:a} pulled it down.", "The {item:cause:g:a} sank it.", "It was hurt by the {item:cause:g:a}.",
                     "Down 2.5 since Oct 14, mostly the {item:cause:g:a}."):
            self.assertEqual(gate_check(text, drop), w.FORM_REASON, text)
        self.assertIsNotNone(gate_check("The {item:cause:g:a} hurt; the {item:cause:g:b} helped.", drop))
        idx = w.fact_index(drop)
        # A cited cause with no slot, a cause named but not cited, a move sentence that isn't its opening.
        self.assertEqual(w.check_sentence("One low score did most of the damage.", ["cause:g:a"], idx, "Geo"),
                         w.FORM_REASON)
        self.assertIsNotNone(w.check_sentence("The {item:cause:g:a} hurt.", ["cause:g:a", "cause:g:b"], idx, "Geo"))
        self.assertEqual(w.check_sentence("Down 2.5 since Oct 14.", ["move:g"], idx, "Geo"), w.FORM_REASON)

    def test_sentences_that_are_no_cause_sentence_skip_the_gate(self):
        idx = w.fact_index(geo2_facts())
        for text, cites in (("Still a D, 1 points from a C.", ["next:g"]), ("Scores usually post Thursdays.", ["pat:g"]),
                            ("A 74 or better on the {next:whatif:g} gets it to a C.", ["whatif:g"]),
                            ("{bucket:whatif:g} is 50% of the grade.", ["whatif:g"])):
            self.assertIsNone(claims(text, cites, idx, "Geo"), text)

    def test_the_v6_story_forms_pass_verbatim(self):
        # The sentence forms of the v6 bake-off stories, on synthetic facts.
        idx = w.fact_index(chem_facts())
        for text, cites in (("Still an A, down 1.6 since Oct 14.", [CHEM_MOVE]),
                            ("The {item:%s} did most of the damage." % CHEM_C1, [CHEM_C1]),
                            ("Mostly the {item:%s}." % CHEM_C1, [CHEM_C1]),
                            ("The {item:%s} hurt." % CHEM_C2, [CHEM_C2])):
            self.assertIsNone(claims(text, cites, idx, "Chem"), text)
        helped = chem_facts()
        helped[0]["causes"].append({"id": "cause:chem:2026-10-14:rocket", "title": "Balloon Rocket Lab",
                                    "category": "Labs", "bucket": "Labs", "bucket_weight_pct": 20.0, "change": "new",
                                    "earned": 10.0, "possible": 10.0, "points": 0.1, "share": 0.1})
        hidx = w.fact_index(helped)
        self.assertIsNone(claims("The {item:cause:chem:2026-10-14:rocket} helped.",
                                           ["cause:chem:2026-10-14:rocket"], hidx, "Chem"))
        geo = w.fact_index(geometry_facts())
        for text, cites in (("Still a C, 0.5 points from a B.", [GEO_NEXT]),
                            ("An 85 or better on the {next:%s} gets it to a B." % GEO_WHATIF, [GEO_WHATIF])):
            self.assertIsNone(claims(text, cites, geo, "Geometry"), text)


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
            idx = w.fact_index(facts)
            for text, cites in (("{bucket:whatif:g} scores helped.", ["whatif:g"]),
                                ("{bucket:whatif:g} scores helped.", ["whatif:g", "move:g"]),
                                ("{bucket:whatif:g} scores hurt.", ["whatif:g", "next:g"]),
                                ("{bucket:whatif:g} scores dragged it down.", ["whatif:g"]),
                                ("Mostly {bucket:whatif:g} scores.", ["whatif:g", "move:g"]),
                                ("{bucket:whatif:g} scores did most of the damage.", ["whatif:g"])):
                self.assertIsNotNone(w.check_sentence(text, cites, idx, "Geo"), text)
            # With no direction word a what-if bucket's weight is fine; other wording is off the forms.
            self.assertIsNone(claims("{bucket:whatif:g} is 50% of the grade.", ["whatif:g"], idx, "Geo"))
            for text in ("A 74 or better on the next {bucket:whatif:g} score gets it to a C.",
                         "The next {bucket:whatif:g} score decides it."):
                self.assertEqual(w.check_sentence(text, ["whatif:g"], idx, "Geo"), w.FORM_REASON, text)

    def test_a_bucket_reads_its_net_share_in_the_move(self):
        # Drop of 2.0: Tests nets +1.0 (a -1.0, c +2.0), Labs -3.0.
        idx = w.fact_index(bucket_net_facts())
        for text in ("{bucket:cause:g:a} scores hurt.", "{bucket:cause:g:c} scores hurt.",
                     "{bucket:cause:g:a} scores did most of the damage.", "Mostly {bucket:cause:g:a} scores.",
                     "{bucket:cause:g:b} scores helped.", "{bucket:cause:g:a} and {bucket:cause:g:c} scores helped."):
            self.assertIsNotNone(gate_check(text, bucket_net_facts()), text)
        for text in ("{bucket:cause:g:a} scores helped.", "{bucket:cause:g:c} scores helped.",
                     "{bucket:cause:g:b} scores hurt.", "{bucket:cause:g:b} scores did most of the damage.",
                     "Mostly {bucket:cause:g:b} scores."):
            self.assertIsNone(cause_claims(text, bucket_net_facts()), text)
        self.assertEqual(w._bucket_net(idx, idx["cause:g:a"]), 1.0)
        # Rise of 2.0: Tests nets -1.0 (a +1.0, c -2.0), Labs +3.0.
        rise = bucket_net_facts(delta=2.0, a=1.0, c=-2.0, b=3.0)
        for text in ("{bucket:cause:g:a} scores helped.", "Mostly {bucket:cause:g:a} scores.", "{bucket:cause:g:b} scores hurt."):
            self.assertIsNotNone(gate_check(text, rise), text)
        for text in ("{bucket:cause:g:a} scores hurt.", "{bucket:cause:g:b} scores helped.", "Mostly {bucket:cause:g:b} scores."):
            self.assertIsNone(cause_claims(text, rise), text)

    def test_mostly_a_bucket_ranks_buckets_by_net(self):
        # Drop: Tests -2.5 (a -1.5, c -1.0) beats Labs -2.0, though Labs holds the largest single cause.
        facts = bucket_net_facts(delta=-4.5, a=-1.5, c=-1.0, b=-2.0)
        self.assertIsNone(cause_claims("Mostly {bucket:cause:g:a} scores.", facts))
        self.assertIsNone(cause_claims("Mostly {bucket:cause:g:c} scores.", facts))
        self.assertIsNotNone(gate_check("Mostly {bucket:cause:g:b} scores.", facts))
        self.assertIsNone(cause_claims("Mostly the {item:cause:g:b}.", facts))
        rise = bucket_net_facts(delta=4.5, a=1.5, c=1.0, b=2.0)
        self.assertIsNone(cause_claims("Mostly {bucket:cause:g:a} scores.", rise))
        self.assertIsNotNone(gate_check("Mostly {bucket:cause:g:b} scores.", rise))

    def test_a_bucket_net_under_a_tenth_claims_no_direction(self):
        # The edge is 0.1 either way: 0.3 - 0.2 nets 0.1 (to the float), 0.29 - 0.2 nets 0.09.
        for c, a, ok in ((0.3, -0.2, True), (0.29, -0.2, False), (0.1, 0.0, True), (0.0, 0.0, False)):
            facts = bucket_net_facts(delta=-2.9, a=a, c=c, b=-3.0)
            got = cause_claims("{bucket:cause:g:c} scores helped.", facts)
            self.assertEqual(got is None, ok, (c, a, got))
        for c, a, ok in ((-0.3, 0.2, True), (-0.29, 0.2, False)):
            facts = bucket_net_facts(delta=2.9, a=a, c=c, b=3.0)
            got = cause_claims("{bucket:cause:g:c} scores hurt.", facts)
            self.assertEqual(got is None, ok, (c, a, got))

    def test_move_direction_words_need_a_cited_move_of_that_sign(self):
        for facts, wrong, right in ((geo2_facts(), "rose", "slipped"), (geo2_facts(delta=2.5, neg=-0.5, pos=3.0), "fell", "rose")):
            idx = w.fact_index(facts)
            for text, cites in (("Scores dropped.", ["next:g"]), ("It rose.", ["next:g"]),
                                ("It fell.", ["pat:g"]), ("Scores went up.", ["pat:g"]), ("It slipped.", ["next:g"])):
                self.assertEqual(w.check_sentence(text, cites, idx, "Geo"), self.MOVED, text)
            self.assertEqual(claims(f"It {wrong} since Oct 14.", ["move:g"], idx, "Geo"), self.MOVED)
            self.assertIsNone(claims(f"It {right} since Oct 14.", ["move:g"], idx, "Geo"))
            opening = w.say_opening(w._say_fields(facts[1]))
            self.assertIsNone(w.check_sentence(opening, ["move:g"], idx, "Geo"), opening)
            flipped = opening.replace("down", "up") if "down" in opening else opening.replace("up", "down")
            self.assertIsNotNone(w.check_sentence(flipped, ["move:g"], idx, "Geo"), flipped)

    def test_the_exact_contrast_forms_pass_both_ways(self):
        drop, rise = geo2_facts(), geo2_facts(delta=2.5, neg=-0.5, pos=3.0)
        self.assertIsNone(cause_claims("The {item:cause:g:b} helped.", drop))
        self.assertIsNone(cause_claims("The {item:cause:g:a} hurt.", rise))
        self.assertIsNone(cause_claims("{bucket:cause:g:a} scores hurt.", rise))
        self.assertIsNotNone(gate_check("The {item:cause:g:a} hurt a little.", rise))
        self.assertIsNotNone(gate_check("The {item:cause:g:a} dragged it down.", rise))

    def test_a_bucket_weight_on_a_cause_is_no_direction_claim(self):
        idx = w.fact_index(geo2_facts())
        self.assertIsNone(claims("{bucket:cause:g:a} is 50% of the grade.", ["cause:g:a"], idx, "Geo"))
        self.assertIsNone(claims("{bucket:cause:g:a} scores are 50% of the grade.", ["cause:g:a"], idx, "Geo"))
        self.assertIsNotNone(w.check_sentence("{bucket:cause:g:a} is 40% of the grade.", ["cause:g:a"], idx, "Geo"))
        self.assertIsNotNone(w.check_sentence("{bucket:cause:g:a} is 50% of the grade and dragged it down.",
                                              ["cause:g:a"], idx, "Geo"))
        self.assertIsNotNone(w.check_sentence("{bucket:cause:g:b} is 30% of the grade, and helped.", ["cause:g:b"], idx, "Geo"))

    def test_bucket_fills_keep_their_capitals_but_not_after_an_article(self):
        idx = w.fact_index(geo2_facts())
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
            self.assertIsNone(cause_claims(text, facts), text)
        idx = w.fact_index(facts)
        self.assertIsNone(claims("{bucket:cause:g:t1} is 30% of the grade.", ["cause:g:t1"], idx, "Geo"))

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
        self.assertIsNone(cause_claims("{bucket:cause:g:t1} scores hurt.", facts))
        self.assertIsNone(cause_claims("Mostly {bucket:cause:g:l0} scores.", facts))
        # No fold: other_causes 0 and other_share 0 or None.
        for share in (0, 0.0, None):
            facts = fold_facts(folded=False)
            facts[1] = {**facts[1], "other_causes": 0, "other_share": share}
            self.assertIsNone(cause_claims("{bucket:cause:g:t1} scores hurt.", facts), share)

    @unittest.skipUnless(hasattr(insights, "fold_causes"), "needs #54's fold_causes")
    def test_a_move_folded_by_fold_causes_claims_no_bucket_direction(self):
        facts = fold_facts(folded=False)
        folded = insights.fold_causes(facts[1])
        self.assertEqual((len(folded["causes"]), folded["other_causes"], folded["other_share"]), (8, 2, -0.5))
        facts[1] = folded
        self.assertIsNotNone(gate_check("{bucket:cause:g:t1} scores helped.", facts))
        self.assert_fails_closed(facts)


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
            idx = w.fact_index(facts)
            for text, cites in self.IDIOMS:
                self.assertIsNotNone(w.check_sentence(text, cites, idx, "Geo"), text)
                if "{" not in text:
                    self.assertEqual(claims(text, cites, idx, "Geo"), GateReviewTests.MOVED, text)
            self.assertEqual(claims("Still a D, down to the D range.", ["next:g"], idx, "Geo"),
                             GateReviewTests.MOVED)
            # The step's own form, with no up or down in it, passes.
            self.assertIsNone(claims("A 74 or better on the {next:whatif:g} gets it to a C.", ["whatif:g"],
                                               idx, "Geo"))
            self.assertIsNone(claims("Scores usually post Thursdays.", ["pat:g"], idx, "Geo"))

    def test_up_to_or_down_to_anything_on_the_wrong_way_move_drops(self):
        moved = GateReviewTests.MOVED
        fall, rise = (w.fact_index(f) for f in (geo2_facts(), geo2_facts(delta=2.5, neg=-0.5, pos=3.0)))
        self.assertEqual(len(self.LEAKS_ON_FALL + self.LEAKS_ON_RISE), 11)
        for idx, texts in ((fall, self.LEAKS_ON_FALL), (rise, self.LEAKS_ON_RISE)):
            for text in texts:
                self.assertIsNotNone(w.check_sentence(text, ["move:g"], idx, "Geo"), text)
                self.assertEqual(claims(text, ["move:g"], idx, "Geo"), moved, text)

    def test_up_to_or_down_to_a_grade_is_a_direction(self):
        # GE's repros on e677998, citing the move: a fall from a C to a D, and a rise from 66.5 to 69 (a D).
        moved = GateReviewTests.MOVED
        fall, rise = (w.fact_index(f) for f in (geo2_facts(), geo2_facts(delta=2.5, neg=-0.5, pos=3.0)))
        for text in ("It's up to a C now.", "Up to 71.5 since Oct 14."):
            self.assertIsNotNone(w.check_sentence(text, ["move:g"], fall, "Geo"), text)
            self.assertEqual(claims(text, ["move:g"], fall, "Geo"), moved, text)
        # The opener's own crossing form is read as a letter move first: no move crossed into a C.
        self.assertEqual(claims("Up to a C since Oct 14.", ["move:g"], fall, "Geo"),
                         "says the class moved to C but no move crossed into that letter")
        for text in ("Now down to a D.", "Down to a D since Oct 14.", "It came down to a D.",
                     "Down to 69 since Oct 14.", "Down to 69% now."):
            self.assertIsNotNone(w.check_sentence(text, ["move:g"], rise, "Geo"), text)
            self.assertEqual(claims(text, ["move:g"], rise, "Geo"), moved, text)
        # The matching sign passes the move check (with the gate set aside, as only the say_opening passes it).
        for text in ("Down to a D since Oct 14.", "It's down to a D now.", "Now down to a D.", "It came down to a D.",
                     "Down to 69 since Oct 14."):
            self.assertIsNone(claims(text, ["move:g"], fall, "Geo"), text)
            self.assertEqual(w.check_sentence(text, ["move:g"], fall, "Geo"), w.FORM_REASON, text)
        for text in ("Now up to a D.", "It came up to a D.", "It's up to a D now.", "Up to 69 since Oct 14."):
            self.assertIsNone(claims(text, ["move:g"], rise, "Geo"), text)
        # "Up to a D" is the opener's crossing form, and a rise that stayed a D crossed into nothing.
        self.assertEqual(claims("Up to a D since Oct 14.", ["move:g"], rise, "Geo"),
                         "says the class moved to D but no move crossed into that letter")
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
        drop, rise = (w.fact_index(f) for f in (geo2_facts(), geo2_facts(delta=2.5, neg=-0.5, pos=3.0)))
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
            idx = w.fact_index(facts)
            for text in self.DIRECTION:
                for cites in (["next:g"], ["next:g", "pat:g"]):
                    self.assertIsNotNone(w.check_sentence(text, cites, idx, "Geo", ["Geo"]), (label, text, cites))
            for text in ("Still a D, 1.0 points from a C, and slipping.", "No longer a C.", "From a C to a D."):
                self.assertEqual(w.check_sentence(text, ["next:g"], idx, "Geo", ["Geo"]), w.FORM_REASON, text)
            # Off the opening's words, even with no direction: the short form drops too (fail closed).
            for text in ("Geo: D, 1 pts from a C.", "Still a D.", "Still a D, 1 point from a C."):
                self.assertEqual(w.check_sentence(text, ["next:g"], idx, "Geo", ["Geo"]), w.FORM_REASON, text)

    def test_the_exact_opening_and_the_step_pass(self):
        for label, facts in self.facts().items():
            model = {f["id"]: w._say_fields(f) for f in facts}
            idx = w.fact_index(facts)
            opening = w.say_opening(model["next:g"])
            self.assertTrue(opening.startswith("Still a D, 1"), opening)
            for text in (opening, "{class:next:g} " + opening[0].lower() + opening[1:]):
                self.assertIsNone(w.check_sentence(text, ["next:g"], idx, "Geo", ["Geo"]), (label, text))
            step = model["whatif:g"]["say_step"]
            self.assertIsNone(w.check_sentence(step, ["whatif:g"], idx, "Geo", ["Geo"]), (label, step))
            # The what-if keeps its own form; a pattern or the move sentence is untouched.
            self.assertIsNone(claims("Scores usually post Thursdays.", ["pat:g"], idx, "Geo"))
            move_opening = w.say_opening(model["move:g"])
            self.assertIsNone(w.check_sentence(move_opening, ["move:g"], idx, "Geo", ["Geo"]), move_opening)


class ReviewNitTests(unittest.TestCase):
    """GE's nits on #52 8d2bd63: codes without letters or weekdays, natural next fills, no keep ids in {next:}."""

    def test_next_fills_read_naturally(self):
        for bucket, said in (("Homework", "the next homework assignment"), ("Labs & Projects", "the next lab or project"),
                             ("Labs/Activities", "the next lab or activity"),
                             ("Participation", "the next participation score"), ("Tests", "the next test")):
            idx = w.fact_index(geo_facts(bucket=bucket))
            self.assertEqual(w.fill_slots("A 74 or better on the {next:whatif:geo} gets it to a C.", idx),
                             f"A 74 or better on {said} gets it to a C.", bucket)
            self.assertIsNone(w.check_sentence("A 74 or better on the {next:whatif:geo} gets it to a C.",
                                               ["whatif:geo"], idx, "Geo"), bucket)


class OtherClassNameTests(unittest.TestCase):
    """GE's nits 1 and 2 on #52 8d2bd63: lowercase class names, and the extra fixed subject words."""

    CLASSES = ["Geo", "English 10", "Physics", "Art", "Band", "Health"]

    def check(self, text, cites=("cause:geo:t",), classes=None, cls="Geo"):
        """The names check (fixed forms aside: these sentences are about class names)."""
        return claims(text, list(cites), w.fact_index(geo_facts(cls=cls)), cls,
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
    """The CTO's ruling on #52 5bc7884: every sentence cites a fact, weekdays only from a computed pattern,
    and one parser leniency (extra trailing braces)."""

    def idx(self, delta=-2.5):
        facts = geo2_facts() if delta < 0 else geo2_facts(delta=2.5, neg=-0.5, pos=3.0)
        return w.fact_index(facts)

    def test_every_uncited_sentence_drops(self):
        idx = self.idx()
        for text in ("Heading the wrong way.", "Its posted scale changed.", "Keep it going.", "Nice work this week.",
                     "Mostly steady.", "Still a D.", "Scores usually post Thursdays.",
                     "A 74 or better on the next test gets it to a C."):
            self.assertEqual(w.check_sentence(text, [], idx, "Geo", ["Geo"]), w.UNCITED_REASON, text)
            self.assertEqual(w.check_sentence(text, [], idx), w.UNCITED_REASON, text)
        self.assertFalse(hasattr(w, "free_reason"))
        self.assertFalse(hasattr(w, "FREE_REASON"))

    def test_a_weekday_only_from_the_cited_computed_pattern(self):
        idx = self.idx()
        self.assertIsNone(claims("Scores usually post Thursdays.", ["pat:g"], idx, "Geo"))
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


def art_facts():
    """Art at a C, 1.0 from a B, with a what-if on Projects (GE's headline_xclass fixture)."""
    return [{"id": "next:a", "kind": "next_letter", "class": "Art", "pct": 79.0, "letter": "C", "next_letter": "B",
             "threshold": 80.0, "gap": 1.0},
            {"id": "whatif:a", "kind": "what_if", "class": "Art", "bucket": "Projects", "bucket_weight_pct": 40.0,
             "points": 20.0, "target": 80.0, "needed_pct": 84.1, "reachable": True}]


class HostileTitleFillTests(unittest.TestCase):
    """GE's info note on a458198: a title with a newline or one that looks like a slot."""

    def check(self, title):
        """check_sentence past its form check (no cause sentence passes that now): the claim checks, then the
        leak check on the filled text, which code's own slotted lines go through too."""
        facts = geo2_facts()
        facts[1]["causes"][0]["title"] = title
        idx = w.fact_index(facts)
        text = "Mostly the {item:cause:g:a}."
        reason = claims(text, ["move:g", "cause:g:a"], idx, "Geo", ["Geo"])
        reason = reason or w.leak_reason(w.fill_slots(text, idx), idx, filled=True)
        return reason, (w.fill_slots(text, w.fact_index(facts)) if reason is None else None)

    def test_fills_are_one_line_without_braces(self):
        self.assertEqual(self.check("Quiz\n  3\tPart 2"), (None, "Mostly the Quiz 3 Part 2 (20/40)."))
        self.assertEqual(self.check("Lab {x}"), (None, "Mostly the Lab x (20/40)."))
        self.assertEqual(self.check("Test}"), (None, "Mostly the Test (20/40)."))
        self.assertEqual(self.check("Unit 3: Part 2"), (None, "Mostly the Unit 3: Part 2 (20/40)."))

    def test_a_title_that_looks_like_a_slot_or_an_id_drops(self):
        # A brace is stripped; a real fact id, or a slot word followed by an id-like token, in the fill drops.
        for title in ("{item:cause:g:b}", "{item:cause:g:zz}", "item:cause:g:b", "Item : Cause:G:H", "{next:whatif:g}",
                      "next：whatif：g", "{bucket:cause:g:b}", "cause:g:b", "move:g", "pattern:posting_day:geo"):
            reason, out = self.check(title)
            self.assertIsNotNone(reason, (title, out))

    def test_real_titles_with_colons_fill_as_written(self):
        # The id and slot-debris filters read the model's raw text; a fill is only checked for slot-shaped text.
        for title in ("Next: Chapter 5", "Class: Notes Check", "Ratio a:b Practice", "Quiz: next: steps", "Test: Cells",
                      "Lab 3: Density", "Bell work 10:15", "DBQ: WWI causes", "[class: Geo]", "Bucket: List Essay"):
            self.assertEqual(self.check(title), (None, "Mostly the %s (20/40)." % title), title)
            facts = geo2_facts()
            facts[2] = {**facts[2], "next_title": title}
            idx = w.fact_index(facts)
            step = "A 74 or better on the {next:whatif:g} gets it to a C."
            self.assertIsNone(w.check_sentence(step, ["whatif:g"], idx, "Geo", ["Geo"]), title)
            self.assertEqual(w.fill_slots(step, w.fact_index(facts)), "A 74 or better on the %s gets it to a C." % title)
        # The same words written by the model, outside a slot, still drop on the raw text.
        idx = w.fact_index(geo2_facts())
        for text in ("Mostly the {item:cause:g:a}, next:whatif:g.", "Mostly the {item:cause:g:a} item:cause:g:b.",
                     "Mostly the {item:cause:g:a}, cause:g:b.", "Mostly the {item:cause:g:a} a:b."):
            self.assertIsNotNone(w.check_sentence(text, ["move:g", "cause:g:a"], idx, "Geo", ["Geo"]), text)


class CannotDropWriterTests(unittest.TestCase):
    """GE's #55 finding, writer side: the can't-drop form only for a named next item at its posted points."""

    LINE = "The {next:%s} can't drop it below an A." % KEEP_LAB

    def test_an_unnamed_cannot_drop_fact_is_never_said(self):
        facts = chem_keep_facts(cannot_lose=True, named=False)
        keep = [f for f in facts if w.is_keep(f)][0]
        self.assertIsNone(w.say_keep(keep))
        self.assertIsNone(w.pick_keep(facts))
        idx = w.fact_index(facts)
        self.assertEqual(w.check_sentence(self.LINE, [KEEP_LAB], idx, "Chem"), w.FORM_REASON)
        self.assertNotIn("can't drop it", w.write_class(facts, "Chem")["story"])

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


def tag_class(name, pct, needed=None, keep=None, due="2026-10-27", points=50.0, named=True, cannot_lose=False,
              reachable=None, keep_named=None, delta="auto"):
    """One synthetic class as the engine emits it: its next_letter (insights.next_letter), a what_if on Tests at
    `points` needing `needed` and a keep_letter needing `keep`, plus the upcoming item that names both.

    delta is the grade_move since Oct 1: by default a 2.0 drop when there's a keep fact (a keep only counts
    after a drop, as in pick_keep) and no move otherwise; None for no move, a number for that move."""
    slug = insights.slug(name)
    facts = []
    if delta == "auto":
        delta = -2.0 if keep is not None or cannot_lose else None
    if delta is not None:
        facts.append({"id": f"move:{slug}:2026-10-01", "kind": "grade_move", "class": name, "since": "2026-10-01",
                      "from_pct": round(pct - delta, 1), "to_pct": pct, "delta": delta, "causes": [],
                      "explained": None, "coverage": None, "coverage_trimmed": False})
    step = insights.next_letter(name, pct)
    if step:
        facts.append(step)
    if needed is not None and step:
        facts.append({"id": f"whatif:{slug}:tests:{points:g}", "kind": "what_if", "class": name, "bucket": "Tests",
                      "bucket_weight_pct": 50.0, "points": points, "target": step["threshold"], "needed_pct": needed,
                      "reachable": needed <= 100 if reachable is None else reachable})
    if keep is not None or cannot_lose:
        facts.append({"id": f"keep:{slug}:tests:{points:g}", "kind": "keep_letter", "class": name,
                      "letter": insights.letter_for(pct), "bucket": "Tests", "points": points,
                      "needed_pct": None if cannot_lose else keep, "cannot_lose": cannot_lose})
    upcoming = []
    if named:
        upcoming.append({"class": name, "bucket": "Tests", "title": f"{name} Unit Test", "due": due, "points": points})
    elif keep_named:
        upcoming.append({"class": name, "bucket": "Tests", "title": f"{name} Unit Test", "due": due, "points": points})
    return facts, upcoming


def tag_facts(*classes):
    facts, upcoming = [], []
    for f, u in classes:
        facts += f
        upcoming += u
    return w.attach_next_items(facts, upcoming)


class StoryTagTests(unittest.TestCase):
    """Code tags each kid's classes: Easiest win (lowest needed on the soonest next item, named or not, that
    reaches the next letter) and Biggest risk (highest keep_letter needed on a named item at its posted points)."""

    def what_if(self, name, bucket, weight, needed, target=80.0):
        return {"id": f"whatif:{insights.slug(name)}:{insights.slug(bucket)}:20", "kind": "what_if", "class": name,
                "bucket": bucket, "bucket_weight_pct": weight, "points": 20.0, "target": target, "needed_pct": needed,
                "reachable": needed <= 100}

    def test_easiest_win_counts_unnamed_next_items(self):
        # The v10 shape with no upcoming item named: Algebra needs an 85, Int Eng&Marktg a 97.
        alg = tag_class("Alg ll Bus Mang", 79.5, needed=84.3, named=False)
        mkt = tag_class("Int Eng&Marktg", 76.3, needed=96.2, named=False)
        for order in ((alg, mkt), (mkt, alg)):
            facts = tag_facts(*order)
            self.assertFalse([f for f in facts if f.get("next_title")])
            self.assertEqual(w.easiest_win(facts), "Alg ll Bus Mang")
            self.assertEqual(w.story_tags(facts), {"Alg ll Bus Mang": ["easiest_win"]})

    def test_a_named_item_comes_before_an_unnamed_one(self):
        # Art's named Tests item (due Oct 23) is its next item, so its unnamed 81 never counts.
        art = [insights.next_letter("Art", 79.5), self.what_if("Art", "Tests", 10.0, 95.0),
               self.what_if("Art", "Homework", 90.0, 81.0)]
        upcoming = [{"class": "Art", "bucket": "Tests", "title": "Unit 3 Test", "due": "2026-10-23", "points": 20.0}]
        bio = [insights.next_letter("Biology", 79.5), self.what_if("Biology", "Tests", 50.0, 90.0)]
        # The CTO's rule: a named item beats any assumed one across the kid, so Art's named 95 beats Biology's
        # assumed 90.
        self.assertEqual(w.easiest_win(w.attach_next_items(art + bio, upcoming)), "Art")
        self.assertEqual(w.class_step(w.facts_for(w.attach_next_items(art, upcoming), "Art"))["id"], "whatif:art:tests:20")
        # Unnamed, both are assumed: Art's 81 beats Biology's 90.
        self.assertEqual(w.easiest_win(w.attach_next_items(art + bio, [])), "Art")
        # A named next item that can't reach the letter isn't said; Art's story says its assumed Homework step
        # instead (another category, nothing posted there), and that 81 is the easiest.
        stuck = [art[0], self.what_if("Art", "Tests", 10.0, 101.0), art[2]]
        self.assertEqual(w.easiest_win(w.attach_next_items(stuck + bio, upcoming)), "Art")
        self.assertEqual(w.class_step(w.attach_next_items(stuck, upcoming))["bucket"], "Homework")
        # Biology's 90 only wins when Art's story says no step at all.
        self.assertEqual(w.easiest_win(w.attach_next_items(stuck[:2] + bio, upcoming)), "Biology")

    def test_unnamed_items_go_heaviest_reachable_bucket_first(self):
        # With nothing named, the tag is the assumed step the story says: the heaviest bucket that can reach.
        letter = insights.next_letter("Art", 79.5)
        heavy_stuck = [letter, self.what_if("Art", "Tests", 60.0, 104.0), self.what_if("Art", "Homework", 20.0, 81.0)]
        self.assertEqual(w.class_step(heavy_stuck)["bucket"], "Homework")
        self.assertEqual(w.easiest_win(heavy_stuck), "Art")
        heavy_ok = [letter, self.what_if("Art", "Tests", 60.0, 90.0), self.what_if("Art", "Homework", 20.0, 81.0)]
        # Both are said (two assumed lines, heaviest first); the tag goes on the easier one.
        self.assertEqual([f["bucket"] for f in w.class_steps(heavy_ok)], ["Tests", "Homework"])
        self.assertEqual(w.class_step(heavy_ok)["bucket"], "Homework")
        # Only what the story says counts.
        self.assertEqual(w.class_step(heavy_ok, said={"whatif:art:tests:20"})["bucket"], "Tests")
        # Neither reaches: no step, no tag.
        stuck = [letter, self.what_if("Art", "Tests", 40.0, 101.0), self.what_if("Art", "Homework", 40.0, 104.0)]
        self.assertIsNone(w.easiest_win(stuck))
        self.assertIsNone(w.easiest_win(stuck[::-1]))

    def test_easiest_win_is_the_lowest_needed_score_not_the_model_pick(self):
        # The v10 shape: Algebra needs an 85, Marketing a 97; the code picks Algebra whatever order they come in.
        alg, mkt = tag_class("Algebra", 79.5, needed=84.3), tag_class("Marketing", 76.3, needed=96.2)
        for order in ((alg, mkt), (mkt, alg)):
            self.assertEqual(w.easiest_win(tag_facts(*order)), "Algebra")

    def test_easiest_win_ties_go_to_the_smaller_gap_then_the_name(self):
        # 84.1 and 84.9 are both said "85 or better": a tie on the note's number.
        self.assertEqual(w.easiest_win(tag_facts(tag_class("Art", 77.0, needed=84.1),
                                                 tag_class("Biology", 78.5, needed=84.9))), "Biology")
        self.assertEqual(w.easiest_win(tag_facts(tag_class("Chem", 78.5, needed=84.9),
                                                 tag_class("Biology", 78.5, needed=84.1))), "Biology")
        self.assertEqual(w.easiest_win(tag_facts(tag_class("Chem", 78.5, needed=84.0),
                                                 tag_class("Biology", 78.5, needed=84.0))), "Biology")

    def test_easiest_win_skips_a_class_that_cant_move_up_on_its_next_item(self):
        cases = {
            "unreachable": tag_facts(tag_class("Art", 79.5, needed=104.0)),
            "an A has no next letter": tag_facts(tag_class("Art", 95.0, needed=50.0)),
            "no what_if": tag_facts(tag_class("Art", 79.5)),
        }
        for why, facts in cases.items():
            self.assertIsNone(w.easiest_win(facts), why)
        # Only the soonest named item counts: a later reachable one doesn't rescue an unreachable next one.
        soon = tag_class("Art", 79.5, needed=101.0, due="2026-10-23", points=50.0)
        later = tag_class("Art", 79.5, needed=81.0, due="2026-10-30", points=20.0)
        facts = tag_facts((soon[0] + later[0][1:], soon[1] + later[1]), tag_class("Biology", 71.0, needed=99.0))
        self.assertEqual(w.easiest_win(facts), "Biology")

    def test_biggest_risk_is_the_highest_keep_score_on_a_named_item(self):
        facts = tag_facts(tag_class("Art", 92.0, keep=62.0), tag_class("Biology", 81.0, keep=78.6),
                          tag_class("Chem", 85.0, keep=99.0, named=False))
        # Chem's unnamed keep is an assumed next item its story says ("If the next test is worth about 50
        # points like the last ones, a 50 or better keeps the B."), so it counts, but the CTO's rule puts named
        # items first across the kid: Biology's named 78.6 beats Chem's assumed 99.
        self.assertEqual(w.biggest_risk(facts), "Biology")
        self.assertEqual(w.biggest_risk([f for f in facts if f["class"] == "Chem"]), "Chem")
        # With a Tests item posted at another size, the posted one wins: Chem's assumed keep isn't said.
        other_size = [{"class": "Chem", "bucket": "Tests", "title": "Chem Quiz", "due": "2026-10-27", "points": 20.0}]
        posted = w.attach_next_items([f for f in facts if f["class"] == "Chem"], other_size)
        self.assertIsNone(w.biggest_risk(posted))
        safe = tag_facts(tag_class("Art", 99.0, cannot_lose=True), tag_class("Chem", 85.0, cannot_lose=True, named=False))
        self.assertIsNone(w.biggest_risk(safe))
        self.assertIsNone(w.biggest_risk(tag_facts(tag_class("Art", 79.5, needed=84.3))))

    def test_biggest_risk_needs_the_item_s_posted_points(self):
        facts = tag_facts(tag_class("Art", 81.0, keep=90.0))
        keep = next(f for f in facts if f["kind"] == "keep_letter")
        self.assertEqual(w.biggest_risk(facts), "Art")
        for points in (None, 0, 0.0, -5.0, True, "50"):
            self.assertIsNone(w.biggest_risk([{**keep, "points": points}]), points)
        # Unnamed, it's an assumed item at its typical size; unless its bucket has a posted item.
        self.assertEqual(w.biggest_risk([{**keep, "next_title": ""}]), "Art")
        self.assertIsNone(w.biggest_risk([{**keep, "next_title": "", "bucket_posted": True}]))

    def test_biggest_risk_ties_go_to_the_smaller_cushion_then_the_name(self):
        # 78.2 and 78.9 are both said "79 or better". Chem sits 0.5 above its B floor, Art 4.0.
        self.assertEqual(w.biggest_risk(tag_facts(tag_class("Art", 84.0, keep=78.2),
                                                  tag_class("Chem", 80.5, keep=78.9))), "Chem")
        self.assertEqual(w.biggest_risk(tag_facts(tag_class("Chem", 80.5, keep=78.9),
                                                  tag_class("Biology", 80.5, keep=78.9))), "Biology")
        # An A that dropped takes its percent from the move: 5.0 above the A floor beats Chem's 9.5 above the B's.
        self.assertEqual(w.biggest_risk(tag_facts(tag_class("Art", 95.0, keep=78.9),
                                                  tag_class("Chem", 89.5, keep=78.9))), "Art")

    def test_one_class_can_carry_both_tags_in_order(self):
        facts = tag_facts(tag_class("Art", 79.5, needed=81.0, keep=70.0), tag_class("Biology", 72.0, needed=99.0, keep=40.0))
        self.assertEqual(w.story_tags(facts), {"Art": ["easiest_win", "biggest_risk"]})
        self.assertEqual(w.story_tags(tag_facts(tag_class("Art", 95.0))), {})

    def test_write_up_puts_tags_on_stories_only(self):
        risk = tag_class("Biology", 81.0, keep=78.6)
        facts = {"as_of": "2026-10-22", "facts": math_facts() + risk[0]}
        upcoming = [{"class": "Math", "bucket": "Tests", "title": "Ch. 5 Test", "due": "2026-10-30", "points": 50.0}]
        up = w.write_up(facts, upcoming=upcoming + risk[1])
        # Every class gets a story: Biology's is code's (its letter and its named keep), so it takes Biggest
        # risk; Math has no keep, and takes Easiest win.
        self.assertEqual([(s["class"], s.get("tags")) for s in up["stories"]],
                         [("Biology", ["biggest_risk"]), ("Math", ["easiest_win"])])
        self.assertIn("keeps the B", up["stories"][0]["story"])
        self.assertEqual([s["tags"] for s in w.public_write_up(up)["stories"]], [["biggest_risk"], ["easiest_win"]])


class CodeLineTests(unittest.TestCase):
    """David's rule at cafb128: code writes the keep sentence and the move-up step for the next named item at
    its posted points, on a rising, flat or falling class; the checker takes both as what-if steps."""

    def chem(self, delta, **kw):
        facts, upcoming = tag_class("Chem", 76.0, **{"needed": 89.2, "keep": 61.0, "delta": delta, **kw})
        return w.attach_next_items(facts, upcoming)

    def test_both_sentences_on_a_rising_a_flat_and_a_falling_class(self):
        for delta in (1.5, None, -1.5):
            facts = self.chem(delta)
            idx = w.fact_index(facts)
            lines = w.code_lines(facts, "Chem")
            self.assertEqual([(l["code"], l["text"]) for l in lines], [
                ("move_up", "A 90 or better on the {next:whatif:chem:tests:50} gets it to a B."),
                ("keep", "A 61 or better on the {next:keep:chem:tests:50} keeps the C.")], delta)
            for line in lines:
                # The checker passes each one with the class's move in the index, whichever way it went.
                self.assertIsNone(w.check_sentence(line["text"], line["cites"], idx, "Chem"), (delta, line))
            story = w.write_class(facts, "Chem")
            self.assertEqual(story["story"].split(". ", 1)[1], "A 90 or better on the Chem Unit Test gets it to a B. "
                                                                "A 61 or better on the Chem Unit Test keeps the C.")
            self.assertEqual(story["keep_sentence"], "A 61 or better on the Chem Unit Test keeps the C.")

    def test_a_falling_class_with_up_words_in_its_step_is_fine(self):
        # "gets it to a B" on a falling class: no up/down claim, so the same-sign rule doesn't read it.
        facts = self.chem(-3.0)
        line = w.code_lines(facts, "Chem")[0]
        self.assertIsNone(w.check_sentence(line["text"], line["cites"], w.fact_index(facts), "Chem"))
        # A model sentence saying the grade went up on that falling class still drops.
        self.assertEqual(w.check_sentence("It went up, and a 90 or better on the {next:whatif:chem:tests:50} gets it to a B.",
                                          ["whatif:chem:tests:50"], w.fact_index(facts), "Chem"),
                         "says the grade moved a way no cited move did")

    def test_an_unnamed_item_gets_the_assumed_line_and_one_off_its_posted_points_none(self):
        self.assertEqual([(l["code"], l["text"]) for l in w.code_lines(self.chem(1.5, named=False), "Chem")], [
            ("assumed", "If the {next:keep:chem:tests:50} is worth about 50 points like the last ones, a 31 or better "
                        "keeps the C and a 45 gets it to a B.")])
        # The item is named at 20 points; the facts are about 50: not its posted points, so no name, and the
        # posted item wins over an assumed one, so no line.
        facts, upcoming = tag_class("Chem", 76.0, needed=89.2, keep=61.0, delta=1.5)
        self.assertEqual(w.code_lines(w.attach_next_items(facts, [{**upcoming[0], "points": 20.0}]), "Chem"), [])

    def test_no_move_up_over_100_and_the_keep_still_goes(self):
        lines = w.code_lines(self.chem(1.5, needed=100.4, reachable=True), "Chem")
        self.assertEqual([l["code"] for l in lines], ["keep"])
        lines = w.code_lines(self.chem(1.5, needed=100.0), "Chem")
        self.assertEqual([l["code"] for l in lines], ["move_up", "keep"])
        self.assertIn("A 100 or better", lines[0]["text"])

    def test_a_cant_drop_class_says_so_only_when_named(self):
        facts, upcoming = tag_class("Chem", 76.0, cannot_lose=True, delta=2.0)
        lines = w.code_lines(w.attach_next_items(facts, upcoming), "Chem")
        self.assertEqual([l["text"] for l in lines], ["The {next:keep:chem:tests:50} can't drop it below a C."])
        self.assertEqual(w.code_lines(w.attach_next_items(facts, []), "Chem"), [])

    def test_a_falling_cant_drop_class_ends_on_its_step_then_the_cant_drop_line(self):
        facts, upcoming = tag_class("Chem", 76.0, needed=89.2, cannot_lose=True, delta=-2.0)
        facts = w.attach_next_items(facts, upcoming)
        story = w.write_class(facts, "Chem")
        self.assertEqual(story["story"], "Still a C, down 2 since Oct 1. A 90 or better on the Chem Unit Test gets it to a B. "
                                         "The Chem Unit Test can't drop it below a C.")
        idx = w.fact_index(facts)
        opening, step, safe = [{"text": t, "cites": c} for t, c in (
            ("Still a C, down 2 since Oct 1.", ["move:chem:2026-10-01"]),
            ("x.", ["whatif:chem:tests:50"]), ("y.", ["keep:chem:tests:50"]))]
        self.assertIsNone(w.eval_story([dict(opening), dict(step), dict(safe)], idx))

    def test_writer_and_runner_sort_stories_the_same_way(self):
        rng = random.Random(7)
        for _ in range(200):
            stories = [{"class": rng.choice(["Art", "art", "Bio", "Ärt", "B"]), "story": rng.choice(["a.", "b.", "A."]),
                        "cites": rng.sample(["x", "y", "z"], rng.randint(1, 3)),
                        **({"tags": rng.choice([["easiest_win"], ["biggest_risk"]])} if rng.random() < 0.5 else {})}
                       for _ in range(rng.randint(1, 6))]
            self.assertEqual([w.story_order(s) for s in stories], [runner._story_order(s) for s in stories])


class TagReviewTests(unittest.TestCase):
    """GitHub Engineer's #57 review at 83d2569, with David's any-direction Biggest risk at cafb128: tags over
    published stories, risk only on a story carrying its keep sentence, the same bytes in any fact order."""

    def written(self, facts, upcoming, stories, no_keep=()):
        """write_up with each class's opening scripted: stories[name] is its text, or None for no story.
        code_lines are added as write_class adds them; a class in no_keep loses its keep sentence."""
        def write_class(all_facts, name):
            text = stories.get(name)
            if not text:
                return None
            class_facts = w.facts_for(all_facts, name)
            idx = w.fact_index(class_facts)
            lines = [l for l in w.code_lines(class_facts, name) if not (l["code"] == "keep" and name in no_keep)]
            said = {l["code"]: w.fix_articles(w.fill_slots(l["text"], idx)) for l in lines}
            ids = sorted(f["id"] for f in class_facts)[:2] + [c for l in lines for c in l["cites"]]
            return {"class": name, "story": " ".join([text, *said.values()]), "cites": list(dict.fromkeys(ids)),
                    "keep_sentence": said.get("keep")}
        with mock.patch.object(w, "write_class", side_effect=write_class):
            return w.write_up({"as_of": "2026-10-22", "facts": facts}, upcoming=upcoming)

    def published(self, up):
        return runner.insights_body(runner.insights_doc([{"sn": "9001", **w.public_write_up(up)}]))

    def test_biggest_risk_can_land_on_a_rising_story_and_it_carries_the_keep_sentence(self):
        # GE's old rise repro: Chem up 1.5. Its keep sentence is now said and it can be the risk.
        for delta in (1.5, None, -1.5):
            chem, up = tag_class("Chem", 76.0, needed=89.2, keep=61.0, delta=delta)
            written = w.write_up({"as_of": "2026-10-22", "facts": chem}, upcoming=up)
            (story,) = written["stories"]
            self.assertEqual(story["tags"], ["easiest_win", "biggest_risk"], delta)
            self.assertIn("A 61 or better on the Chem Unit Test keeps the C.", story["story"])
            self.assertEqual(story["keep_sentence"], "A 61 or better on the Chem Unit Test keeps the C.")
        rise = w.attach_next_items(*tag_class("Chem", 76.0, needed=89.2, keep=61.0, delta=1.5))
        self.assertEqual(w.biggest_risk(rise), "Chem")

    def test_risk_needs_the_keep_sentence_on_the_story_and_a_score(self):
        facts, upcoming = [], []
        for f, u in (tag_class("Algebra", 79.5, keep=88.0, delta=0.8), tag_class("Chem", 85.0, keep=62.0),
                     tag_class("Art", 91.0, cannot_lose=True, delta=1.0)):
            facts += f
            upcoming += u
        every = {"Algebra": "Alg.", "Chem": "Chem.", "Art": "Art."}
        tags = lambda up: {s["class"]: s["tags"] for s in up["stories"] if s.get("tags")}
        self.assertEqual(tags(self.written(facts, upcoming, every)), {"Algebra": ["biggest_risk"]})
        # If Algebra's story lost its keep sentence, it can't carry the tag: Chem, the next-worst, gets it.
        self.assertEqual(tags(self.written(facts, upcoming, every, no_keep={"Algebra"})), {"Chem": ["biggest_risk"]})
        # A class that keeps its letter even with a zero is never the risk.
        self.assertEqual(tags(self.written(facts, upcoming, {"Art": "Art."})), {})

    def test_the_same_facts_give_the_same_tags(self):
        facts, upcoming = [], []
        for f, u in (tag_class("Algebra", 79.5, needed=84.3, keep=70.0, delta=1.0), tag_class("Marketing", 76.3, needed=84.9),
                     tag_class("Chem", 85.0, keep=70.4, delta=-2.0), tag_class("Biology", 80.5, keep=69.1, delta=None)):
            facts += f
            upcoming += u
        want = w.story_tags(w.attach_next_items(facts, upcoming))
        self.assertEqual(want, {"Algebra": ["easiest_win"], "Chem": ["biggest_risk"]})
        rng = random.Random(53)
        for _ in range(25):
            f, u = facts[:], upcoming[:]
            rng.shuffle(f)
            rng.shuffle(u)
            self.assertEqual(w.story_tags(w.attach_next_items(f, u)), want)

    def test_tags_go_to_the_next_best_class_with_a_published_story(self):
        facts, upcoming = [], []
        for f, u in (tag_class("Algebra", 79.5, needed=84.3, keep=70.0), tag_class("Marketing", 76.3, needed=96.2),
                     tag_class("Chem", 85.0, keep=62.0), tag_class("Spanish", 88.0)):
            facts += f
            upcoming += u
        every = {"Algebra": "Alg.", "Marketing": "Mkt.", "Chem": "Chem.", "Spanish": "Sp."}
        tags = lambda up: {s["class"]: s["tags"] for s in up["stories"] if s.get("tags")}
        self.assertEqual(tags(self.written(facts, upcoming, every)), {"Algebra": ["easiest_win", "biggest_risk"]})
        # Algebra's story doesn't survive: Marketing is the next-best win and Chem the next-worst risk.
        self.assertEqual(tags(self.written(facts, upcoming, {**every, "Algebra": None})),
                         {"Marketing": ["easiest_win"], "Chem": ["biggest_risk"]})
        # A story over the Worker's limits is dropped the same way, before the tags are worked out.
        self.assertEqual(tags(self.written(facts, upcoming, {**every, "Algebra": "x" * 601})),
                         {"Marketing": ["easiest_win"], "Chem": ["biggest_risk"]})
        # No eligible class has a story: no tag at all.
        self.assertEqual(tags(self.written(facts, upcoming, {"Spanish": "Sp."})), {})

    def test_shuffled_facts_publish_the_same_bytes(self):
        facts, upcoming = [], []
        for f, u in (tag_class("Algebra", 79.5, needed=84.3), tag_class("Marketing", 76.3, needed=84.9),
                     tag_class("Chem", 85.0, keep=62.0), tag_class("Biology", 80.5, keep=62.4),
                     tag_class("Spanish", 88.0), tag_class("Art", 95.0, keep=70.0, delta=1.0)):
            facts += f
            upcoming += u
        stories = {name: f"{name} note." for name in ("Algebra", "Marketing", "Chem", "Biology", "Spanish", "Art")}
        want = self.published(self.written(facts, upcoming, stories))
        self.assertEqual([s["class"] for s in json.loads(want)["students"]["9001"]["stories"]],
                         ["Algebra", "Art", "Biology", "Chem", "Marketing", "Spanish"])
        rng = random.Random(57)
        for _ in range(25):
            f, u = facts[:], upcoming[:]
            rng.shuffle(f)
            rng.shuffle(u)
            self.assertEqual(self.published(self.written(f, u, stories)), want)


if __name__ == "__main__":
    unittest.main()


# ── Grade insights follow-up: every class gets a story, assumed next items, biggest drag ─────────────────
BIO_KEEP, BIO_STEP = "keep:bio:tests:50", "whatif:bio:tests:50"
BIO_DRAG = "drag:bio:unit2test"
DAVIDS_EXAMPLE = ("If the next test is worth about 50 points like the last ones, a 38 or better keeps the B and a 46 "
                  "gets it to an A.")


def bio_facts(drag_title="Unit 2 Test", order=1):
    """Bio at a B, nothing posted: what_if and keep facts at each bucket's typical size, heaviest Tests (60%),
    then Homework (30%) and Labs (10%), plus its biggest drag. Synthetic."""
    def pair(bucket, weight, points, step, keep):
        slug = insights.slug(bucket)
        return [{"id": f"whatif:bio:{slug}:{points:g}", "kind": "what_if", "class": "Bio", "bucket": bucket,
                 "bucket_weight_pct": weight, "points": points, "target": 90.0, "needed_pct": step,
                 "reachable": step <= 100},
                {"id": f"keep:bio:{slug}:{points:g}", "kind": "keep_letter", "class": "Bio", "letter": "B",
                 "bucket": bucket, "points": points, "needed_pct": keep, "cannot_lose": False,
                 "bucket_weight_pct": weight}]
    buckets = [pair("Tests", 60.0, 50.0, 92.0, 76.0), pair("Homework", 30.0, 10.0, 100.0, 60.0),
               pair("Labs", 10.0, 20.0, 99.0, 70.0)][::order]
    return [{"id": "next:bio", "kind": "next_letter", "class": "Bio", "pct": 84.0, "letter": "B", "next_letter": "A",
             "threshold": 90.0, "gap": 6.0}, *[f for p in buckets for f in p],
            {"id": BIO_DRAG, "kind": "drag", "class": "Bio", "title": drag_title, "category": "Tests", "bucket": "Tests",
             "earned": 31.0, "possible": 50.0, "points": 3.2}]


class AssumedNextItemTests(unittest.TestCase):
    """David at 9:36 AM: with nothing posted, say the keep and move-up math for the next item at the size of
    the category's past items, and say that's the assumption."""

    def test_david_s_example_reads_word_for_word(self):
        facts = w.attach_next_items(bio_facts(), [])
        idx = w.fact_index(facts)
        lines = [l for l in w.code_lines(facts, "Bio") if l["code"] == "assumed"]
        self.assertEqual(lines[0]["text"], "If the {next:%s} is worth about 50 points like the last ones, a 38 or better "
                                           "keeps the B and a 46 gets it to an A." % BIO_KEEP)
        self.assertEqual(lines[0]["cites"], [BIO_KEEP, BIO_STEP])
        self.assertEqual(w.fix_articles(w.fill_slots(lines[0]["text"], idx)), DAVIDS_EXAMPLE)
        self.assertIsNone(w.check_sentence(lines[0]["text"], lines[0]["cites"], idx, "Bio"))

    def test_points_are_rounded_up_never_down(self):
        self.assertEqual(w.needed_points({"needed_pct": 76.0, "points": 50.0}), 38)
        self.assertEqual(w.needed_points({"needed_pct": 76.1, "points": 50.0}), 39)
        self.assertEqual(w.needed_points({"needed_pct": 100.0, "points": 10.0}), 10)
        self.assertIsNone(w.needed_points({"needed_pct": 100.4, "points": 10.0}))
        self.assertIsNone(w.needed_points({"needed_pct": None, "points": 10.0}))
        self.assertIsNone(w.needed_points({"needed_pct": 50.0, "points": 0}))

    def test_one_or_two_categories_largest_weight_first_in_any_fact_order(self):
        for order in (1, -1):
            facts = w.attach_next_items(bio_facts(order=order), [])
            said = [(l["code"], l["cites"]) for l in w.code_lines(facts, "Bio")]
            self.assertEqual(said, [("drag", [BIO_DRAG]), ("assumed", [BIO_KEEP, BIO_STEP]),
                                    ("assumed", ["keep:bio:homework:10", "whatif:bio:homework:10"])], order)

    def test_a_posted_item_always_wins_over_an_assumed_one(self):
        # A 20-point Tests item is posted: nothing is said about a 50-point next test; Homework and Labs fill in.
        quiz = [{"class": "Bio", "bucket": "Tests", "title": "Ch. 6 Quiz", "due": "2026-10-28", "points": 20.0}]
        facts = w.attach_next_items(bio_facts(), quiz)
        self.assertTrue(all(f.get("bucket_posted") for f in facts if f.get("bucket") == "Tests" and f["kind"] != "drag"))
        lines = w.code_lines(facts, "Bio")
        self.assertNotIn(BIO_KEEP, [c for l in lines for c in l["cites"]])
        self.assertEqual([l["cites"][0] for l in lines if l["code"] == "assumed"], ["keep:bio:homework:10", "keep:bio:labs:20"])
        # The model's old "on the next test" step is no longer true there either.
        step = "A 92 or better on the {next:%s} gets it to an A." % BIO_STEP
        self.assertEqual(w.check_sentence(step, [BIO_STEP], w.fact_index(facts), "Bio"), w.FORM_REASON)
        # Posted at the same size, it's named: code says the named lines, and one assumed bucket is left.
        test = [{"class": "Bio", "bucket": "Tests", "title": "Unit 3 Test", "due": "2026-10-28", "points": 50.0}]
        facts = w.attach_next_items(bio_facts(), test)
        lines = w.code_lines(facts, "Bio")
        self.assertEqual([l["code"] for l in lines], ["drag", "move_up", "keep", "assumed"])
        self.assertEqual(w.fill_slots(lines[1]["text"], w.fact_index(facts)), "A 92 or better on the Unit 3 Test gets it to an A.")

    def test_attack_a_wrong_size_drops(self):
        idx = w.fact_index(w.attach_next_items(bio_facts(), []))
        cites = [BIO_KEEP, BIO_STEP]
        for wrong in ("If the {next:%s} is worth about 40 points like the last ones, a 38 or better keeps the B and a 46 "
                      "gets it to an A." % BIO_KEEP,
                      "If the {next:%s} is worth about 100 points like the last ones, a 76 or better keeps the B and a 92 "
                      "gets it to an A." % BIO_KEEP,
                      "If the {next:%s} is worth about 50 points like the last ones, a 37 or better keeps the B and a 46 "
                      "gets it to an A." % BIO_KEEP):
            self.assertIsNotNone(w.check_sentence(wrong, cites, idx, "Bio"), wrong)

    def test_attack_points_without_the_assumption_drop(self):
        idx = w.fact_index(w.attach_next_items(bio_facts(), []))
        self.assertEqual(w.check_sentence("A 38 or better on the {next:%s} keeps the B." % BIO_KEEP, [BIO_KEEP], idx, "Bio"),
                         "number 38 isn't the needed score or a round-up of it")
        self.assertIsNotNone(w.check_sentence("A 46 on the {next:%s} gets it to an A." % BIO_STEP, [BIO_STEP], idx, "Bio"))
        # Half the assumption is no assumption.
        self.assertIsNotNone(w.check_sentence("If the {next:%s} is worth about 50 points, a 38 or better keeps the B."
                                              % BIO_KEEP, [BIO_KEEP], idx, "Bio"))

    def test_attack_a_date_or_a_schedule_drops(self):
        idx = w.fact_index(w.attach_next_items(bio_facts(), []))
        cites = [BIO_KEEP, BIO_STEP]
        tail = " is worth about 50 points like the last ones, a 38 or better keeps the B and a 46 gets it to an A."
        cases = {
            "If the {next:%s} on Oct 30%s" % (BIO_KEEP, tail): "names a date",
            "If the {next:%s} on 10/30%s" % (BIO_KEEP, tail): "names a date",
            "If the {next:%s} tomorrow%s" % (BIO_KEEP, tail): "names a relative day (tomorrow)",
            "If the scheduled {next:%s}%s" % (BIO_KEEP, tail): "says an assumed item is scheduled",
            "If the upcoming {next:%s}%s" % (BIO_KEEP, tail): "says an assumed item is scheduled",
            "If the {next:%s} due next week%s" % (BIO_KEEP, tail): "says an assumed item is scheduled",
            "If the {next:%s} that's due%s" % (BIO_KEEP, tail): "says an assumed item is scheduled",
        }
        for text, reason in cases.items():
            self.assertEqual(w.check_sentence(text, cites, idx, "Bio"), reason, text)

    def test_attack_a_can_t_drop_claim_on_an_assumed_item_drops(self):
        facts = w.attach_next_items(bio_facts(), [])
        facts = [{**f, "needed_pct": None, "cannot_lose": True} if f["id"] == BIO_KEEP else f for f in facts]
        idx = w.fact_index(facts)
        self.assertIsNotNone(w.check_sentence("The {next:%s} can't drop it below a B." % BIO_KEEP, [BIO_KEEP], idx, "Bio"))
        # And code says no keep half there: only the step.
        first = next(l for l in w.code_lines(facts, "Bio") if l["code"] == "assumed")
        self.assertEqual(first["cites"], [BIO_STEP])
        self.assertEqual(w.fill_slots(first["text"], idx), "If the next test is worth about 50 points like the last ones, "
                                                          "a 46 or better gets it to an A.")

    def test_a_keep_needing_under_half_isn_t_said(self):
        facts = [f if f["id"] != "keep:bio:homework:10" else {**f, "needed_pct": 10.0} for f in bio_facts()]
        lines = w.code_lines(w.attach_next_items(facts, []), "Bio")
        homework = next(l for l in lines if "whatif:bio:homework:10" in l["cites"])
        self.assertEqual(homework["cites"], ["whatif:bio:homework:10"])

    def test_the_aeries_category_names_the_item(self):
        facts = bio_facts()
        facts = [{**f, "bucket": "summative", "category": "Learning Targets (Quizzes)"} if f.get("bucket") == "Tests"
                 and f["kind"] != "drag" else f for f in facts]
        idx = w.fact_index(w.attach_next_items(facts, []))
        line = next(l for l in w.code_lines(list(idx.values()), "Bio") if l["code"] == "assumed")
        self.assertTrue(w.fill_slots(line["text"], idx).startswith("If the next learning target is worth about 50 points"))


class DragLineTests(unittest.TestCase):
    """On a class with no recent move, code names the past item that costs it the most."""

    def test_the_drag_line_passes_and_reads(self):
        facts = bio_facts()
        idx = w.fact_index(facts)
        text = w.say_drag(w.drag_fact(facts))
        self.assertEqual(text, "The {item:%s} is what's pulling it down most." % BIO_DRAG)
        self.assertIsNone(w.check_sentence(text, [BIO_DRAG], idx, "Bio"))
        self.assertEqual(w.fill_slots(text, idx), "The Unit 2 Test (31/50) is what's pulling it down most.")

    def test_no_drag_line_on_a_class_that_moved(self):
        move = {"id": "move:bio:2026-10-15", "kind": "grade_move", "class": "Bio", "since": "2026-10-15", "from_pct": 85.0,
                "to_pct": 84.0, "delta": -1.0, "explained": None, "coverage": None, "coverage_trimmed": False, "causes": []}
        facts = bio_facts() + [move]
        self.assertIsNone(w.drag_fact(facts))
        self.assertNotIn("drag", [l["code"] for l in w.code_lines(facts, "Bio")])

    def test_attack_other_drag_wording_drops(self):
        idx = w.fact_index(bio_facts())
        for text in ("The {item:%s} dragged it down." % BIO_DRAG, "The {item:%s} hurt." % BIO_DRAG,
                     "The {item:%s} is what's pulling it down most since Oct 14." % BIO_DRAG,
                     "The {item:%s} is what's pulling it up most." % BIO_DRAG,
                     "The {item:%s} (30/50) is what's pulling it down most." % BIO_DRAG):
            self.assertIsNotNone(w.check_sentence(text, [BIO_DRAG], idx, "Bio"), text)
        # The drag form never backs a move: with a move cited, the moved-down check still runs.
        self.assertIsNotNone(w.check_sentence("It went down, and the {item:%s} is what's pulling it down most."
                                              % BIO_DRAG, [BIO_DRAG], idx, "Bio"))

    def test_nit_1_an_item_s_own_title_is_no_restatement(self):
        # "Late Work Reflection" and "Lab due Friday" are the items' names, not the to-do list.
        for title in ("Late Work Reflection", "Lab due Friday", "Missing Link Essay"):
            facts = bio_facts(drag_title=title)
            story = w.write_class(facts, "Bio")
            self.assertIn(title, story["story"], title)
            sentences = [{"text": "Still a B, 6 points from an A.", "cites": ["next:bio"]},
                         {"text": w.fill_slots(w.say_drag(w.drag_fact(facts)), w.fact_index(facts)), "cites": [BIO_DRAG]}]
            self.assertIsNone(w.eval_story(sentences, w.fact_index(facts)), title)
        # Outside the title, the words still trip it.
        self.assertEqual(w.eval_story([{"text": "Still a B, 6 points from an A. The Unit 2 Test was late.",
                                        "cites": ["next:bio", BIO_DRAG]}], w.fact_index(bio_facts())),
                         "restates past-due or missing work")


class EveryClassStoryTests(unittest.TestCase):
    """Every class gets a story: the letter and gap first, then the code lines wherever facts exist."""

    def test_a_quiet_class_gets_its_letter_drag_and_assumed_lines(self):
        facts = [f for f in bio_facts() if f["kind"] != "what_if"]  # no reachable what-if
        story = w.write_class(facts, "Bio")
        self.assertEqual(story["story"], "Still a B, 6 points from an A. The Unit 2 Test (31/50) is what's pulling it "
                                         "down most. If the next test is worth about 50 points like the last ones, a 38 "
                                         "or better keeps the B. If the next homework assignment is worth about 10 "
                                         "points like the last ones, a 6 or better keeps the B.")
        self.assertTrue(story["keep_sentence"].startswith("If the next test"))

    def test_an_a_opens_on_its_letter(self):
        facts = [{"id": "letter:bio", "kind": "current_letter", "class": "Bio", "pct": 95.0, "letter": "A"},
                 {"id": "keep:bio:tests:50", "kind": "keep_letter", "class": "Bio", "letter": "A", "bucket": "Tests",
                  "points": 50.0, "needed_pct": 82.0, "cannot_lose": False}]
        story = w.write_class(facts, "Bio")
        self.assertEqual(story["story"], "Still an A. If the next test is worth about 50 points like the last ones, a 41 or "
                                         "better keeps the A.")
        idx = w.fact_index(facts)
        self.assertIsNone(w.check_sentence("Still an A.", ["letter:bio"], idx, "Bio"))
        for wrong in ("Still a B.", "Still an A, 2 points from an A+.", "Up to an A."):
            self.assertIsNotNone(w.check_sentence(wrong, ["letter:bio"], idx, "Bio"), wrong)
        # No next letter at an A: an assumed gets-it-to half has nothing to stand on.
        self.assertIsNotNone(w.check_sentence("If the {next:keep:bio:tests:50} is worth about 50 points like the last ones, "
                                              "a 41 or better keeps the A and a 50 gets it to an A.",
                                              ["keep:bio:tests:50"], idx, "Bio"))

    def test_a_class_with_no_letter_fact_gets_no_story(self):
        # A gated class (no_raise_math, no letter) or one with no facts at all has nothing to open on.
        gated = [{"id": "gate:bio", "kind": "no_raise_math", "class": "Bio"}]
        self.assertIsNone(w.write_class(gated, "Bio"))


class AssumedTagTests(unittest.TestCase):
    """Tags are picked from what the stories say: named items first, else assumed ones."""

    def test_tags_use_assumed_items_and_land_on_stories_that_say_them(self):
        art = [{**f, "class": "Art", "id": f["id"].replace("bio", "art")} for f in bio_facts()]
        art = [{**f, "needed_pct": 82.0} if f["id"] == "whatif:art:tests:50" else f for f in art]
        up = w.write_up({"as_of": "2026-10-22", "facts": bio_facts() + art}, upcoming=[])
        tags = {s["class"]: s.get("tags") for s in up["stories"]}
        # Art's assumed step needs 82%, Bio's 92%: Art is the easiest win. Both keep the B at 76%: the tie goes
        # to the cushion (equal), then the name.
        self.assertEqual(tags, {"Art": ["easiest_win", "biggest_risk"], "Bio": None})
        art_story = next(s for s in up["stories"] if s["class"] == "Art")
        self.assertIn("a 41 gets it to an A", art_story["story"])

    def test_a_named_item_comes_before_an_assumed_one_in_a_class(self):
        # Homework's HW 9 is posted; the engine's own keep fact (the first) is about it.
        test = [{"class": "Bio", "bucket": "Homework", "title": "HW 9", "due": "2026-10-28", "points": 10.0}]
        facts = bio_facts()
        home = [f for f in facts if f.get("bucket") == "Homework"]
        facts = w.attach_next_items([facts[0], *home, *[f for f in facts[1:] if f not in home]], test)
        self.assertEqual(w.class_step(facts)["id"], "whatif:bio:homework:10")
        self.assertEqual(w.class_keep(facts)["id"], "keep:bio:homework:10")
        self.assertEqual(w.class_step(w.attach_next_items(bio_facts(), []))["id"], BIO_STEP)

    def test_no_tag_for_a_step_no_story_says(self):
        facts = w.attach_next_items(bio_facts(), [])
        self.assertEqual(w.easiest_win(facts, said=set()), None)
        self.assertEqual(w.easiest_win(facts, said={BIO_STEP}), "Bio")
        self.assertEqual(w.biggest_risk(facts, said={BIO_STEP}), None)
        self.assertEqual(w.biggest_risk(facts, said={BIO_KEEP}), "Bio")


# ── PR #60 review: no false "Still", one drag item, deterministic keeps, fitting stories ─────────────────────
def one_test_class(name, prior, now):
    """A class whose whole grade is one 1000-point test, so its percent is exactly `now`'s (test_insights's
    synthetic helpers), with yesterday's snapshot at `prior`."""
    from test_insights import TODAY, cat, class_input, row, snapshot
    totals = {"parse_ok": True, "layout": "percent_of_grade",
              "categories": [cat("Tests", 100.0, now[0] * 10, 1000.0, now[0])]}
    snap = snapshot("2026-10-21", {name: prior})
    return insights.class_facts(class_input(name, totals, [row("Unit Test", "Tests", now[0] * 10, 1000.0)], *now),
                                [snap], snap, TODAY)


class NoFalseStillTests(unittest.TestCase):
    """GE's must-fix 2 and the CTO's call: "Still a X" only when the letter the dashboard shows is unchanged."""

    def test_ge_repros_open_on_now(self):
        for prior, now, opening in (((89.8, "B"), (90.1, "A"), "Now an A."),
                                    ((80.2, "B"), (79.9, "C"), "Now a C, 0.1 points from a B."),
                                    ((85.0, "B"), (85.2, "B"), "Still a B, 4.8 points from an A."),
                                    ((90.4, "A"), (90.1, "A"), "Still an A.")):
            facts = one_test_class("Art", prior, now)
            self.assertNotIn("grade_move", [f["kind"] for f in facts], prior)
            story = w.write_class(w.attach_next_items(facts, []), "Art")
            self.assertTrue(story["story"].startswith(opening), (prior, now, story["story"]))

    def test_a_still_sentence_across_a_letter_change_drops(self):
        changed = {"id": "next:art", "kind": "next_letter", "class": "Art", "pct": 79.9, "letter": "C",
                   "next_letter": "B", "threshold": 80.0, "gap": 0.1,
                   "prior_shown": "B", "now_shown": "C"}
        at_a = {"id": "letter:art", "kind": "current_letter", "class": "Art", "pct": 90.1, "letter": "A",
                "prior_shown": None, "now_shown": "A"}
        idx = w.fact_index([changed])
        self.assertEqual(w.say_opening(changed), "Now a C, 0.1 points from a B.")
        self.assertIsNone(w.check_sentence("Now a C, 0.1 points from a B.", ["next:art"], idx, "Art"))
        for text in ("Still a C, 0.1 points from a B.", "It remains a C, 0.1 points from a B."):
            self.assertEqual(w.check_sentence(text, ["next:art"], idx, "Art"), "says still across a letter change", text)
        idx = w.fact_index([at_a])
        self.assertEqual(w.say_opening(at_a), "Now an A.")
        self.assertIsNone(w.check_sentence("Now an A.", ["letter:art"], idx, "Art"))
        self.assertIsNotNone(w.check_sentence("Still an A.", ["letter:art"], idx, "Art"))
        # A move the dashboard shows as a letter change, though the percents' letters agree, says the crossing.
        move = {"id": "move:art:2026-10-21", "kind": "grade_move", "class": "Art", "since": "2026-10-21",
                "from_pct": 81.0, "to_pct": 80.2, "delta": -0.8,
                "prior_shown": "A", "now_shown": "B"}
        self.assertTrue(w.say_opening(move).startswith("Slipped to a B"))


class DragRankTests(unittest.TestCase):
    """GE's must-fix 1 and the CTO's calls: one drag item, the real top cost, and none on a clear A."""

    def test_an_a_says_its_drag_only_within_2_points_of_the_b_line(self):
        for pct, said in ((95.0, False), (92.0, False), (91.9, True)):
            facts = [{"id": "letter:art", "kind": "current_letter", "class": "Art", "pct": pct, "letter": "A"},
                     {"id": "drag:art:lab", "kind": "drag", "class": "Art", "title": "Lab 3", "category": "Labs",
                      "bucket": "Labs", "earned": 6.0, "possible": 10.0, "points": 1.2}]
            self.assertEqual(w.drag_fact(facts) is not None, said, pct)

    def test_the_engine_s_no_drag_case_gives_a_story_with_no_drag_line(self):
        # GE's repro end to end: the zero tops the ranking, so no drag fact and no line naming Unit 2.
        model = insights.ClassModel("Physics", "percent_of_grade", (
            insights.Bucket("Tests", 70.0, tuple(insights.Score(f"unit{i}test", f"Unit {i} Test", "Tests", e, 50.0)
                                                 for i, e in ((1, 0.0), (2, 25.0), (3, 48.0), (4, 49.0)))),
            insights.Bucket("HW", 30.0, (insights.Score("hw1", "HW 1", "HW", 10.0, 10.0),))))
        self.assertIsNone(insights.biggest_drag(model, "Physics"))
        facts = [{"id": "next:physics", "kind": "next_letter", "class": "Physics", "pct": 72.7, "letter": "C",
                  "next_letter": "B", "threshold": 80.0, "gap": 7.3}]
        story = w.write_class(facts, "Physics")
        self.assertNotIn("pulling it down", story["story"])
        self.assertNotIn("Unit 2", story["story"])


class AssumedReviewTests(unittest.TestCase):
    """The CTO's "a perfect N", GE's nits on sizes, nouns, buckets and "due"."""

    def test_full_points_read_a_perfect_n(self):
        facts = [f if f["id"] != "keep:bio:homework:10" else {**f, "needed_pct": 100.0} for f in bio_facts()]
        idx = w.fact_index(w.attach_next_items(facts, []))
        lines = [l for l in w.code_lines(list(idx.values()), "Bio") if l["code"] == "assumed"]
        homework = w.fill_slots(lines[1]["text"], idx)
        self.assertEqual(homework, "If the next homework assignment is worth about 10 points like the last ones, a "
                                   "perfect 10 keeps the B and a perfect 10 gets it to an A.")
        step_only = w.say_assumed(None, idx["whatif:bio:homework:10"])
        self.assertTrue(step_only.endswith("a perfect 10 gets it to an A."))
        self.assertIsNone(w.check_sentence(step_only, ["whatif:bio:homework:10"], idx, "Bio"))
        # "a 10 or better" on a 10-point item isn't the form.
        self.assertIsNotNone(w.check_sentence(step_only.replace("a perfect 10", "a 10 or better"),
                                              ["whatif:bio:homework:10"], idx, "Bio"))

    def test_attack_a_keep_and_step_from_two_buckets_drop(self):
        facts = bio_facts()
        # Labs at 50 points too, so only the bucket tells the two apart.
        facts = [{**f, "id": f["id"].replace(":20", ":50"), "points": 50.0} if f.get("bucket") == "Labs" else f
                 for f in facts]
        idx = w.fact_index(w.attach_next_items(facts, []))
        keep, step = idx[BIO_KEEP], idx["whatif:bio:labs:50"]
        self.assertIsNone(w.say_assumed(keep, step))
        text = ("If the {next:%s} is worth about 50 points like the last ones, a 38 or better keeps the B and a 50 gets "
                "it to an A." % BIO_KEEP)
        self.assertIsNotNone(w.check_sentence(text, [BIO_KEEP, "whatif:bio:labs:50"], idx, "Bio"))

    def test_a_bucket_whose_sizes_vary_gets_no_assumed_line(self):
        facts = [{**f, "sizes_vary": True} if f["id"] == BIO_STEP else f for f in bio_facts()]
        facts = [f for f in facts if f["id"] != BIO_KEEP]  # the engine makes no typical keep there
        lines = w.code_lines(w.attach_next_items(facts, []), "Bio")
        self.assertNotIn(BIO_STEP, [c for l in lines for c in l["cites"]])
        self.assertEqual([l["cites"][0] for l in lines if l["code"] == "assumed"],
                         ["keep:bio:homework:10", "keep:bio:labs:20"])

    def test_a_keep_whose_bucket_sizes_vary_is_never_assumed(self):
        # GE's must-fix 2: the engine marks keep_fact's typical-size keep in such a bucket, and it's never said.
        facts = [{**f, "sizes_vary": True} if f["id"] == BIO_KEEP else f for f in bio_facts()]
        facts = [f for f in facts if f["id"] != BIO_STEP]
        self.assertFalse(w._assumable(next(f for f in facts if f["id"] == BIO_KEEP)))
        lines = w.code_lines(w.attach_next_items(facts, []), "Bio")
        self.assertNotIn(BIO_KEEP, [c for l in lines for c in l["cites"]])
        # Named at its posted points, the same keep is said.
        named = w.attach_next_items(facts, [{"class": "Bio", "bucket": "Tests", "title": "Unit 3 Test",
                                             "due": "2026-10-27", "points": 50.0}])
        self.assertEqual(w.pick_keep(w.facts_for(named, "Bio"))["id"], BIO_KEEP)

    def test_a_category_reads_as_one_item(self):
        for category, start in (("Projects and Presentations", "If the next project or presentation is worth"),
                                ("Projects & Presentations", "If the next project or presentation is worth"),
                                ("Speaking and Listening", "If the next item in Speaking and Listening is worth"),
                                ("Reading, Writing", "If the next item in Reading, Writing is worth")):
            facts = [{**f, "bucket": "summative", "category": category} if f.get("bucket") == "Tests"
                     and f["kind"] != "drag" else f for f in bio_facts()]
            idx = w.fact_index(w.attach_next_items(facts, []))
            line = next(l for l in w.code_lines(list(idx.values()), "Bio") if l["code"] == "assumed")
            self.assertTrue(w.fill_slots(line["text"], idx).startswith(start), category)

    def test_due_says_the_item_is_scheduled_but_due_to_doesn_t(self):
        self.assertTrue(w._SCHEDULED_RE.search("the next test, due soon"))
        self.assertFalse(w._SCHEDULED_RE.search("due to the Unit 2 Test"))


class StoryFitTests(unittest.TestCase):
    """GE's should-fix: a story over 600 characters sheds the drag line, then the second assumed line."""

    def story(self, limit):
        facts = [{**f, "category": "Projects and Presentations"} if f.get("bucket") == "Homework" else f
                 for f in bio_facts()]
        with mock.patch.object(w, "PUBLISH_MAX_TEXT", limit):
            return w.write_class(w.attach_next_items(facts, []), "Bio")["story"]

    def test_the_drag_line_goes_first_then_the_second_assumed_line(self):
        whole = self.story(600)
        self.assertEqual(len(whole), 342)
        self.assertIn("pulling it down", whole)
        self.assertEqual(whole.count("like the last ones"), 2)
        no_drag = self.story(341)
        self.assertNotIn("pulling it down", no_drag)
        self.assertEqual(no_drag.count("like the last ones"), 2)
        self.assertEqual(no_drag, whole.replace(" The Unit 2 Test (31/50) is what's pulling it down most.", ""))
        one = self.story(len(no_drag) - 1)
        self.assertEqual(one, "Still a B, 6 points from an A. " + DAVIDS_EXAMPLE)
        # Too long even then: write_class keeps it, and write_up drops it (story_fits).
        self.assertEqual(self.story(len(one) - 1), one)
        with mock.patch.object(w, "PUBLISH_MAX_TEXT", len(one) - 1):
            self.assertEqual(w.write_up({"as_of": "2026-10-22", "facts": bio_facts()})["stories"], [])


SUBJ_D = "Subj K"


def subj_d_facts():
    """A synthetic multi-keep A: two keep facts both named by posted items (a keep on a 20-point Lab Report and a
    typical Tests keep the 50-point Unit 4 Test matches), an assumed Homework keep, and its drag."""
    return [
        {"id": "letter:subjd", "kind": "current_letter", "class": SUBJ_D, "pct": 91.0, "letter": "A"},
        {"id": "keep:subjd:labs:20", "kind": "keep_letter", "class": SUBJ_D, "letter": "A", "bucket": "Labs",
         "points": 20.0, "needed_pct": 85.0, "cannot_lose": False},
        {"id": "keep:subjd:tests:50", "kind": "keep_letter", "class": SUBJ_D, "letter": "A", "bucket": "Tests",
         "points": 50.0, "needed_pct": 72.0, "cannot_lose": False},
        {"id": "keep:subjd:homework:10", "kind": "keep_letter", "class": SUBJ_D, "letter": "A", "bucket": "Homework",
         "points": 10.0, "needed_pct": 60.0, "cannot_lose": False},
        {"id": "drag:subjd:quiz2", "kind": "drag", "class": SUBJ_D, "title": "Quiz 2", "category": "Tests",
         "bucket": "Tests", "earned": 30.0, "possible": 50.0, "points": 1.1},
        {"id": "next:art", "kind": "next_letter", "class": "Art", "pct": 84.0, "letter": "B", "next_letter": "A",
         "threshold": 90.0, "gap": 6.0, "prior_shown": "A", "now_shown": "B"},
        {"id": "keep:art:tests:50", "kind": "keep_letter", "class": "Art", "letter": "B", "bucket": "Tests",
         "points": 50.0, "needed_pct": 99.0, "cannot_lose": False},
    ]


SUBJ_D_UPCOMING = [
    {"class": SUBJ_D, "bucket": "Labs", "title": "Lab Report", "due": "2026-10-23", "points": 20.0},
    {"class": SUBJ_D, "bucket": "Tests", "title": "Unit 4 Test", "due": "2026-10-28", "points": 50.0},
]


def ge_class(name, items, up=(), weights=(("Tests", 70.0), ("Homework", 30.0))):
    """GE's probe class shape: a percent-of-grade class from (title, category, earned, possible) rows and
    (title, category, points, due) upcoming items, posted at its rebuilt grade. Synthetic."""
    ci = {"layout": "percent_of_grade", "matches_posted": True,
          "categories": [{"name": n, "weight_pct": wt} for n, wt in weights]}
    rows = [{"description": t, "category": c, "points_earned": e, "points_possible": p, "due_date": "2026-10-01"}
            for t, c, e, p in items]
    rows += [{"description": t, "category": c, "points_earned": None, "points_possible": p, "due_date": d}
             for t, c, p, d in up]
    pct = insights.truncated_grade(insights.build_class_model(name, ci, rows))
    return {"name": name, "pct": pct, "mark": insights.letter_for(pct), "counted_insight": ci, "assignments": rows}


def ge_doc(classes, prior=None):
    prior = prior if prior is not None else {c["name"]: {"pct": c["pct"], "mark": c["mark"]} for c in classes}
    doc = insights.student_facts(classes, [], {"date": "2026-10-14", "classes": prior}, date(2026, 10, 22))
    return doc, w.upcoming_items(classes, "2026-10-22")


def ge_subj_d():
    return ge_doc([ge_class("Subj D", [("Unit 1 Test", "Tests", 40, 50), ("Unit 2 Test", "Tests", 36, 50),
                                       ("HW 1", "Homework", 7, 10), ("HW 2", "Homework", 8, 10)],
                            up=[("Lab Report", "Homework", 40, "2026-10-28")])])


class FactOrderTests(unittest.TestCase):
    """GE's must-fix 3: #57's invariant, the same facts in any order give the same bytes."""

    def test_ge_subj_d_lab_report_repro_any_order_same_bytes(self):
        # GE's exact case: a C (Tests 70 / Homework 30) with a 40-point Homework Lab Report due Oct 28 and three
        # keep facts from the engine; reversed, and over 30 shuffles.
        doc, up = ge_subj_d()
        self.assertEqual([f["id"] for f in doc["facts"] if f["kind"] == "keep_letter"],
                         ["keep:subjd:homework:40", "keep:subjd:tests:50", "keep:subjd:homework:10"])
        first = json.dumps(w.write_up(copy.deepcopy(doc), None, up))
        story = json.loads(first)["stories"][0]
        self.assertEqual(story["story"], "Still a C, 4.3 points from a B. The Unit 2 Test (36/50) is what's pulling "
                                         "it down most. A 47 or better on the Lab Report keeps the C. If the next test "
                                         "is worth about 50 points like the last ones, a 26 or better keeps the C and a "
                                         "48 gets it to a B.")
        backwards = copy.deepcopy(doc)
        backwards["facts"].reverse()
        self.assertEqual(json.dumps(w.write_up(backwards, None, up[::-1])), first)
        for seed in range(30):
            shuffled = copy.deepcopy(doc)
            random.Random(seed).shuffle(shuffled["facts"])
            self.assertEqual(json.dumps(w.write_up(shuffled, None, up)), first, seed)

    def test_the_whole_fact_list_in_any_order_gives_the_same_bytes(self):
        # The CTO's structural fix: write_up puts the facts in one canonical order where they enter, so a full
        # shuffle of several multi-category classes (keep-only categories, assumed and named items, drags,
        # moves) never changes a byte.
        classes = [
            ge_class("Subj D", [("Unit 1 Test", "Tests", 40, 50), ("Unit 2 Test", "Tests", 36, 50),
                                ("HW 1", "Homework", 7, 10), ("HW 2", "Homework", 8, 10)],
                     up=[("Lab Report", "Homework", 40, "2026-10-28")]),
            ge_class("Subj E", [("Quiz 1", "Quizzes", 18, 20), ("Quiz 2", "Quizzes", 15, 20), ("Lab 1", "Labs", 22, 25),
                                ("Lab 2", "Labs", 20, 25), ("Essay 1", "Projects", 80, 100)],
                     weights=(("Quizzes", 40.0), ("Labs", 30.0), ("Projects", 30.0))),
            ge_class("Subj G", [("Unit 1 Test", "Tests", 47, 50), ("HW 1", "Homework", 9, 10), ("HW 2", "Homework", 6, 10),
                                ("HW 3", "Homework", 10, 10)], up=[("Unit 2 Test", "Tests", 50, "2026-10-27")]),
            ge_class("Subj H", [("Test A", "Tests", 30, 50), ("Test B", "Tests", 41, 50), ("HW 1", "Homework", 4, 10),
                                ("Lab 1", "Labs", 15, 20)], weights=(("Tests", 50.0), ("Homework", 25.0), ("Labs", 25.0))),
        ]
        prior = {c["name"]: {"pct": c["pct"] + d, "mark": insights.letter_for(c["pct"] + d)}
                 for c, d in zip(classes, (0.0, 1.5, -1.2, 0.0))}
        doc, up = ge_doc(classes, prior)
        self.assertGreaterEqual(len({f["kind"] for f in doc["facts"]}), 5)
        first = json.dumps(w.write_up(copy.deepcopy(doc), None, up))
        self.assertEqual(len(json.loads(first)["stories"]), 4)
        rng = random.Random(600)
        for _ in range(60):
            shuffled = copy.deepcopy(doc)
            rng.shuffle(shuffled["facts"])
            ups = list(up)
            rng.shuffle(ups)
            self.assertEqual(json.dumps(w.write_up(shuffled, None, ups)), first)
        # The canonical order is total: the same facts sort to the same list from any start.
        self.assertEqual(w.canonical_facts(doc["facts"]), w.canonical_facts(doc["facts"][::-1]))

    def test_keep_only_categories_order_by_weight_then_name(self):
        # Two keep-only assumed categories (no what-if carries their weight): the engine's weight on the keep
        # facts orders them, whatever the fact order.
        facts = [f for f in bio_facts() if f["kind"] != "what_if"]
        for order in (facts, facts[::-1]):
            picks = w.assumed_picks(w.attach_next_items(order, []))
            self.assertEqual([k["bucket"] for k, _s in picks], ["Tests", "Homework"])
        unweighted = [{k: v for k, v in f.items() if k != "bucket_weight_pct"} for f in facts]
        for order in (unweighted, unweighted[::-1]):
            picks = w.assumed_picks(w.attach_next_items(order, []))
            self.assertEqual([k["bucket"] for k, _s in picks], ["Homework", "Labs"])

    def test_pick_keep_ranks_weight_then_due_then_id(self):
        facts = w.attach_next_items(subj_d_facts(), SUBJ_D_UPCOMING)
        weights = [{"id": "whatif:subjd:tests:50", "kind": "what_if", "class": SUBJ_D, "bucket": "Tests",
                    "bucket_weight_pct": 60.0, "points": 50.0, "target": 90.0, "needed_pct": 50.0, "reachable": True}]
        mine = w.facts_for(facts, SUBJ_D)
        rng = random.Random(7)
        for _ in range(12):
            rng.shuffle(mine)
            self.assertEqual(w.pick_keep(mine)["id"], "keep:subjd:labs:20")
            self.assertEqual(w.pick_keep(mine + weights)["id"], "keep:subjd:tests:50")

    def test_named_keeps_beat_assumed_ones_across_the_kid(self):
        # The CTO's rule: Subj D's named Lab Report keep (85) is the Biggest risk over Art's assumed 99.
        facts = w.attach_next_items(subj_d_facts(), SUBJ_D_UPCOMING)
        self.assertEqual(w.biggest_risk(facts), SUBJ_D)
        self.assertEqual(w.biggest_risk(w.facts_for(facts, "Art")), "Art")
        up = w.write_up({"as_of": "2026-10-22", "facts": subj_d_facts()}, upcoming=SUBJ_D_UPCOMING)
        self.assertEqual({s["class"]: s.get("tags") for s in up["stories"]}, {"Art": None, SUBJ_D: ["biggest_risk"]})

    def test_named_steps_beat_assumed_ones_across_the_kid(self):
        named = [{"id": "next:x", "kind": "next_letter", "class": "X", "pct": 85.0, "letter": "B", "next_letter": "A",
                  "threshold": 90.0, "gap": 5.0},
                 {"id": "whatif:x:tests:20", "kind": "what_if", "class": "X", "bucket": "Tests", "bucket_weight_pct": 50.0,
                  "points": 20.0, "target": 90.0, "needed_pct": 97.0, "reachable": True}]
        assumed = [{**f, "class": "Y", "id": f["id"].replace(":x", ":y"), "needed_pct": 60.0} if f["kind"] == "what_if"
                   else {**f, "class": "Y", "id": "next:y"} for f in named]
        upcoming = [{"class": "X", "bucket": "Tests", "title": "Quiz 4", "due": "2026-10-27", "points": 20.0}]
        facts = w.attach_next_items(named + assumed, upcoming)
        self.assertEqual(w.easiest_win(facts), "X")
        self.assertEqual(w.easiest_win(w.attach_next_items(named + assumed, [])), "Y")


class ShownLetterTests(unittest.TestCase):
    """GE's must-fix 3 and the CTO's call: Still/Now and the letter named follow the Aeries mark (the letter the
    dashboard shows), and no gap is stated when the mark and the percent disagree."""

    def test_ge_repro_last_90_2_marked_a_now_89_5_marked_a_is_still_an_a(self):
        cls = ge_class("Subj F", [("Unit 1 Test", "Tests", 45, 50), ("Unit 2 Test", "Tests", 46.5, 50),
                                  ("HW 1", "Homework", 8.5, 10)])
        self.assertEqual((cls["pct"], cls["mark"]), (89.5, "B"))
        cls["mark"] = "A"  # Aeries rounds 89.5 up
        doc, up = ge_doc([cls], {"Subj F": {"pct": 90.2, "mark": "A"}})
        move = next(f for f in doc["facts"] if f["kind"] == "grade_move")
        self.assertEqual((move["prior_shown"], move["now_shown"]), ("A", "A"))
        stories = w.write_up(doc, None, up)["stories"]
        self.assertEqual([s["story"] for s in stories], ["Still an A, down 0.7 since Oct 14."])
        # The other way: last 90.2 marked A, now 89.5 marked B is the crossing.
        cls["mark"] = "B"
        doc, up = ge_doc([cls], {"Subj F": {"pct": 90.2, "mark": "A"}})
        stories = w.write_up(doc, None, up)["stories"]
        self.assertEqual(len(stories), 1)
        self.assertTrue(stories[0]["story"].startswith("Slipped to a B since Oct 14, down 0.7. "), stories)

    def test_a_letter_fact_on_a_mismatched_mark_names_the_mark_with_no_gap(self):
        fact = {"id": "next:art", "kind": "next_letter", "class": "Art", "pct": 89.5, "letter": "B", "next_letter": "A",
                "threshold": 90.0, "gap": 0.5, "prior_shown": "A", "now_shown": "A"}
        self.assertEqual(w.say_opening(fact), "Still an A.")
        self.assertEqual(w.say_opening({**fact, "prior_shown": "B"}), "Now an A.")
        self.assertEqual(w.say_opening({**fact, "prior_shown": None}), "Now an A.")
        self.assertIsNone(w.check_sentence("Still an A.", ["next:art"], w.fact_index([fact]), "Art"))
        self.assertIsNotNone(w.check_sentence("Still a B, 0.5 points from an A.", ["next:art"], w.fact_index([fact]),
                                              "Art"))
        at_a = {"id": "letter:art", "kind": "current_letter", "class": "Art", "pct": 90.1, "letter": "A",
                "prior_shown": "B", "now_shown": "B"}
        self.assertEqual(w.say_opening(at_a), "Still a B.")
        # The engine puts both shown letters on the letter fact. A mark that isn't its percent's letter today is
        # a scale break (gated, no letter fact); last time's mark still decides Still/Now.
        engine = one_test_class("Art", (89.6, "A"), (89.5, "A"))
        self.assertEqual([f["kind"] for f in engine], ["scale_break", "no_raise_math"])
        cls = ge_class("Art", [("Unit 1 Test", "Tests", 46, 50), ("Unit 2 Test", "Tests", 45, 50),
                               ("HW 1", "Homework", 9, 10)])
        self.assertEqual(cls["mark"], "A")
        for prior_mark, opener in (("A", "Still an A."), ("B", "Now an A.")):
            doc, up = ge_doc([cls], {"Art": {"pct": cls["pct"] - 0.2, "mark": prior_mark}})
            letter = [f for f in doc["facts"] if f["kind"] in ("next_letter", "current_letter")]
            self.assertEqual([(f["prior_shown"], f["now_shown"]) for f in letter], [(prior_mark, "A")])
            self.assertEqual(w.say_opening(letter[0]), opener)


def _shown(pct, mark):
    return insights.mark_letter(mark) or insights.letter_for(pct)


class ShownPairTests(unittest.TestCase):
    """GE's #60 must-fixes after a5493a0: the engine computes prior_shown and now_shown once, and the opener's
    still/now, its letter, the crossing and the checks read only those two."""

    @staticmethod
    def subj_g(mark="B"):
        # GE's class: 79.5 to 79.9 percent, which the dashboard rounds to a B.
        for score in (x / 2 for x in range(60, 100)):
            cls = ge_class("Subj G", [("Unit 1 Test", "Tests", 40, 50), ("Unit 2 Test", "Tests", score, 50),
                                      ("HW 1", "Homework", 8, 10)])
            if 79.5 <= cls["pct"] < 80:
                cls["mark"] = mark
                return cls
        raise AssertionError("no 79.5-79.9 grade")

    def stories(self, cls, prior):
        doc, up = ge_doc([cls], {cls["name"]: prior})
        return doc, [s["story"] for s in w.write_up(doc, None, up)["stories"]]

    def test_ge_repro_a_marked_a_to_b_marked_b_is_a_crossing_never_still(self):
        cls = self.subj_g()
        doc, stories = self.stories(cls, {"pct": 89.6, "mark": "A"})
        move = next(f for f in doc["facts"] if f["kind"] == "grade_move")
        self.assertEqual((move["prior_shown"], move["now_shown"]), ("A", "B"))
        self.assertEqual(stories, ["Slipped to a B since Oct 14, down 10."])
        # Last time's mark was already a B: the same move is "Still a B".
        _doc, stories = self.stories(cls, {"pct": 89.6, "mark": "B"})
        self.assertEqual(stories, ["Still a B, down 10 since Oct 14."])

    def test_ge_repro_c_to_a_shown_b_gets_its_note(self):
        cls = self.subj_g()
        doc, stories = self.stories(cls, {"pct": 70.4, "mark": "C"})
        move = next(f for f in doc["facts"] if f["kind"] == "grade_move")
        self.assertEqual((move["prior_shown"], move["now_shown"]), ("C", "B"))
        self.assertEqual(stories, ["Up to a B since Oct 14, up 9.2."])
        # The checker takes the opener's letter from now_shown and the crossing from prior_shown != now_shown.
        idx = w.fact_index([move])
        self.assertIsNone(w.check_sentence(w.say_opening(w._say_fields(move)), [move["id"]], idx, "Subj G"))
        self.assertIsNotNone(w.check_sentence("Up to an A since Oct 14, up 9.2.", [move["id"]], idx, "Subj G"))

    @staticmethod
    def subj_g_in(lo, hi, mark):
        for score in (x / 2 for x in range(40, 101)):
            cls = ge_class("Subj G", [("Unit 1 Test", "Tests", 40, 50), ("Unit 2 Test", "Tests", score, 50),
                                      ("HW 1", "Homework", 8, 10)])
            if lo <= cls["pct"] < hi:
                cls["mark"] = mark
                return cls
        raise AssertionError((lo, hi))

    def test_ge_marks_against_the_percent_open_on_now_never_a_crossing_verb(self):
        # GE's three cases on 1b09e7e: the shown letter moved one way and the percent the other.
        for (lo, hi, mark), prior, want in (
                # "Now an A, down 1.4 since Oct 14.", "Now a B, down 3 since Oct 14. ...", "Now a C, up 1 since Oct 14."
                ((79.5, 80, "A"), {"pct": 81.0, "mark": "B"}, "Now an A, down {} since Oct 14."),
                ((80, 82, "B"), {"pct": 83.0, "mark": "C"}, "Now a B, down {} since Oct 14."),
                ((80, 83, "C"), {"pct": 79.0, "mark": "B"}, "Now a C, up {} since Oct 14.")):
            cls = self.subj_g_in(lo, hi, mark)
            doc, stories = self.stories(cls, prior)
            move = next(f for f in doc["facts"] if f["kind"] == "grade_move")
            self.assertEqual(len(stories), 1)
            opener = want.format(f"{abs(move['delta']):g}")
            self.assertTrue(stories[0] == opener or stories[0].startswith(opener + " "), (prior, mark, stories))
            # The checker is direction-aware: no "slipped to" a letter above the one it left, no "up to" below.
            idx = w.fact_index([move])
            for verb in ("Slipped", "Up"):
                text = f"{verb} to {w._article(mark)} {mark} since Oct 14."
                self.assertIsNotNone(w.check_sentence(text, [move["id"]], idx, "Subj G"), text)

    def test_a_crossing_verb_needs_the_letter_to_move_that_way(self):
        cls = self.subj_g_in(79.5, 80, "A")
        doc, _stories = self.stories(cls, {"pct": 81.0, "mark": "B"})
        move = next(f for f in doc["facts"] if f["kind"] == "grade_move")
        self.assertEqual(w.shown_direction(move), 1)
        idx = w.fact_index([move])
        self.assertEqual(w.check_sentence("It slipped to an A.", [move["id"]], idx, "Subj G"),
                         "says the class moved to A the wrong way")
        self.assertEqual(w.shown_direction({**move, "prior_shown": "A"}), 0)
        self.assertEqual(w.shown_direction({**move, "prior_shown": None}), None)
        self.assertEqual(w.shown_direction({**move, "prior_shown": "A", "now_shown": "C"}), -1)

    def test_property_still_exactly_when_the_shown_letters_match_and_every_opener_gets_a_note(self):
        rng = random.Random(60)
        near = {"A": "AB", "B": "ABC", "C": "BCD", "D": "CDF", "F": "DF"}
        checked = {"still": 0, "not_still": 0, "cross": 0, "against": 0}
        for n in range(400):
            cls = ge_class(f"Subj {n}", [("Unit 1 Test", "Tests", rng.choice(range(20, 51)), 50),
                                         ("Unit 2 Test", "Tests", rng.choice(range(20, 51)) / 1, 50),
                                         ("HW 1", "Homework", rng.choice(range(4, 11)), 10)])
            if rng.random() < 0.5:
                cls["mark"] = rng.choice(near[insights.letter_for(cls["pct"])])
            prior_pct = round(max(0.0, min(100.0, cls["pct"] + rng.choice((-12, -4, -1, -0.2, 0, 0.3, 2, 9)))), 1)
            prior_mark = rng.choice(near[insights.letter_for(prior_pct)]) if rng.random() < 0.5 \
                else insights.letter_for(prior_pct)
            doc, up = ge_doc([cls], {cls["name"]: {"pct": prior_pct, "mark": prior_mark}})
            stories = {s["class"]: s["story"] for s in w.write_up(doc, None, up)["stories"]}
            opening = w.opening_fact(w.facts_for(doc["facts"], cls["name"]))
            if opening is None:
                self.assertNotIn(cls["name"], stories, n)
                continue
            self.assertIn(cls["name"], stories, (n, opening))
            prior, now = _shown(prior_pct, prior_mark), _shown(cls["pct"], cls["mark"])
            self.assertEqual((opening["prior_shown"], opening["now_shown"]), (prior, now), n)
            first = stories[cls["name"]].split(". ")[0]
            self.assertEqual(first.startswith("Still "), prior == now, (n, first, prior, now))
            self.assertRegex(first, rf"^(Still|Now|Slipped to|Up to) an? {now}\b", n)
            checked["still" if prior == now else "not_still"] += 1
            # A crossing verb only when the shown letter's direction and the percent's agree; else "Now".
            if opening["kind"] == "grade_move" and prior != now:
                letter_way = 1 if "ABCDF".index(now) < "ABCDF".index(prior) else -1
                pct_way = (opening["delta"] > 0) - (opening["delta"] < 0)
                verb = {1: "Up to ", -1: "Slipped to "}.get(letter_way) if letter_way == pct_way else "Now "
                self.assertTrue(first.startswith(verb), (n, first, prior, now, opening["delta"]))
                checked["cross" if verb != "Now " else "against"] += 1
        self.assertGreater(min(checked.values()), 5, checked)
        self.assertGreater(min(checked["still"], checked["not_still"]), 30, checked)
