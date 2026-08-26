"""GradebookDetails parent-portal parser: Totals footer, NA/TX, counted rebuild."""

import sys
import unittest
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scraper  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_soup(name):
    return BeautifulSoup((FIXTURES / name).read_text(), "html.parser")


class ParseAssignmentRowsTests(unittest.TestCase):
    def test_keeps_score_raw_comment_documents(self):
        soup = load_soup("gradebook_perc_of_grade.html")
        rows = scraper.parse_assignment_rows(soup)
        self.assertEqual(len(rows), 4)
        slides = rows[0]
        self.assertEqual(slides["score_raw"], "5 / 5")
        self.assertEqual(slides["points_earned"], 5.0)
        self.assertEqual(slides["comment"], "")
        preso = rows[1]
        self.assertEqual(preso["comment"], "Nice work")
        self.assertEqual(preso["documents"], "1 file")
        self.assertEqual(preso["correct_raw"], "")

    def test_na_tx_keep_raw_not_blanked_by_safe_float(self):
        soup = load_soup("gradebook_na_tx.html")
        rows = scraper.parse_assignment_rows(soup)
        by_name = {r["description"]: r for r in rows}
        self.assertEqual(by_name["Transferred quiz"]["score_raw"], "TX")
        self.assertIsNone(by_name["Transferred quiz"]["points_earned"])
        self.assertEqual(by_name["Not applicable warmup"]["score_raw"], "NA")
        self.assertIsNone(by_name["Not applicable warmup"]["points_earned"])
        self.assertTrue(by_name["Bonus article"]["extra_credit"])
        self.assertEqual(by_name["Bonus article"]["points_earned"], 2.0)
        self.assertEqual(by_name["Bonus article"]["points_possible"], 0.0)

    def test_safe_float_still_returns_none_for_codes(self):
        self.assertIsNone(scraper.safe_float("NA"))
        self.assertIsNone(scraper.safe_float("TX"))
        self.assertEqual(scraper.score_status_code("NA"), "NA")
        self.assertEqual(scraper.score_status_code("TX / "), "TX")


class ParseTotalsTests(unittest.TestCase):
    def test_summative_formative_layout(self):
        soup = load_soup("gradebook_summative_formative.html")
        totals = scraper.parse_gradebook_totals(soup)
        self.assertIsNotNone(totals)
        self.assertEqual(totals["layout"], "summative_formative")
        self.assertEqual(totals["summative_weight_pct"], 100.0)
        self.assertEqual(totals["formative_weight_pct"], 0.0)
        self.assertEqual(totals["overall_perc"], 100.0)
        self.assertEqual(totals["overall_mark"], "A")
        names = {c["name"]: c for c in totals["categories"]}
        self.assertEqual(names["Summatives"]["perc"], 100.0)
        self.assertTrue(names["Formatives"]["empty"])

    def test_perc_of_grade_layout_and_min_max_note(self):
        soup = load_soup("gradebook_perc_of_grade.html")
        totals = scraper.parse_gradebook_totals(soup)
        self.assertIsNotNone(totals)
        self.assertEqual(totals["layout"], "perc_of_grade")
        self.assertTrue(totals["min_max_in_effect"])
        self.assertEqual(totals["min_assignment_pct"], 50.0)
        self.assertEqual(totals["max_assignment_pct"], 100.0)
        by_name = {c["name"]: c for c in totals["categories"]}
        self.assertEqual(by_name["Assessments"]["weight_pct"], 70.0)
        self.assertTrue(by_name["Assessments"]["empty"])
        self.assertEqual(by_name["Assignments"]["weight_pct"], 20.0)
        self.assertEqual(by_name["Presentations"]["weight_pct"], 10.0)
        self.assertEqual(by_name["Daily Assignments"]["weight_pct"], 0.0)
        self.assertEqual(totals["overall_perc"], 95.0)

    def test_soft_fail_without_footer(self):
        soup = load_soup("gradebook_na_tx.html")
        self.assertIsNone(scraper.parse_gradebook_totals(soup))


class RebuildAndStatusTests(unittest.TestCase):
    def test_empty_category_drops_out_like_aeries(self):
        soup = load_soup("gradebook_perc_of_grade.html")
        totals = scraper.parse_gradebook_totals(soup)
        assignments = scraper.parse_assignment_rows(soup)
        rebuild = scraper.rebuild_counted_percent(totals, assignments)
        self.assertEqual(rebuild, 95.0)

    def test_formative_zero_weight_does_not_count(self):
        soup = load_soup("gradebook_summative_formative.html")
        totals = scraper.parse_gradebook_totals(soup)
        assignments = scraper.parse_assignment_rows(soup)
        rebuild = scraper.rebuild_counted_percent(totals, assignments)
        self.assertEqual(rebuild, 100.0)

    def test_assignment_statuses(self):
        soup = load_soup("gradebook_perc_of_grade.html")
        group = {
            "assignments": scraper.parse_assignment_rows(soup),
            "totals": scraper.parse_gradebook_totals(soup),
        }
        scraper.finalize_gradebook_group(group)
        by_name = {a["description"]: a for a in group["assignments"]}
        self.assertEqual(by_name["About Me Slides"]["status"], "counts")
        self.assertEqual(by_name["About Me Slides"]["status_label"], "counts in Assignments")
        self.assertEqual(by_name["Daily Assignment"]["status"], "zero_weight")
        self.assertEqual(by_name["Unit 1 Assessment"]["status"], "pending")

        soup_codes = load_soup("gradebook_na_tx.html")
        coded = scraper.parse_assignment_rows(soup_codes)
        for row in coded:
            scraper.annotate_assignment_status(row, {})
        by_name = {a["description"]: a for a in coded}
        self.assertEqual(by_name["Transferred quiz"]["status"], "tx")
        self.assertEqual(by_name["Transferred quiz"]["status_label"], "TX")
        self.assertEqual(by_name["Not applicable warmup"]["score_raw"], "NA")
        self.assertEqual(by_name["Bonus article"]["status"], "extra_credit")

    def test_na_tx_not_treated_as_missing(self):
        today = datetime(2026, 8, 26)
        soup = load_soup("gradebook_na_tx.html")
        rows = scraper.parse_assignment_rows(soup)
        for row in rows:
            scraper.annotate_assignment_status(row, {})
        past = [a for a in rows if scraper.is_past_due_ungraded(a, today)]
        self.assertEqual(past, [])

    def test_posted_percent_never_overwritten(self):
        classes = [{
            "period": 1,
            "course_name": "Mkt Adv GrphDes",
            "percent": "95.0",
            "mark": "A",
        }]
        soup = load_soup("gradebook_perc_of_grade.html")
        groups = [{
            "class_name": "Mkt Adv GrphDes",
            "period": 1,
            "assignments": scraper.parse_assignment_rows(soup),
            "totals": scraper.parse_gradebook_totals(soup),
        }]
        scraper.attach_gradebook_insights(classes, groups)
        self.assertEqual(classes[0]["percent"], "95.0")
        self.assertEqual(classes[0]["mark"], "A")
        self.assertEqual(classes[0]["counted_insight"]["rebuild_pct"], 95.0)
        self.assertTrue(classes[0]["counted_insight"]["matches_posted"])

    def test_rebuild_differs_still_keeps_posted(self):
        classes = [{
            "period": 2,
            "course_name": "Engineering Geo",
            "percent": "0.0",
            "mark": "",
        }]
        soup = load_soup("gradebook_perc_of_grade.html")
        groups = [{
            "class_name": "Engineering Geo",
            "period": 2,
            "assignments": scraper.parse_assignment_rows(soup),
            "totals": scraper.parse_gradebook_totals(soup),
        }]
        scraper.attach_gradebook_insights(classes, groups)
        self.assertEqual(classes[0]["percent"], "0.0")
        self.assertEqual(classes[0]["counted_insight"]["posted_pct"], 0.0)
        self.assertFalse(classes[0]["counted_insight"]["matches_posted"])

    def test_category_breakdown_is_not_a_class_grade(self):
        soup = load_soup("gradebook_perc_of_grade.html")
        assignments = scraper.parse_assignment_rows(soup)
        totals = scraper.parse_gradebook_totals(soup)
        breakdown = scraper.build_category_breakdown(assignments, totals)
        self.assertIn("Assignments", breakdown)
        self.assertIn("assignment_avg_pct", breakdown["Assignments"])
        self.assertNotIn("avg_pct", breakdown["Assignments"])
        self.assertTrue(breakdown["Assignments"]["not_a_class_grade"])
        self.assertEqual(breakdown["Daily Assignments"]["weight_pct"], 0.0)
        self.assertFalse(breakdown["Daily Assignments"]["counts"])
        self.assertTrue(all(info.get("not_a_class_grade") for info in breakdown.values()))
        self.assertFalse(any(
            "class_grade" in key or key in ("overall_pct", "class_pct")
            for key in breakdown
        ))

    def test_no_teacher_urls_in_scraper(self):
        src = Path(scraper.__file__).read_text()
        self.assertNotIn("/teacher/", src)
        self.assertNotIn("Manage Gradebooks", src)
        self.assertNotIn("Gradebook Information", src)


if __name__ == "__main__":
    unittest.main()
