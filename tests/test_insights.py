import copy
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


if __name__ == "__main__":
    unittest.main()
