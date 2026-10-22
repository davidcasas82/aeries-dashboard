"""The local write-up: synthetic facts only, and a scripted model in place of a real one."""

import json
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
    return [
        {"id": NEXT, "kind": "next_letter", "class": "Math", "pct": 87.9, "letter": "B",
         "next_letter": "A", "threshold": 90.0, "gap": 2.1},
        {"id": MOVE, "kind": "grade_move", "class": "Math", "since": "2026-10-15", "from_pct": 90.4,
         "to_pct": 87.9, "delta": -2.5, "explained": explained, "causes": [
             {"id": CAUSE, "title": "Ch.4 Quiz", "category": "Tests", "bucket": "Tests",
              "bucket_weight_pct": 60.0, "earned": 31.0, "possible": 50.0, "points": -2.4},
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
    {"text": "An 88% on the next test gets the A back.", "cites": [WHATIF]},
    {"text": "Scores mostly post on Fridays.", "cites": [PATTERN]},
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
        self.assertEqual(story["cites"], [NEXT, CAUSE, WHATIF, PATTERN])
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
        backwards = [GOOD[2], GOOD[0]]
        self.assertEqual(w.eval_story([dict(s) for s in backwards], index()),
                         "out of order (letter, cause, what-if, pattern)")

    def test_no_filler_a_quiet_class_gets_no_story_and_no_model_call(self):
        model = ScriptedModel({"Biology": [{"text": "Biology: B, 6 pts from an A.", "cites": ["next:biology"]}]})
        self.assertIsNone(w.write_class(model, quiet_facts(), "Biology"))
        self.assertEqual(model.calls, [])

    def test_the_prompt_labels_the_example_as_made_up(self):
        self.assertIn("MADE-UP EXAMPLE", w.DRAFT_SYSTEM)
        self.assertIn("never copy its numbers", w.DRAFT_SYSTEM)
        self.assertIn("Math: B, 2.1 pts from an A.", w.DRAFT_SYSTEM)


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

    def move(self, explained, delta=-1.6):
        return {"id": MOVE, "kind": "grade_move", "class": "Math", "since": "2026-10-15", "from_pct": 89.5,
                "to_pct": 87.9, "delta": delta, "explained": explained, "causes": [
                    {"id": CAUSE, "title": "Ch.4 Quiz", "category": "Tests", "bucket": "Tests",
                     "bucket_weight_pct": 60.0, "earned": 31.0, "possible": 50.0, "points": -1.5}]}

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
        self.assertEqual(w.apply_write_up(summary, {"headline": None, "stories": []})["headline"], "Grok headline")


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
