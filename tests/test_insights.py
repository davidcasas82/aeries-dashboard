import contextlib
import copy
import io
import json
import random
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

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
                "change": "new",
                "earned": 34.0,
                "possible": 50.0,
                "points": -6.6,
                "share": -6.6,
            }],
            "explained": -6.6,
            "coverage": 1.0,
            "coverage_trimmed": False,
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
        rows = [(f["id"], f["points"], f["needed_pct"], f["reachable"]) for f in by_kind(self.facts, "what_if") if f.get("target") != "keep"]
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
        rows = [(f["id"], f["needed_pct"], f["reachable"]) for f in by_kind(self.facts, "what_if") if f.get("target") != "keep"]
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


class FloorAndCapTests(unittest.TestCase):
    """One Tests bucket holding 70/100; the next test is 100 points."""

    def totals(self, min_pct=None, max_pct=None):
        return {
            "parse_ok": True,
            "layout": "percent_of_grade",
            "min_assignment_pct": min_pct,
            "max_assignment_pct": max_pct,
            "categories": [cat("Tests", 100.0, 70.0, 100.0, None)],
        }

    def needed(self, target, min_pct=None, max_pct=None):
        totals = self.totals(min_pct, max_pct)
        rows = [row("Quiz 1", "Tests", 70.0, 100.0)]
        model = insights.build_class_model("Algebra", scraper.build_counted_insight(totals, rows), rows)
        return insights.needed_score(model, "Tests", 100.0, target)

    def rebuild_with(self, earned, min_pct=None, max_pct=None):
        rows = [row("Quiz 1", "Tests", 70.0, 100.0), row("Next test", "Tests", earned, 100.0)]
        return scraper.rebuild_counted_percent(self.totals(min_pct, max_pct), rows)

    def test_floor_counts_a_zero_as_the_floor(self):
        self.assertEqual(self.needed(80.0, min_pct=50.0), 90.0)
        self.assertEqual(self.rebuild_with(90.0, min_pct=50.0), 80.0)
        self.assertEqual(self.rebuild_with(80.0, min_pct=50.0), 75.0)

    def test_floor_already_meets_a_low_target(self):
        self.assertEqual(self.needed(60.0, min_pct=50.0), 0.0)
        self.assertEqual(self.rebuild_with(0.0, min_pct=50.0), 60.0)

    def test_cap_stretches_the_raw_score(self):
        self.assertEqual(self.needed(75.0, max_pct=90.0), 80.0)
        self.assertEqual(self.rebuild_with(80.0, max_pct=90.0), 75.0)

    def test_cap_reached_early_and_a_target_past_it(self):
        self.assertEqual(self.needed(80.0, max_pct=90.0), 90.0)
        self.assertEqual(self.rebuild_with(90.0, max_pct=90.0), 80.0)
        self.assertIsNone(self.needed(85.0, max_pct=90.0))
        self.assertEqual(self.rebuild_with(100.0, max_pct=90.0), 80.0)

    def test_floor_and_cap_together(self):
        self.assertEqual(self.needed(75.0, min_pct=50.0, max_pct=90.0), 80.0)
        self.assertEqual(self.rebuild_with(80.0, min_pct=50.0, max_pct=90.0), 75.0)

    def test_no_floor_or_cap_is_plain_interpolation(self):
        self.assertEqual(self.needed(80.0), 90.0)
        self.assertEqual(self.needed(60.0), 50.0)


def keep_model(buckets, min_pct=None, max_pct=None, name="Physics"):
    """buckets: [(name, weight, [(earned, possible), ...])]."""
    return insights.ClassModel(name, "percent_of_grade", tuple(
        insights.Bucket(b, w, tuple(insights.Score(f"{b}{i}", f"{b} {i}", b, e, p) for i, (e, p) in enumerate(rows)))
        for b, w, rows in buckets), min_pct, max_pct)


class KeepTheLetterTests(unittest.TestCase):
    """The keep what-if: the lowest score on the next item that keeps the letter the class holds now."""

    # Tests 70% at 180/200 (90%), Classwork 30% at 30/30: a 93.0, an A.
    A_CLASS = [("Tests", 70.0, [(92, 100), (88, 100)]), ("Classwork", 30.0, [(10, 10), (10, 10), (10, 10)])]

    def assert_lands(self, model, bucket, points, needed, floor):
        """The named score lands at the floor or above; a point less lands below it."""
        self.assertGreaterEqual(insights.truncated_grade(insights.with_score(model, bucket, needed / 100 * points, points)), floor)
        self.assertLess(insights.truncated_grade(insights.with_score(model, bucket, (needed - 1) / 100 * points, points)), floor)

    def test_an_a_that_needs_a_real_score_to_stay_above_90(self):
        model = keep_model(self.A_CLASS)
        self.assertEqual(insights.grade(model), 93.0)
        facts = {f["id"]: f for f in insights.keep_search(model, 93.0)}
        test = facts["keep:physics:tests:100"]
        self.assertEqual((test["kind"], test["letter"], test["threshold"]), ("keep_letter", "A", 90.0))
        self.assertNotIn("target", test)
        # 77.1 lands at 89.99, shown 89.9 (cut off, not rounded); 77.2 lands at 90.01.
        self.assertEqual((test["needed_pct"], test["cannot_lose"], test["reachable"]), (77.2, False, True))
        self.assertFalse(insights.holds(model, "Tests", 77.1, 100, 90.0))
        self.assert_lands(model, "Tests", 100, 77.2, 90.0)
        self.assertEqual(facts["keep:physics:classwork:10"]["needed_pct"], 60.0)
        self.assert_lands(model, "Classwork", 10, 60.0, 90.0)

    def test_an_a_the_next_item_cannot_cost(self):
        model = keep_model([("Tests", 100.0, [(940, 1000), (10, 10), (10, 10)])])
        fact = insights.keep_search(model, insights.grade(model))[0]
        self.assertEqual(fact["points"], 10.0)
        self.assertEqual((fact["needed_pct"], fact["cannot_lose"], fact["reachable"]), (None, True, True))
        self.assertGreaterEqual(insights.truncated_grade(insights.with_score(model, "Tests", 0.0, 10.0)), 90.0)

    def test_a_letter_even_the_cap_cannot_hold(self):
        # Capped at 85% a score, the A rests on 10 bonus points; a 200-point item at the cap pulls it to 88.33.
        model = keep_model([("Tests", 100.0, [(85, 100), (10, 0)])], max_pct=85.0)
        self.assertEqual(insights.grade(model), 95.0)
        self.assertEqual(insights.keep_score(model, "Tests", 200.0, 90.0), (None, False))
        self.assertEqual(insights.keep_score(model, "Tests", 100.0, 90.0), (85.0, False))

    def test_floor_and_cap(self):
        floored = keep_model([("Tests", 100.0, [(90, 100), (90, 100)])], min_pct=50.0)
        self.assertEqual(insights.keep_score(floored, "Tests", 100.0, 90.0), (90.0, False))
        self.assert_lands(floored, "Tests", 100, 90.0, 90.0)
        # A floor of 90 a score: even a blank keeps the A.
        self.assertEqual(insights.keep_score(keep_model([("Tests", 100.0, [(95, 100), (95, 100)])], min_pct=90.0),
                                             "Tests", 100.0, 90.0), (None, True))
        # Capped at 95: the 100 counts as 95, so the class is an 87.5 B; a 65 keeps the B.
        capped = keep_model([("Tests", 100.0, [(100, 100), (80, 100)])], max_pct=95.0)
        self.assertEqual(insights.grade(capped), 87.5)
        fact = insights.keep_search(capped, 87.5)[0]
        self.assertEqual((fact["letter"], fact["threshold"], fact["needed_pct"]), ("B", 80.0, 65.0))
        self.assert_lands(capped, "Tests", 100, 65.0, 80.0)

    def test_truncation_at_89_95_and_90_00(self):
        model = keep_model([("Tests", 100.0, [(90, 100), (90, 100)])])
        at = lambda pct: insights.with_score(model, "Tests", pct, 100.0)
        self.assertEqual(insights.truncated_grade(at(89.85)), 89.9)  # a raw 89.95
        self.assertFalse(insights.holds(model, "Tests", 89.85, 100, 90.0))
        # 89.99 on the item is a 89.9967: grade() rounds it to 90.0, Aeries shows 89.9, a B.
        self.assertEqual((insights.grade(at(89.99)), insights.truncated_grade(at(89.99))), (90.0, 89.9))
        self.assertFalse(insights.holds(model, "Tests", 89.99, 100, 90.0))
        self.assertEqual(insights.truncated_grade(at(90.0)), 90.0)
        self.assertEqual(insights.keep_score(model, "Tests", 100.0, 90.0), (90.0, False))

    def test_an_f_has_no_letter_to_keep_and_golden_classes_get_keep_facts(self):
        self.assertEqual(insights.keep_search(keep_model([("Tests", 100.0, [(40, 100)])]), 40.0), [])
        facts = insights.class_facts(class_input("Biology", POG_TOTALS, POG_ROWS, 85.2, "B"), [], None, TODAY)
        keeps = by_kind(facts, "keep_letter")
        # One per class: the Unit 3 Test (50 points, due Nov 5) is the soonest named next item.
        self.assertEqual(keeps, [{"id": "keep:biology:tests:50", "kind": "keep_letter", "class": "Biology", "letter": "B",
                                  "bucket": "Tests", "points": 50.0, "needed_pct": 53.3, "cannot_lose": False}])
        model = insights.build_class_model("Biology", class_input("Biology", POG_TOTALS, POG_ROWS, 85.2, "B")["counted_insight"], POG_ROWS)
        for f in keeps:
            if f["needed_pct"] is not None:
                self.assert_lands(model, f["bucket"], f["points"], f["needed_pct"], 80.0)

    def test_the_payload_still_fits(self):
        classes = [dict(class_input(f"Class {i}", POG_TOTALS, POG_ROWS, 85.2, "B"), name=f"Class {i}") for i in range(9)]
        facts = insights.student_facts(classes, [], None, TODAY)
        keeps = by_kind(facts["facts"], "keep_letter")
        self.assertEqual(len(keeps), 9)
        self.assertLessEqual(insights.facts_size(facts), insights.FACTS_CAP_BYTES)
        self.assertNotIn("trimmed", facts)
        data = {"students": [{"sn": "1", "insight_facts": facts}, {"sn": "2", "insight_facts": facts}]}
        scraper.fit_payload_for_family_data(data, today=TODAY)
        self.assertEqual(data["students"][0]["insight_facts"], facts)


class KeepFactShapeTests(unittest.TestCase):
    """#54 review: keep facts are their own kind, one per class, picked like the writer picks one."""

    A_CLASS = KeepTheLetterTests.A_CLASS

    def test_biology_repro_keep_fact_is_never_a_what_if(self):
        # GE's repro: Biology at 85.2 keeps the B with a 53.3 ("54 or better") on the next test. That fact
        # must not read as a next-letter what-if, so "A 54 or better ... gets it to an A." has nothing to cite.
        facts = insights.class_facts(class_input("Biology", POG_TOTALS, POG_ROWS, 85.2, "B"), [], None, TODAY)
        keep = by_kind(facts, "keep_letter")[0]
        self.assertEqual((keep["letter"], keep["needed_pct"]), ("B", 53.3))
        self.assertTrue(all(f.get("target") == 90.0 for f in by_kind(facts, "what_if")))
        self.assertFalse(any(f["id"].startswith("keep:") for f in by_kind(facts, "what_if")))
        model = keep_model(self.A_CLASS)
        self.assertTrue(all(f["kind"] == "keep_letter" for f in insights.keep_search(model, 93.0)))
        doc = insights.student_facts([class_input(f"Kid A Class {i}", POG_TOTALS, POG_ROWS, 85.2, "B") for i in range(3)],
                                     [], None, TODAY)
        for f in doc["facts"]:
            if f["id"].startswith("keep:"):
                self.assertEqual(f["kind"], "keep_letter")
                for dropped in ("target", "threshold", "bucket_weight_pct", "reachable"):
                    self.assertNotIn(dropped, f)

    def test_order_named_item_then_drop_cause_then_heaviest(self):
        model = keep_model(self.A_CLASS)  # Tests 70%, Classwork 30%: both need a real score to keep the A
        heaviest = insights.keep_fact(model, 93.0)
        self.assertEqual(heaviest["bucket"], "Tests")
        move = {"kind": "grade_move", "delta": -1.2, "causes": [
            {"bucket": "Tests", "share": 0.3, "points": 0.3},
            {"bucket": "Classwork", "share": -1.5, "points": -1.5}]}
        self.assertEqual(insights.keep_fact(model, 93.0, move=move)["bucket"], "Classwork")
        # A rise has no drop cause to follow.
        self.assertEqual(insights.keep_fact(model, 93.0, move={**move, "delta": 1.2})["bucket"], "Tests")
        upcoming = [(date(2026, 10, 23), "Lab 9", "Classwork", 20.0), (date(2026, 10, 30), "Unit 5 Test", "Tests", 100.0)]
        named = insights.keep_fact(model, 93.0, upcoming, move={**move, "causes": move["causes"][:1]})
        self.assertEqual((named["bucket"], named["points"]), ("Classwork", 20.0))

    def test_a_needed_score_beats_cannot_drop_it(self):
        # Physics: an Assessments item needs a real score to keep the A; the next Coursework item can't
        # cost it. The Assessments fact wins even though Coursework is the soonest named item and the
        # bucket the drop came from.
        model = keep_model([("Assessments", 80.0, [(902, 1000)]), ("Coursework", 20.0, [(10, 10)] * 40)], min_pct=50.0)
        self.assertEqual(insights.truncated_grade(model), 92.1)
        self.assertEqual(insights.keep_score(model, "Coursework", 10.0, 90.0), (None, True))
        upcoming = [(date(2026, 10, 23), "Lab 9", "Coursework", 10.0),
                    (date(2026, 10, 30), "Unit 5 Assessment", "Assessments", 200.0)]
        move = {"kind": "grade_move", "delta": -0.8, "causes": [{"bucket": "Coursework", "share": -0.8, "points": -0.8}]}
        fact = insights.keep_fact(model, 92.1, upcoming, move)
        self.assertEqual((fact["bucket"], fact["points"], fact["needed_pct"], fact["cannot_lose"]),
                         ("Assessments", 200.0, 74.0, False))
        # With nothing that needs a score, the can't-drop-it fact is still the one.
        safe = insights.keep_fact(model, 92.1, upcoming[:1])
        self.assertEqual(safe["bucket"], "Assessments")  # its typical 1000-point item still needs a score
        only_safe = keep_model([("Coursework", 100.0, [(10, 10)] * 40)])
        # A can't-drop fact only about a named upcoming item at its posted points; at a typical size, none.
        self.assertIsNone(insights.keep_fact(only_safe, 100.0))
        named = [(date(2026, 10, 23), "Lab 9", "Coursework", 10.0)]
        self.assertEqual(insights.keep_fact(only_safe, 100.0, named)["cannot_lose"], True)

    def test_keep_score_with_zero_points_is_none(self):
        model = keep_model([("Tests", 100.0, [(85, 100)])])
        self.assertIsNone(insights.keep_score(model, "Tests", 0.0, 80.0))
        self.assertEqual(insights.keep_search(model, 85.0, sizes={"Tests": [0.0]}), [])
        self.assertIsNone(insights.needed_score(model, "Tests", 0.0, 90.0))

    def test_posted_and_rebuilt_letters_disagree_so_no_score_is_named(self):
        # Posted 80.0 B, but the footer rebuilds to 79.7, a C as Aeries cuts it: no what-if, no keep fact.
        rows = [row("Kid A Test", "Tests", 797.0, 1000.0)]
        totals = {"parse_ok": True, "layout": "percent_of_grade", "categories": [cat("Tests", 100.0, 797.0, 1000.0, 79.7)]}
        facts = insights.class_facts(class_input("Chem", totals, rows, 80.0, "B"), [], None, TODAY)
        self.assertEqual([f["kind"] for f in facts], ["next_letter"])
        agree = insights.class_facts(class_input("Chem", totals, rows, 79.7, "C"), [], None, TODAY)
        self.assertEqual(sorted({f["kind"] for f in agree}), ["keep_letter", "next_letter", "what_if"])


class NeededScoreTruncationTests(unittest.TestCase):
    """The next-letter what-if never names a score that leaves the grade at 89.9 as Aeries shows it."""

    def test_896_of_1000_plus_a_300_point_item(self):
        model = keep_model([("Tests", 100.0, [(896, 1000)])])
        needed = insights.needed_score(model, "Tests", 300.0, 90.0)
        self.assertEqual(needed, 91.4)  # 91.3 lands at 89.99, shown 89.9
        self.assertEqual(insights.truncated_grade(insights.with_score(model, "Tests", 91.3 / 100 * 300, 300.0)), 89.9)
        self.assertGreaterEqual(insights.truncated_grade(insights.with_score(model, "Tests", needed / 100 * 300, 300.0)), 90.0)

    def test_edges(self):
        model = keep_model([("Tests", 100.0, [(80, 100)])])
        self.assertEqual(insights.needed_score(model, "Tests", 100.0, 90.0), 100.0)  # exactly 90.0 at the cap
        self.assertIsNone(insights.needed_score(model, "Tests", 50.0, 90.0))  # 85.3 at best
        self.assertEqual(insights.needed_score(keep_model([("Tests", 100.0, [(99, 100)])]), "Tests", 10.0, 90.0), 0.0)
        floored = keep_model([("Tests", 100.0, [(95, 100)] * 3)], min_pct=50.0)
        self.assertEqual(insights.needed_score(floored, "Tests", 100.0, 80.0), 0.0)  # a 50 floor keeps it at 80+
        random.seed(54)
        for _ in range(400):
            buckets = [(f"B{i}", float(random.choice([20, 30, 50])),
                        [(random.randint(0, p), p) for p in random.choices([10, 50, 100, 300, 1000], k=random.randint(1, 4))])
                       for i in range(random.randint(1, 3))]
            m = keep_model(buckets)
            step = insights.next_threshold(insights.truncated_grade(m))
            if not step:
                continue
            for b in m.buckets:
                for pts in insights._typical_sizes(b):
                    need = insights.needed_score(m, b.name, pts, step[1])
                    if need is None or need == 0.0:
                        continue
                    self.assertGreaterEqual(insights.truncated_grade(insights.with_score(m, b.name, need / 100 * pts, pts)), step[1])
                    self.assertLess(insights.truncated_grade(insights.with_score(m, b.name, (need - 0.1) / 100 * pts, pts)), step[1])


def busy_night(moves, new=6):
    """GE's synthetic busy night: Kid A, 8 classes x 40 items x 4 buckets, the last `new` scores new."""
    rng = random.Random(7)
    cats = [("Tests", 50.0, [50, 100]), ("Quizzes", 20.0, [20, 25]), ("Homework", 20.0, [10]), ("Labs", 10.0, [15, 30])]
    classes = []
    for c in range(8):
        rows = []
        for i in range(40):
            n, _w, sizes = cats[i % 4]
            p = float(rng.choice(sizes))
            e = float(round(rng.uniform(.55, 1) * p))
            rows.append(row(f"{n} Assignment {i} Chapter {i // 4}", n, e, p, f"10/{1 + i % 20:02d}/2026"))
        tot = {"parse_ok": True, "layout": "percent_of_grade", "categories": []}
        m = insights.ClassModel("x", "percent_of_grade", tuple(insights.Bucket(n, w, tuple(
            insights.Score(str(k), "", n, r["points_earned"], r["points_possible"])
            for k, r in enumerate(rows) if r["category"] == n)) for n, w, _ in cats))
        for n, w, _ in cats:
            e = sum(r["points_earned"] for r in rows if r["category"] == n)
            p = sum(r["points_possible"] for r in rows if r["category"] == n)
            tot["categories"].append(cat(n, w, e, p, round(e / p * 100, 1)))
        g = insights.grade(m)
        pct = int(g * 10) / 10
        mark = insights.letter_for(pct)
        name = f"Advanced Placement Course Number {c} Honors"
        classes.append({"name": name, "pct": pct, "mark": mark,
                        "counted_insight": scraper.build_counted_insight(tot, rows, posted_pct=pct, posted_mark=mark),
                        # One named upcoming item per class, so a can't-drop keep fact has real points to be about.
                        "assignments": rows + [row("Homework Assignment 40 Chapter 10", "Homework", None, 10.0, "10/27/2026")],
                        "new_keys": [insights.slug(r["description"]) for r in rows[-new:]],
                        "prior_scores": {}})
    prior = snapshot("2026-10-21", {c["name"]: (c["pct"] + 1.3, c["mark"]) for c in classes}) if moves else None
    return insights.student_facts(classes, [], prior, TODAY)


# main (#49, e763a3b) on busy_night: 9,251 bytes quiet, 22,332 with moves. Keep facts may add at most this.
MAIN_BUSY_NIGHT = {False: 9_251, True: 22_332}
KEEP_BUDGET_BYTES = 2_000
# busy_night(True, new=40), every score new: 30,355 bytes after the causes fold, 81,501 before it.
EVERY_SCORE_NEW_BYTES = 31_000


class InsightFactsSizeTests(unittest.TestCase):
    def test_whole_object_on_the_busy_night(self):
        for moves in (False, True):
            doc = busy_night(moves)
            size = insights.facts_size(doc)
            self.assertLessEqual(size, MAIN_BUSY_NIGHT[moves] + KEEP_BUDGET_BYTES, moves)
            self.assertLessEqual(size, insights.FACTS_CAP_BYTES)
            self.assertNotIn("trimmed", doc)
            self.assertEqual(len(by_kind(doc["facts"], "keep_letter")), 8)
            self.assertEqual(len(by_kind(doc["facts"], "grade_move")), 8 if moves else 0)

    def test_the_cap_trims_keep_then_far_what_ifs_then_oldest_patterns(self):
        doc = busy_night(True)
        log = [{"class": f"Kid A Pattern {i}", "first_seen": f"2026-10-{d:02d}", "scrape": "evening"}
               for i in range(6) for d in range(1 + i, 22, 7)]
        patterns = [{"id": f"pattern:posting_day:kidapattern{i}", "kind": "posting_day", "class": f"Kid A Pattern {i}",
                     "weekday": "Someday", "share": 1.0, "scores": 8, "window_days": 28} for i in range(6)]
        doc = {**doc, "facts": doc["facts"] + patterns}
        moves = by_kind(doc["facts"], "grade_move")
        self.assertTrue(all(m["causes"] for m in moves))
        full = insights.facts_size(doc)
        kinds = lambda d: [f["kind"] for f in d["facts"]]  # noqa: E731
        keep_bytes = sum(insights.facts_size(f) + 1 for f in by_kind(doc["facts"], "keep_letter"))

        # (a) a little over: only keep facts go, the last class's first.
        a = insights.fit_facts(doc, log, cap=full - 50)
        self.assertEqual(a["trimmed"], ["keep_letter"])
        self.assertEqual(len(by_kind(a["facts"], "keep_letter")), 7)
        self.assertNotIn("Advanced Placement Course Number 7 Honors", [f["class"] for f in by_kind(a["facts"], "keep_letter")])
        self.assertEqual(kinds(a).count("what_if"), 32)

        # (b) past all keep facts: what-ifs past the next item go; one per class stays.
        b = insights.fit_facts(doc, log, cap=full - keep_bytes - 100)
        self.assertEqual(b["trimmed"], ["keep_letter", "what_if"])
        self.assertEqual(kinds(b).count("keep_letter"), 0)
        self.assertGreaterEqual(kinds(b).count("what_if"), 8)
        self.assertLess(kinds(b).count("what_if"), 32)

        # (c) still over: the oldest patterns go (Pattern 0's last score is the oldest).
        with contextlib.redirect_stdout(io.StringIO()):
            floor = insights.fit_facts(doc, log, cap=1)
        c = insights.fit_facts(doc, log, cap=insights.facts_size(floor) + 300)
        self.assertEqual(c["trimmed"], ["keep_letter", "what_if", "posting_day"])
        left = [f["class"] for f in by_kind(c["facts"], "posting_day")]
        self.assertTrue(left and len(left) < 6)
        self.assertEqual(left, [f"Kid A Pattern {i}" for i in range(6 - len(left), 6)])

        # Nothing trims a move or its causes, even under an impossible cap.
        for out in (a, b, c, floor):
            self.assertEqual(by_kind(out["facts"], "grade_move"), moves)
        self.assertEqual(kinds(floor).count("what_if"), 8)
        self.assertEqual(kinds(floor).count("posting_day"), 0)
        self.assertEqual(kinds(floor).count("next_letter"), 8)
        # Under the cap, nothing changes.
        self.assertIs(insights.fit_facts(doc, log, cap=full), doc)


class CauseFoldTests(unittest.TestCase):
    """A move keeps its top CAUSES_MAX causes; the rest fold into other_share and leave no trace."""

    def every_score_new(self):
        return busy_night(True, new=40)

    def test_fold_keeps_eight_by_share_and_the_sum(self):
        causes = [{"id": f"cause:kida:2026-10-21:item{i}", "title": f"Folded Item {i}", "bucket": "Tests",
                   "share": round(-0.1 * (i + 1), 2), "points": round(-0.1 * (i + 1), 2)} for i in range(12)]
        causes.sort(key=lambda c: (-abs(c["share"]), c["id"]))
        move = {"id": "move:kida:2026-10-21", "kind": "grade_move", "causes": causes, "delta": -8.0,
                "explained": -7.8, "coverage": 0.98}
        out = insights.fold_causes(move)
        self.assertEqual(len(out["causes"]), 8)
        self.assertEqual([c["title"] for c in out["causes"]], [f"Folded Item {i}" for i in range(11, 3, -1)])
        # other_share is the folded causes' own shares (-0.1 - 0.2 - 0.3 - 0.4), not explained's leftover.
        self.assertEqual(out["other_share"], -1.0)
        self.assertAlmostEqual(sum(c["share"] for c in out["causes"]) + out["other_share"],
                               sum(c["share"] for c in causes), places=9)
        self.assertEqual((out["coverage"], out["other_causes"]), (0.98, 4))
        text = json.dumps(out)
        for i in range(4):
            self.assertNotIn(f"Folded Item {i}\"", text)
            self.assertNotIn(f"item{i}\"", text)
        eight = {**move, "causes": causes[:8]}
        self.assertIs(insights.fold_causes(eight), eight)

    def test_real_moves_fold_and_keep_coverage(self):
        # new=10: shares exist (10 changes or fewer), so kept shares + other_share == explained.
        # new=40: every score new, nothing to rebuild "before" from: explained and other_share are None.
        sums = 0
        for new in (10, 40):
            with patch.object(insights, "CAUSES_MAX", 10 ** 6):
                unfolded = busy_night(True, new=new)
            folded = busy_night(True, new=new)
            before, after = by_kind(unfolded["facts"], "grade_move"), by_kind(folded["facts"], "grade_move")
            self.assertEqual(len(after), len(before))
            for full, cut in zip(before, after):
                self.assertEqual((cut["coverage"], cut["explained"], cut["delta"]), (full["coverage"], full["explained"], full["delta"]))
                if len(full["causes"]) <= 8:
                    self.assertEqual(cut["causes"], full["causes"])
                    continue
                self.assertEqual(cut["causes"], full["causes"][:8])  # the same ranking, cut at 8
                self.assertEqual(cut["other_causes"], len(full["causes"]) - 8)
                if full["causes"][0]["share"] is None:
                    self.assertIsNone(cut["other_share"])
                else:
                    sums += 1
                    self.assertAlmostEqual(cut["other_share"], sum(c["share"] for c in full["causes"][8:]), delta=0.005 * len(full["causes"]))
                # No folded item's title or id is anywhere in its class's facts.
                text = json.dumps([f for f in folded["facts"] if f.get("class") == cut["class"]], ensure_ascii=False)
                for gone in full["causes"][8:]:
                    self.assertNotIn(json.dumps(gone["title"], ensure_ascii=False), text)
                    self.assertNotIn(gone["id"], json.dumps(folded))
        self.assertGreater(sums, 0)

    def test_other_share_with_and_without_shares(self):
        # 10 changed scores: shares exist. other_share is the two folded causes' own unrounded shares
        # summed, and all ten shares sum to explained.
        rows = [row(f"Kid A Quiz {i}", "Tests", float(3 + (i * 7) % 8), 10.0, "10/20/2026") for i in range(10)]
        rows += [row(f"Kid A Old {i}", "Tests", 9.0, 10.0, "10/01/2026") for i in range(5)]
        totals = {"parse_ok": True, "layout": "percent_of_grade", "categories": [cat("Tests", 100.0, 0, 0, 0)]}
        model = insights.build_class_model("Chem", totals, rows)
        new = [insights.slug(r["description"]) for r in rows[:10]]
        now = insights.grade(model)
        move = insights.explain_move(model, "Chem", now + 9.0, now, "2026-10-21", new)
        self.assertEqual((len(move["causes"]), move["other_causes"]), (8, 2))
        shares = insights._shares(model, {k: None for k in new})
        listed = {c["id"] for c in move["causes"]}
        folded = [k for k in new if f"cause:chem:2026-10-21:{k}" not in listed]
        self.assertEqual(len(folded), 2)
        self.assertEqual(move["other_share"], round(sum(shares[k] for k in folded), 2))
        self.assertAlmostEqual(sum(shares.values()), move["explained"], places=2)
        # 11 changed scores: no shares, leave-one-out points don't add up, so other_share is None.
        rows.append(row("Kid A Quiz 10", "Tests", 4.0, 10.0, "10/20/2026"))
        model = insights.build_class_model("Chem", totals, rows)
        new = [insights.slug(r["description"]) for r in rows if "Quiz" in r["description"]]
        self.assertEqual(len(new), 11)
        now = insights.grade(model)
        move = insights.explain_move(model, "Chem", now + 9.0, now, "2026-10-21", new)
        self.assertTrue(all(c["share"] is None for c in move["causes"]))
        self.assertEqual(len(move["causes"]), 8)
        self.assertEqual(move["other_causes"], 3)
        self.assertIsNone(move["other_share"])
        self.assertIsNotNone(move["coverage"])

    def test_every_score_new_night_size_and_warning(self):
        def measure(causes_max):
            out = io.StringIO()
            with patch.object(insights, "CAUSES_MAX", causes_max), contextlib.redirect_stdout(out):
                doc = self.every_score_new()
            return insights.facts_size(doc), doc, out.getvalue()

        before, doc_before, said = measure(10 ** 6)
        self.assertGreater(before, insights.FACTS_CAP_BYTES)  # 81.5 KB with every cause kept
        self.assertEqual(doc_before["trimmed"], ["keep_letter", "what_if"])
        self.assertEqual(said, f"::warning::insight_facts is {before} bytes after every trim step, "
                               f"over the {insights.FACTS_CAP_BYTES}-byte cap\n")
        self.assertNotRegex(said, r"[A-Za-z]{2,} [A-Z][a-z]+ \d")  # numbers only, no class or item names
        self.assertNotIn("Course", said)
        after, doc_after, quiet = measure(insights.CAUSES_MAX)
        self.assertLessEqual(after, EVERY_SCORE_NEW_BYTES)
        self.assertLessEqual(after, insights.FACTS_CAP_BYTES)
        self.assertEqual(quiet, "")
        self.assertNotIn("trimmed", doc_after)

    def test_warning_only_when_the_protected_facts_alone_are_over(self):
        doc = busy_night(True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fitted = insights.fit_facts(doc, cap=insights.facts_size(doc) - 10)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(fitted["trimmed"], ["keep_letter"])
        with contextlib.redirect_stdout(out):
            insights.fit_facts(doc, cap=1000)
        self.assertEqual(out.getvalue().count("::warning::"), 1)
        self.assertRegex(out.getvalue(), r"^::warning::insight_facts is \d+ bytes after every trim step, over the 1000-byte cap\n$")


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

    def test_oversize_payload_drops_insight_facts_before_failing_the_publish(self):
        facts = {"scale": "90/80/70/60", "facts": [{"id": f"next:c{i}", "note": "x" * 200} for i in range(40)]}
        data = {"students": [{"sn": "1", "name": "A", "insight_facts": facts}, {"sn": "2", "name": "B"}]}
        with patch.object(scraper, "FAMILY_LATEST_TARGET_CHARS", 2_000), \
             patch.object(scraper, "FAMILY_LATEST_MAX_CHARS", 3_000):
            size = scraper.fit_payload_for_family_data(data, today=TODAY)
        self.assertEqual(data, {"students": [{"sn": "1", "name": "A"}, {"sn": "2", "name": "B"}]})
        self.assertLess(size, 2_000)

    def test_payload_under_the_target_keeps_insight_facts(self):
        facts = {"scale": "90/80/70/60", "facts": [{"id": "next:c1"}]}
        data = {"students": [{"sn": "1", "insight_facts": facts}]}
        scraper.fit_payload_for_family_data(data, today=TODAY)
        self.assertEqual(data["students"][0]["insight_facts"], facts)


def student_for(name, totals, rows, pct, mark):
    meta = {"period": 3, "course_name": name, "percent": str(pct), "mark": mark}
    meta["counted_insight"] = scraper.build_counted_insight(totals, rows, posted_pct=pct, posted_mark=mark)
    return {"classes": [meta], "assignments_by_class": [{"class_name": f"3 {name}", "period": 3, "assignments": rows}]}


def moves_for(name, totals, rows, pct, mark, prior_row, prior_day="2026-10-14"):
    prior = {"date": prior_day, "classes": {name: prior_row}}
    history = {"students": {"k1": {"snapshots": [prior]}}}
    facts = scraper.attach_insight_facts(student_for(name, totals, rows, pct, mark), history, "k1", today=TODAY)
    return by_kind(facts["facts"], "grade_move")


# Anonymized shape of a real -1.6 move: an A class, Assessments 95% / Coursework 5%, where three
# coursework scores posted in a week. Two were open in the prior list; one was never listed at all.
LAB_TOTALS = {
    "parse_ok": True,
    "layout": "percent_of_grade",
    "categories": [
        cat("Assessments", 95.0, 24.5, 25.0, 98.0),
        cat("Formative", 0.0, 4.0, 6.0, 66.7),
        cat("Coursework", 5.0, 17.0, 26.0, 65.4),
    ],
}
LAB_ROWS = [
    row("Unit Exam 1", "Assessments", 10.0, 10.0, "09/24/2026"),
    row("Lab Practical", "Assessments", 4.5, 5.0, "10/09/2026"),
    row("Unit Exam 2", "Assessments", 10.0, 10.0, "10/08/2026"),
    row("Warmup Set", "Formative", 3.0, 3.0, "10/10/2026"),
    row("Exit Ticket", "Formative", 1.0, 3.0, "10/13/2026"),
    row("Safety Contract", "Coursework", 3.0, 3.0, "09/18/2026"),
    row("Lab Writeup A", "Coursework", 1.0, 3.0, "10/16/2026"),
    row("Lab Writeup B", "Coursework", 9.0, 10.0, "10/17/2026"),
    row("Design Brief", "Coursework", 4.0, 10.0, "10/16/2026"),
    row("Next Exam", "Assessments", None, 10.0, "10/30/2026"),
]
LAB_PRIOR_ITEMS = [
    {"title": "Lab Practical", "due": "2026-10-09", "state": "marked"},
    {"title": "Unit Exam 2", "due": "2026-10-08", "state": "marked"},
    {"title": "Warmup Set", "due": "2026-10-10", "state": "marked"},
    {"title": "Exit Ticket", "due": "2026-10-13", "state": "marked"},
    {"title": "Lab Writeup A", "due": "2026-10-16", "state": "open"},
    {"title": "Lab Writeup B", "due": "2026-10-17", "state": "open"},
    {"title": "Next Exam", "due": "2026-10-30", "state": "open"},
]

# Anonymized shape of a real +14.7 move: Assessments 70% / Assignments 20% / Presentations 10%,
# where the one assessment was rescored. It was marked the whole time, never open or missing.
STUDIO_TOTALS = {
    "parse_ok": True,
    "layout": "percent_of_grade",
    "categories": [
        cat("Assessments", 70.0, 9.2, 10.0, 92.0),
        cat("Assignments", 20.0, 55.5, 56.0, 99.1),
        cat("Presentations", 10.0, 16.5, 20.0, 82.5),
    ],
}
STUDIO_ROWS = [
    row("Portfolio Review", "Assessments", 9.2, 10.0, "10/02/2026"),
    row("Brief 1", "Assignments", 10.0, 10.0, "09/22/2026"),
    row("Brief 2", "Assignments", 10.0, 10.0, "09/29/2026"),
    row("Brief 3", "Assignments", 9.5, 10.0, "10/06/2026"),
    row("Brief 4", "Assignments", 10.0, 10.0, "10/09/2026"),
    row("Brief 5", "Assignments", 10.0, 10.0, "10/13/2026"),
    row("Sketch Check", "Assignments", 5.0, 5.0, "10/14/2026"),
    row("Sticker Check", "Assignments", 1.0, 1.0, "10/14/2026"),
    row("Pitch 1", "Presentations", 8.5, 10.0, "10/01/2026"),
    row("Pitch 2", "Presentations", 8.0, 10.0, "10/12/2026"),
]


class MoveCoverageTests(unittest.TestCase):
    """A cause is any score that is new or changed since the prior snapshot."""

    def assert_covered(self, move, delta):
        self.assertEqual(move["delta"], delta)
        self.assertAlmostEqual(move["coverage"], move["explained"] / delta, places=2)
        self.assertLessEqual(abs(move["coverage"] - 1.0), 0.2)
        shares = sum(c["share"] for c in move["causes"])
        self.assertAlmostEqual(shares, move["explained"], delta=0.02 * len(move["causes"]))

    def test_lab_drop_counts_a_score_the_prior_list_never_showed(self):
        prior_row = {"pct": 98.1, "mark": "A", "items": LAB_PRIOR_ITEMS}
        [move] = moves_for("Lab Science", LAB_TOTALS, LAB_ROWS, 96.4, "A", prior_row)
        self.assert_covered(move, -1.7)
        self.assertEqual(move["explained"], -1.73)
        causes = [(c["category"], c["change"], c["earned"], c["possible"]) for c in move["causes"]]
        self.assertEqual(causes, [
            ("Coursework", "new", 4.0, 10.0),
            ("Coursework", "new", 1.0, 3.0),
            ("Coursework", "new", 9.0, 10.0),
        ])
        # Leave-one-out stays as it was: undo one score, rebuild.
        design = move["causes"][0]
        self.assertEqual(design["points"], -0.79)
        self.assertEqual([c["share"] for c in move["causes"]], [-1.36, -0.75, 0.38])

    def test_lab_drop_with_a_score_map_sees_a_rescored_assessment(self):
        scores = scraper.snapshot_scores_for_class(LAB_ROWS)
        for key in ("labwriteupa", "labwriteupb", "designbrief"):
            scores.pop(key)
        scores["labpractical"] = [5.0, 5.0]
        prior_row = {"pct": 99.9, "mark": "A", "items": LAB_PRIOR_ITEMS, "scores": scores}
        [move] = moves_for("Lab Science", LAB_TOTALS, LAB_ROWS, 96.4, "A", prior_row)
        self.assert_covered(move, -3.5)
        changed = [c for c in move["causes"] if c["change"] == "changed"]
        self.assertEqual([(c["category"], c["prior_earned"], c["earned"]) for c in changed], [("Assessments", 5.0, 4.5)])
        self.assertEqual(changed[0]["points"], -1.9)
        self.assertEqual(sorted(c["change"] for c in move["causes"]), ["changed", "new", "new", "new"])

    def test_studio_jump_from_a_rescored_assessment_that_was_never_open(self):
        scores = scraper.snapshot_scores_for_class(STUDIO_ROWS)
        scores["portfolioreview"] = [7.1, 10.0]
        items = [{"title": "Portfolio Review", "due": "2026-10-02", "state": "marked"}]
        prior_row = {"pct": 77.7, "mark": "C", "items": items, "scores": scores}
        [move] = moves_for("Studio Design", STUDIO_TOTALS, STUDIO_ROWS, 92.4, "A", prior_row)
        self.assert_covered(move, 14.7)
        [cause] = move["causes"]
        self.assertEqual(
            (cause["category"], cause["bucket_weight_pct"], cause["change"], cause["prior_earned"], cause["earned"]),
            ("Assessments", 70.0, "changed", 7.1, 9.2),
        )
        self.assertEqual((cause["points"], cause["share"]), (14.7, 14.7))

    def test_studio_jump_with_only_an_item_list_stays_unexplained(self):
        items = [
            {"title": r["description"], "due": scraper._snapshot_due_iso(r), "state": "marked"}
            for r in STUDIO_ROWS
            if r["due_date"] >= "09/30/2026"
        ]
        [move] = moves_for("Studio Design", STUDIO_TOTALS, STUDIO_ROWS, 92.4, "A", {"pct": 77.7, "mark": "C", "items": items})
        self.assertEqual((move["causes"], move["explained"], move["coverage"]), ([], None, None))

    def test_studio_jump_from_an_assessment_posted_and_scored_the_same_day(self):
        scores = scraper.snapshot_scores_for_class(STUDIO_ROWS)
        scores.pop("portfolioreview")
        prior_row = {"pct": 93.6, "mark": "A", "items": [], "scores": scores}
        [move] = moves_for("Studio Design", STUDIO_TOTALS, STUDIO_ROWS, 92.4, "A", prior_row)
        self.assertEqual([(c["category"], c["change"]) for c in move["causes"]], [("Assessments", "new")])
        self.assertLessEqual(abs(move["coverage"] - 1.0), 0.2)


class ScoreChangeTests(unittest.TestCase):
    def test_old_scores_missing_from_the_list_are_not_new(self):
        new_keys, prior_scores = scraper.score_changes({"items": LAB_PRIOR_ITEMS}, LAB_ROWS, "2026-10-14")
        self.assertEqual(sorted(new_keys), ["designbrief", "labwriteupa", "labwriteupb"])
        self.assertEqual(prior_scores, {})

    def test_a_full_list_proves_nothing_about_absent_scores(self):
        full = LAB_PRIOR_ITEMS + [{"title": f"Filler {i}", "due": "2026-10-10", "state": "marked"} for i in range(5)]
        new_keys, _ = scraper.score_changes({"items": full}, LAB_ROWS, "2026-10-14")
        self.assertEqual(sorted(new_keys), ["labwriteupa", "labwriteupb"])

    def test_no_item_list_no_changes(self):
        self.assertEqual(scraper.score_changes({"pct": 90.0}, LAB_ROWS, "2026-10-14"), ([], {}))

    def test_score_map_lives_on_the_newest_three_days(self):
        history = {"students": {}}
        for day in ("2026-10-15", "2026-10-17", "2026-10-18", "2026-10-19"):
            snap = {"date": day, "classes": {"Lab": {"pct": 90.0, "items": [], "scores": {"a": [1, 1]}}}}
            scraper.upsert_student_snapshot(history, "1", "", snap)
        rows = [s["classes"]["Lab"] for s in history["students"]["1"]["snapshots"]]
        self.assertEqual(["scores" in r for r in rows], [False, True, True, True])
        self.assertTrue(all("items" in r for r in rows))

    def test_a_daytime_scrape_keeps_the_evening_score_map(self):
        history = {"students": {}}
        evening = {"date": "2026-10-19", "classes": {"Lab": {"pct": 90.0, "items": [], "scores": {"a": [1, 1]}}}}
        scraper.upsert_student_snapshot(history, "1", "", evening)
        later = {"date": "2026-10-19", "classes": {"Lab": {"pct": 91.0}}}
        scraper.upsert_student_snapshot(history, "1", "", later, replace_items=False)
        row = history["students"]["1"]["snapshots"][0]["classes"]["Lab"]
        self.assertEqual((row["pct"], row["scores"]), (91.0, {"a": [1, 1]}))


class NoFooterGateTests(unittest.TestCase):
    def test_a_class_with_no_footer_gets_its_own_gate_reason(self):
        cls = {"name": "PE", "pct": 98.4, "mark": "A", "counted_insight": None, "assignments": POG_ROWS, "new_keys": []}
        facts = insights.class_facts(cls, [], None, TODAY)
        self.assertEqual(facts, [{"id": "gate:pe", "kind": "no_raise_math", "class": "PE", "reason": "no_footer_weights"}])

    def test_a_footer_that_misses_keeps_the_mismatch_reason(self):
        cls = class_input("Biology", POG_TOTALS, POG_ROWS, 80.0, "B")
        facts = insights.class_facts(cls, [], None, TODAY)
        self.assertEqual(by_kind(facts, "no_raise_math")[0]["reason"], "weights_do_not_match_posted")


def big_snapshot(classes=9, per_class=60, day="2026-10-21"):
    """An evening snapshot whose score maps push it past family-data's 20,000-character cap."""
    snap = {
        "date": day,
        "captured_at": f"{day}T20:08:00-07:00",
        "since_yesterday": "One new score.",
        "change_events": [{"type": "marked", "class_name": "Class 0", "title": "Item", "due": day}],
        "classes": {},
    }
    for c in range(classes):
        scores = {}
        for i in range(per_class):
            due = f"2026-{8 + i % 3:02d}-{1 + (i * 7 + c) % 28:02d}"
            scores[f"assignmentnumber{c}x{i:03d}"] = [9.5, 10.0, due]
        snap["classes"][f"Class {c}"] = {
            "period": c,
            "pct": 90.0,
            "mark": "A",
            "missing_count": 0,
            "missing_names": [],
            "aeries_trend": None,
            "items": [{"title": f"Item {i}", "due": "2026-10-20", "state": "marked"} for i in range(5)],
            "scores": scores,
        }
    return snap


def without_scores(snap):
    out = copy.deepcopy(snap)
    for row in out["classes"].values():
        row.pop("scores", None)
        row.pop("scores_trimmed", None)
    return out


class SnapshotSizeGuardTests(unittest.TestCase):
    def test_measures_like_json_stringify(self):
        self.assertEqual(scraper.snapshot_json_chars({"a": [1, "é"]}), len('{"a":[1,"é"]}'))
        self.assertEqual(scraper.snapshot_json_chars({"a": "😀"}), len('{"a":""}') + 2)

    def test_an_oversize_snapshot_is_trimmed_under_19k_before_it_is_stored(self):
        snap = big_snapshot()
        self.assertGreater(scraper.snapshot_json_chars(snap), 20_000)
        before = copy.deepcopy(snap)
        history = {"students": {}}
        scraper.upsert_student_snapshot(history, "1", "", snap)
        stored = history["students"]["1"]["snapshots"][0]
        self.assertLessEqual(scraper.snapshot_json_chars(stored), 19_000)
        self.assertEqual(without_scores(stored), without_scores(before))
        kept = [v[2] for row in stored["classes"].values() for v in (row.get("scores") or {}).values()]
        gone = [
            v[2] for name, row in before["classes"].items() for k, v in row["scores"].items()
            if k not in (stored["classes"][name].get("scores") or {})
        ]
        self.assertTrue(kept and gone)
        self.assertLessEqual(max(gone), min(kept))
        trimmed = sum(row.get("scores_trimmed", 0) for row in stored["classes"].values())
        self.assertEqual(trimmed, len(gone))

    def test_a_small_snapshot_is_untouched(self):
        snap = big_snapshot(classes=2, per_class=10)
        before = copy.deepcopy(snap)
        self.assertEqual(scraper.fit_snapshot_scores(snap), 0)
        self.assertEqual(snap, before)

    def test_the_trim_order_does_not_depend_on_insertion_order(self):
        a = big_snapshot()
        b = copy.deepcopy(a)
        rng = random.Random(7)
        for row in b["classes"].values():
            items = list(row["scores"].items())
            rng.shuffle(items)
            row["scores"] = dict(items)
        b["classes"] = dict(reversed(list(b["classes"].items())))
        scraper.fit_snapshot_scores(a)
        scraper.fit_snapshot_scores(b)
        keys = lambda s: {n: sorted(r.get("scores") or {}) for n, r in s["classes"].items()}  # noqa: E731
        self.assertEqual(keys(a), keys(b))

    def test_scores_go_entirely_when_trimming_cannot_fit(self):
        snap = big_snapshot(classes=3, per_class=20)
        before = copy.deepcopy(snap)
        dropped = scraper.fit_snapshot_scores(snap, limit=scraper.snapshot_json_chars(without_scores(snap)) - 1)
        self.assertEqual(dropped, 60)
        self.assertEqual(without_scores(snap), without_scores(before))
        self.assertTrue(all("scores" not in r and r["scores_trimmed"] == 20 for r in snap["classes"].values()))

    def test_stored_history_is_fitted_before_publish(self):
        history = {"students": {"1": {"snapshots": [big_snapshot()]}}}
        self.assertGreater(scraper.fit_history_snapshots(history), 0)
        self.assertLessEqual(scraper.snapshot_json_chars(history["students"]["1"]["snapshots"][0]), 19_000)

    def test_a_trimmed_class_reads_as_unexplained(self):
        scores = scraper.snapshot_scores_for_class(STUDIO_ROWS)
        scores["portfolioreview"] = [7.1, 10.0, "2026-10-02"]
        items = [{"title": "Portfolio Review", "due": "2026-10-02", "state": "marked"}]
        prior_row = {"pct": 77.7, "mark": "C", "items": items, "scores": scores, "scores_trimmed": 3}
        [move] = moves_for("Studio Design", STUDIO_TOTALS, STUDIO_ROWS, 92.4, "A", prior_row)
        self.assertEqual(
            (move["delta"], move["causes"], move["explained"], move["coverage"], move["coverage_trimmed"]),
            (14.7, [], None, None, True),
        )

    def test_a_dropped_map_also_reads_as_unexplained(self):
        prior_row = {"pct": 98.1, "mark": "A", "items": LAB_PRIOR_ITEMS, "scores_trimmed": 9}
        [move] = moves_for("Lab Science", LAB_TOTALS, LAB_ROWS, 96.4, "A", prior_row)
        self.assertEqual((move["causes"], move["coverage"], move["coverage_trimmed"]), ([], None, True))

    def test_trim_marker_rides_with_the_map_through_a_daytime_merge(self):
        history = {"students": {}}
        evening = {"date": "2026-10-19", "classes": {"Lab": {"pct": 90.0, "items": [], "scores_trimmed": 4}}}
        scraper.upsert_student_snapshot(history, "1", "", evening)
        scraper.upsert_student_snapshot(history, "1", "", {"date": "2026-10-19", "classes": {"Lab": {"pct": 91.0}}}, replace_items=False)
        self.assertEqual(history["students"]["1"]["snapshots"][0]["classes"]["Lab"]["scores_trimmed"], 4)



class CannotDropNeedsANamedItemTests(unittest.TestCase):
    """GE's #55 finding: "The {next:} can't drop it below an A." was worked out at the bucket's typical size and
    was false about 19% of the time on a bigger next item. Now it is only made for a named upcoming item, at
    that item's posted points."""

    # Tests 80% at 92/100, Coursework 20% at 5 x 10/10: a 93.6. A 0 on a 10-point item keeps the A; on 20 it doesn't.
    MODEL = [("Tests", 80.0, [(92, 100)]), ("Coursework", 20.0, [(10, 10)] * 5)]

    def test_ges_case_a_typical_size_can_drop_fact_is_not_made(self):
        m = keep_model(self.MODEL)
        g = insights.truncated_grade(m)
        self.assertEqual(g, 93.6)
        self.assertEqual(insights.keep_score(m, "Coursework", 10.0, 90.0), (None, True))
        self.assertFalse(insights.holds(m, "Coursework", 0.0, 20.0, 90.0))  # the bigger next item breaks it
        for upcoming in ((), [(date(2026, 10, 23), "Big Lab", "Coursework", 20.0)]):
            fact = insights.keep_fact(m, g, upcoming)
            self.assertFalse(fact and fact["cannot_lose"], (upcoming, fact))
        only = keep_model([("Coursework", 100.0, [(10, 10)] * 40)])
        self.assertIsNone(insights.keep_fact(only, 100.0))
        # A named 400-point project: worked out at its real points it needs a score, so the score form, not can't-drop.
        big = insights.keep_fact(only, 100.0, [(date(2026, 10, 23), "Unit Project", "Coursework", 400.0)])
        self.assertEqual((big["points"], big["needed_pct"], big["cannot_lose"]), (400.0, 80.0, False))

    def test_a_named_item_where_even_a_0_keeps_the_a_gets_the_fact(self):
        m = keep_model([("Coursework", 100.0, [(10, 10)] * 40)])
        named = [(date(2026, 10, 23), "Lab 9", "Coursework", 10.0)]
        fact = insights.keep_fact(m, 100.0, named)
        self.assertEqual((fact["bucket"], fact["points"], fact["needed_pct"], fact["cannot_lose"]),
                         ("Coursework", 10.0, None, True))
        # Checked against the item's real points: the lowest score the class allows still keeps the A.
        self.assertTrue(insights.holds(m, "Coursework", insights._score_range(m)[0], 10.0, 90.0))


if __name__ == "__main__":
    unittest.main()
