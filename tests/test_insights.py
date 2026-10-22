import sys
import unittest
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import insights  # noqa: E402
import scraper  # noqa: E402

TODAY = date(2026, 10, 22)
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def row(title, category, earned, possible, due="10/15/2026"):
    return {
        "description": title,
        "category": category,
        "points_earned": earned,
        "points_possible": possible,
        "score_raw": "" if earned is None else f"{earned} / {possible}",
        "due_date": due,
    }


def cat(name, weight, points, mx, perc, kind=None):
    return {"name": name, "kind": kind, "weight_pct": weight, "points": points, "max": mx, "perc": perc}


# Percent-of-grade class: Tests 60%, Homework 30%, Participation 10%.
POG_TOTALS = {
    "parse_ok": True,
    "layout": "percent_of_grade",
    "categories": [
        cat("Tests", 60.0, 79.0, 100.0, 79.0),
        cat("Homework", 30.0, 37.0, 40.0, 92.5),
        cat("Participation", 10.0, 10.0, 10.0, 100.0),
    ],
}
POG_ROWS = [
    row("Unit 1 Test", "Tests", 45.0, 50.0, "10/01/2026"),
    row("Unit 2 Test", "Tests", 34.0, 50.0, "10/20/2026"),
    row("HW 1", "Homework", 10.0, 10.0),
    row("HW 2", "Homework", 9.0, 10.0),
    row("HW 3", "Homework", 10.0, 10.0),
    row("HW 4", "Homework", 8.0, 10.0),
    row("Participation", "Participation", 10.0, 10.0),
    row("Unit 3 Test", "Tests", None, 50.0, "11/05/2026"),
]

# Summative/formative class: summative 70%, formative 30%.
SF_TOTALS = {
    "parse_ok": True,
    "layout": "summative_formative",
    "summative_weight_pct": 70.0,
    "formative_weight_pct": 30.0,
    "categories": [
        cat("Unit Tests", 70.0, 34.0, 40.0, 85.0, kind="summative"),
        cat("Classwork", 30.0, 27.0, 30.0, 90.0, kind="formative"),
    ],
}
SF_ROWS = [
    row("Test A", "Unit Tests", 18.0, 20.0),
    row("Test B", "Unit Tests", 16.0, 20.0),
    row("CW 1", "Classwork", 9.0, 10.0),
    row("CW 2", "Classwork", 10.0, 10.0),
    row("CW 3", "Classwork", 8.0, 10.0),
]


def class_input(name, totals, rows, pct, mark, new_keys=()):
    return {
        "name": name,
        "pct": pct,
        "mark": mark,
        "counted_insight": scraper.build_counted_insight(totals, rows, posted_pct=pct, posted_mark=mark),
        "assignments": rows,
        "new_keys": list(new_keys),
    }


def snapshot(day, classes):
    return {"date": day, "classes": {name: {"pct": pct, "mark": mark} for name, (pct, mark) in classes.items()}}


def by_kind(facts, kind):
    return [f for f in facts if f["kind"] == kind]


class PercentOfGradeGoldenTests(unittest.TestCase):
    def setUp(self):
        prior = snapshot("2026-10-21", {"Biology": (91.8, "A")})
        cls = class_input("Biology", POG_TOTALS, POG_ROWS, 85.2, "B", new_keys=["unit2test"])
        self.facts = insights.class_facts(cls, [prior], prior, TODAY)

    def test_grade_move_names_the_test_its_weight_and_points(self):
        self.assertEqual(by_kind(self.facts, "grade_move"), [{
            "id": "move:biology:2026-10-21",
            "kind": "grade_move",
            "class": "Biology",
            "since": "2026-10-21",
            "from_pct": 91.8,
            "to_pct": 85.2,
            "delta": -6.6,
            "causes": [{
                "id": "cause:biology:2026-10-21:unit2test",
                "title": "Unit 2 Test",
                "category": "Tests",
                "bucket": "Tests",
                "bucket_weight_pct": 60.0,
                "earned": 34.0,
                "possible": 50.0,
                "points": -6.6,
            }],
            "explained": -6.6,
        }])

    def test_distance_to_the_next_letter(self):
        self.assertEqual(by_kind(self.facts, "next_letter"), [{
            "id": "next:biology",
            "kind": "next_letter",
            "class": "Biology",
            "pct": 85.2,
            "letter": "B",
            "next_letter": "A",
            "threshold": 90.0,
            "gap": 4.8,
        }])

    def test_what_if_says_no_single_next_assignment_reaches_an_a(self):
        rows = [(f["id"], f["points"], f["needed_pct"], f["reachable"]) for f in by_kind(self.facts, "what_if")]
        self.assertEqual(rows, [
            ("whatif:biology:tests:50", 50.0, None, False),
            ("whatif:biology:homework:10", 10.0, None, False),
            ("whatif:biology:participation:10", 10.0, None, False),
        ])

    def test_no_gate_when_weights_match(self):
        self.assertEqual(by_kind(self.facts, "no_raise_math"), [])


class SummativeFormativeGoldenTests(unittest.TestCase):
    def setUp(self):
        cls = class_input("World History", SF_TOTALS, SF_ROWS, 86.5, "B")
        self.cls = cls
        self.facts = insights.class_facts(cls, [], None, TODAY)

    def test_model_rebuilds_the_posted_percent(self):
        model = insights.build_class_model("World History", self.cls["counted_insight"], SF_ROWS)
        self.assertEqual(insights.grade(model), 86.5)
        self.assertEqual(insights.live_weight_pct(model, "summative"), 70.0)

    def test_next_test_at_100_reaches_an_a_and_classwork_cannot(self):
        rows = [(f["id"], f["needed_pct"], f["reachable"]) for f in by_kind(self.facts, "what_if")]
        self.assertEqual(rows, [
            ("whatif:worldhistory:summative:20", 100.0, True),
            ("whatif:worldhistory:formative:10", None, False),
        ])

    def test_needed_score_for_a_bigger_test(self):
        model = insights.build_class_model("World History", self.cls["counted_insight"], SF_ROWS)
        self.assertEqual(insights.needed_score(model, "summative", 40.0, 90.0), 95.0)

    def test_no_move_without_a_prior_snapshot(self):
        self.assertEqual(by_kind(self.facts, "grade_move"), [])


class GateTests(unittest.TestCase):
    def test_weights_that_miss_the_posted_percent_get_no_raise_math(self):
        prior = snapshot("2026-10-21", {"Biology": (86.0, "B")})
        cls = class_input("Biology", POG_TOTALS, POG_ROWS, 80.0, "B", new_keys=["unit2test"])
        self.assertFalse(cls["counted_insight"]["matches_posted"])
        facts = insights.class_facts(cls, [prior], prior, TODAY)
        self.assertEqual([f["kind"] for f in facts], ["grade_move", "no_raise_math"])
        self.assertEqual(facts[0]["causes"], [])
        self.assertEqual(facts[1]["reason"], "weights_do_not_match_posted")

    def test_scale_break_in_stored_snapshots_gets_flagged_and_gated(self):
        snaps = [
            snapshot("2026-10-14", {"Biology": (88.0, "A")}),
            snapshot("2026-10-15", {"Biology": (90.0, "A-")}),
            snapshot("2026-10-16", {"Biology": (None, "")}),
        ]
        cls = class_input("Biology", POG_TOTALS, POG_ROWS, 85.2, "B")
        facts = insights.class_facts(cls, snaps, None, TODAY)
        self.assertEqual(facts[0], {
            "id": "scale:biology",
            "kind": "scale_break",
            "class": "Biology",
            "scale": "90/80/70/60",
            "days": 1,
            "examples": [{"date": "2026-10-14", "pct": 88.0, "mark": "A"}],
        })
        self.assertEqual([f["kind"] for f in facts], ["scale_break", "no_raise_math"])
        self.assertEqual(facts[1]["reason"], "scale_break")

    def test_letters_on_the_plain_scale(self):
        self.assertEqual(
            [insights.letter_for(p) for p in (90.0, 89.99, 80.0, 70.0, 60.0, 59.9)],
            ["A", "B", "B", "C", "D", "F"],
        )
        self.assertIsNone(insights.next_threshold(93.0))
        self.assertEqual(insights.next_threshold(59.0), ("D", 60.0))


class CommittedFooterFixtureTests(unittest.TestCase):
    """The parser's real output for both footer layouts feeds the model unchanged."""

    def model_for(self, fixture):
        soup = BeautifulSoup((FIXTURES / fixture).read_text(), "html.parser")
        totals = scraper.parse_gradebook_totals(soup)
        rows = scraper.parse_assignment_rows(soup)
        rebuild = scraper.rebuild_counted_percent(totals, rows)
        counted = scraper.build_counted_insight(totals, rows, posted_pct=rebuild)
        return rebuild, counted, insights.build_class_model("Fixture", counted, rows)

    def test_percent_of_grade_fixture(self):
        rebuild, counted, model = self.model_for("gradebook_perc_of_grade.html")
        self.assertEqual((rebuild, insights.grade(model)), (95.0, 95.0))
        self.assertEqual(insights.live_weight_pct(model, "Assignments"), 66.7)
        self.assertTrue(counted["matches_posted"])

    def test_summative_formative_fixture(self):
        rebuild, _counted, model = self.model_for("gradebook_summative_formative.html")
        self.assertEqual((rebuild, insights.grade(model)), (100.0, 100.0))
        self.assertEqual([b.name for b in model.buckets], ["summative"])


class PatternTests(unittest.TestCase):
    def log(self, cls, days, scrape="evening", baseline=False):
        return [{"class": cls, "first_seen": d, "scrape": scrape, "baseline": baseline} for d in days]

    def test_posting_day_needs_enough_scores_on_one_weekday(self):
        mondays = ["2026-09-28", "2026-10-05", "2026-10-12", "2026-10-19"]
        items = (
            self.log("Algebra", mondays)
            + self.log("Algebra", ["2026-10-06", "2026-10-13"], scrape="overnight")
            + self.log("Algebra", ["2026-10-08", "2026-10-15"])
            + self.log("Algebra", ["2026-10-01"] * 5, baseline=True)
            + self.log("Spanish", ["2026-10-19"] * 4)
        )
        self.assertEqual(insights.find_patterns(items, TODAY), [{
            "id": "pattern:posting_day:algebra",
            "kind": "posting_day",
            "class": "Algebra",
            "weekday": "Monday",
            "share": 0.75,
            "scores": 8,
            "window_days": 28,
        }])

    def test_no_log_no_patterns(self):
        self.assertEqual(insights.find_patterns(None, TODAY), [])


class ScraperWiringTests(unittest.TestCase):
    def test_attach_insight_facts_uses_the_prior_evening_item_list(self):
        prior = snapshot("2026-10-21", {"Biology": (91.8, "A")})
        prior["classes"]["Biology"]["items"] = [{"title": "Unit 2 Test", "due": "2026-10-20", "state": "open"}]
        history = {"students": {"k1": {"snapshots": [prior]}}}
        meta = {"period": 2, "course_name": "Biology", "percent": "85.2", "mark": "B"}
        meta["counted_insight"] = scraper.build_counted_insight(POG_TOTALS, POG_ROWS, posted_pct=85.2, posted_mark="B")
        student = {
            "classes": [meta],
            "assignments_by_class": [{"class_name": "2 Biology", "period": 2, "assignments": POG_ROWS}],
        }
        out = scraper.attach_insight_facts(student, history, "k1", today=TODAY)
        self.assertIs(student["insight_facts"], out)
        self.assertEqual(out["scale"], "90/80/70/60")
        self.assertEqual([f["id"] for f in out["facts"]][:2], ["move:biology:2026-10-21", "next:biology"])
        self.assertEqual(out["facts"][0]["causes"][0]["id"], "cause:biology:2026-10-21:unit2test")


if __name__ == "__main__":
    unittest.main()
