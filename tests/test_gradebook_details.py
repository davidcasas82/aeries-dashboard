"""GradebookDetails parent-portal parser: Totals footer, NA/TX, counted rebuild."""

import sys
import unittest
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import footer_fixtures as ff  # noqa: E402
import scraper  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_soup(name):
    return BeautifulSoup((FIXTURES / name).read_text(), "html.parser")


class ParseAssignmentRowsTests(unittest.TestCase):
    def test_keeps_score_raw_comment_documents(self):
        soup = load_soup("gradebook_perc_of_grade.html")
        rows = scraper.parse_assignment_rows(soup)
        self.assertEqual(len(rows), 4)
        pending = rows[3]
        self.assertEqual(pending["description"], "Unit 1 Assessment")
        self.assertEqual(pending["score_raw"], "")
        self.assertIsNone(pending["points_earned"])
        self.assertEqual(pending["points_possible"], 20.0)
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

    def test_mi_score_cell_sets_aeries_missing(self):
        html = """
        <table><tr class="assignment-info">
          <td>4<br>Date Assigned: 08/17/2026</td>
          <td>Homework 2</td><td>Assignments</td>
          <td>MI</td>
          <td></td><td>/</td><td>10</td>
          <td></td><td></td><td></td><td></td>
          <td></td><td></td><td></td>
          <td>08/21/2026</td><td></td><td></td>
        </tr></table>"""
        rows = scraper.parse_assignment_rows(BeautifulSoup(html, "html.parser"))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["aeries_missing"])
        self.assertIsNone(rows[0]["points_earned"])
        self.assertEqual(rows[0]["score_raw"], "MI")
        # An ordinary blank is not flagged
        soup = load_soup("gradebook_perc_of_grade.html")
        self.assertFalse(scraper.parse_assignment_rows(soup)[3]["aeries_missing"])

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
        self.assertEqual(names["Summatives"]["weight_basis"], "bucket")
        self.assertEqual(names["Formatives"]["kind"], "formative")
        self.assertEqual(names["Formatives"]["weight_pct"], 0.0)

    def test_perc_of_grade_layout_and_min_max_note(self):
        soup = load_soup("gradebook_perc_of_grade.html")
        totals = scraper.parse_gradebook_totals(soup)
        self.assertIsNotNone(totals)
        self.assertEqual(totals["layout"], "percent_of_grade")
        self.assertTrue(all(c["weight_basis"] == "category" for c in totals["categories"]))
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


POG_B = [("Assessments", 70), ("Assignments", 20), ("Presentations", 10), ("Daily Assignments", 0)]


def _weights(totals):
    return [(c["name"], c.get("kind"), c["weight_pct"], c["weight_basis"]) for c in totals["categories"]]


class FooterMarkupVariantTests(unittest.TestCase):
    """Plausible real footer markup, not only the hand-written fixtures."""

    def assert_pog_b(self, totals):
        self.assertIsNotNone(totals)
        self.assertEqual(totals["layout"], "percent_of_grade")
        self.assertEqual(_weights(totals), [
            ("Assessments", None, 70.0, "category"),
            ("Assignments", None, 20.0, "category"),
            ("Presentations", None, 10.0, "category"),
            ("Daily Assignments", None, 0.0, "category"),
        ])

    def test_header_row_in_thead(self):
        self.assert_pog_b(ff.totals_for(ff.page(ff.pog_table(POG_B))))

    def test_header_row_of_td_cells(self):
        self.assert_pog_b(ff.totals_for(ff.page(ff.pog_table(POG_B, header="td"))))

    def test_th_header_row_inside_tbody(self):
        self.assert_pog_b(ff.totals_for(ff.page(ff.pog_table(POG_B, header="tbody_th"))))

    def test_footer_without_totals_id_still_parses(self):
        self.assert_pog_b(ff.totals_for(ff.page(ff.pog_table(POG_B, ident='class="Grid"'))))

    def test_footer_nested_in_a_layout_table(self):
        html = ff.page(f"<table><tr><td>{ff.pog_table(POG_B)}</td></tr></table>")
        self.assert_pog_b(ff.totals_for(html))

    def test_grid_divs_percent_of_grade(self):
        html = ff.page(ff.grid_div(ff.POG_HEADERS, ff.pog_rows(POG_B)))
        self.assert_pog_b(ff.totals_for(html))

    def test_grid_divs_summative_formative(self):
        html = ff.page(ff.grid_div(ff.sf_headers(100, 0),
                                   ff.sf_rows([("Summatives", "summative"), ("Formatives", "none")])))
        totals = ff.totals_for(html)
        self.assertEqual(totals["layout"], "summative_formative")
        self.assertEqual(totals["summative_weight_pct"], 100.0)
        self.assertEqual(_weights(totals), [
            ("Summatives", "summative", 100.0, "bucket"),
            ("Formatives", "formative", 0.0, "bucket"),
        ])

    def test_summative_formative_neutral_names_both_sides(self):
        html = ff.page(ff.sf_table(70, 30, [
            ("Assessments", "summative"), ("Classwork", "formative"), ("Projects", "both"),
        ]))
        self.assertEqual(_weights(ff.totals_for(html)), [
            ("Assessments", "summative", 70.0, "bucket"),
            ("Classwork", "formative", 30.0, "bucket"),
            ("Projects", "summative", 70.0, "bucket"),
            ("Projects", "formative", 30.0, "bucket"),
        ])
        insight = ff.insight_for(html)
        self.assertEqual(insight["summative_weight_pct"], 70.0)
        self.assertEqual(insight["formative_weight_pct"], 30.0)
        self.assertEqual({c["weight_basis"] for c in insight["categories"]}, {"bucket"})

    def test_two_row_bucket_header(self):
        html = ff.page(ff.sf_two_row_header_table(70, 30, [("Assessments", "summative"), ("Classwork", "formative")]))
        totals = ff.totals_for(html)
        self.assertEqual(totals["layout"], "summative_formative")
        self.assertEqual(_weights(totals), [
            ("Assessments", "summative", 70.0, "bucket"),
            ("Classwork", "formative", 30.0, "bucket"),
        ])

    def test_async_update_panel_delta(self):
        delta = ff.async_delta(ff.pog_table(POG_B))
        self.assert_pog_b(ff.totals_for(delta))
        segments = scraper._async_delta_segments(delta)
        panels = [(ident, content) for typ, ident, content in segments if typ == "updatePanel"]
        self.assertEqual(panels[0][0], "ctl00_MainContent_subGBS_upEverything")
        self.assertIn("Perc of Grade", panels[0][1])
        self.assertEqual(segments[-1][:2], ("hiddenField", "__VIEWSTATE"))

    def test_perc_of_grade_header_spellings(self):
        for spelling in ("Perc of Grade", "Percent of Grade", "% of Grade", "Weight"):
            layout, idx = scraper._classify_totals_headers(["Category", spelling, "Points", "Max", "Perc"])
            self.assertEqual(layout, "percent_of_grade", spelling)
            self.assertEqual(idx["weight"], 1)
            self.assertEqual(idx["perc"], 4)


class FooterRejectReasonTests(unittest.TestCase):
    def reasons(self, html):
        diagnostics = []
        self.assertIsNone(ff.totals_for(html, diagnostics))
        return diagnostics

    def test_missing_weight_column_is_named(self):
        html = ff.page('<table id="tblTotals"><tr><th>Category</th><th>Points</th><th>Max</th>'
                       "<th>Perc</th></tr><tr><td>Assessments</td><td>1</td><td>2</td><td>50%</td></tr></table>")
        self.assertEqual(self.reasons(html), [{
            "container": "table#tblTotals",
            "reason": "no Perc of Grade or Summative/Formative Perc column",
        }])

    def test_one_sided_summative_header_is_named(self):
        html = ff.page('<table id="tblTotals"><tr><th>Category</th><th>Summative Perc (100%)</th></tr>'
                       "<tr><td>Summatives</td><td>90%</td></tr></table>")
        self.assertEqual(self.reasons(html)[0]["reason"], "only one of Summative/Formative Perc columns")

    def test_header_with_no_rows_is_named(self):
        html = ff.page('<table id="tblTotals"><tr><th>Category</th><th>Perc of Grade</th></tr></table>')
        self.assertEqual(self.reasons(html)[0]["reason"], "no category rows under the header")

    def test_assignment_list_is_never_reported(self):
        self.assertEqual(self.reasons(ff.page()), [{
            "container": "",
            "reason": "no footer candidate among 2 table(s) and 0 grid div(s)",
        }])

    def test_zero_of_n_warning(self):
        warning = scraper.totals_parse_warning("student 1", 0, 7)
        self.assertTrue(warning.startswith("::warning::GradebookDetails totals parsed for 0/7"))
        self.assertIn("probe_gradebook_totals=true", warning)
        self.assertIsNone(scraper.totals_parse_warning("student 1", 3, 7))
        self.assertIsNone(scraper.totals_parse_warning("student 1", 0, 0))


class CategoryWeightLookupTests(unittest.TestCase):
    def test_assignments_does_not_take_daily_assignments_zero(self):
        absent = scraper.category_weight_map(ff.totals_for(ff.page(ff.pog_table(
            [("Assessments", 70), ("Presentations", 30), ("Daily Assignments", 0)]))))
        self.assertIsNone(scraper.lookup_category_weight("Assignments", absent))
        present = scraper.category_weight_map(ff.totals_for(ff.page(ff.pog_table(POG_B))))
        self.assertEqual(scraper.lookup_category_weight("Assignments", present), 20.0)
        self.assertEqual(scraper.lookup_category_weight("Daily Assignments", present), 0.0)
        self.assertEqual(scraper.lookup_category_weight("Assessment", present), 70.0)

    def test_two_sided_name_is_not_called_zero_weight(self):
        weights = scraper.category_weight_map(ff.totals_for(ff.page(ff.sf_table(100, 0, [
            ("Projects", "both")]))))
        self.assertEqual(scraper.lookup_category_weight("Projects", weights), 100.0)
        row = {"category": "Projects", "points_earned": 9, "points_possible": 10, "score_raw": "9 / 10"}
        scraper.annotate_assignment_status(row, weights)
        self.assertEqual(row["status"], "counts")


class ProbeGradebookTotalsTests(unittest.TestCase):
    """The probe logs structure only: no scores, points, marks, names, or student numbers."""

    FORBIDDEN = ("43.25", "44.25", "84.8", "86.7", "83.3", "7.5", "Quill", "Fixturekid", "9900112")

    def probe(self, html):
        lines = []
        scraper.probe_totals_structure(ff.soup(html), ff.FAKE_STUDENT, out=lines.append)
        return "\n".join(lines)

    def assert_safe(self, text):
        for bad in self.FORBIDDEN:
            self.assertNotIn(bad, text)
        self.assertNotRegex(text, r"'(?:[A-F][+-]?)'")

    def test_percent_of_grade_probe(self):
        text = self.probe(ff.page(ff.pog_table(POG_B)))
        self.assert_safe(text)
        self.assertIn("table#ctl00_MainContent_subGBS_tblTotals", text)
        self.assertIn("table#ctl00_MainContent_tblStudentInfo", text)
        self.assertIn("'Perc of Grade'", text)
        self.assertIn("layout=percent_of_grade", text)
        self.assertIn("row: 'Daily Assignments' weights={'Perc of Grade': '0%'}", text)
        self.assertIn("row: 'Assessments' weights={'Perc of Grade': '70%'}", text)
        self.assertIn("<pct>", text)

    def test_summative_formative_probe_and_grid(self):
        html = ff.page(
            ff.sf_table(70, 30, [("Assessments", "summative"), ("Projects", "both")]),
            ff.grid_div(ff.sf_headers(100, 0), ff.sf_rows([("Summatives", "summative")])),
        )
        text = self.probe(html)
        self.assert_safe(text)
        self.assertIn("'Summative Perc (70%)'", text)
        self.assertIn('div#ctl00_MainContent_subGBS_divTotals.k-grid', text)
        self.assertIn("row: 'Projects' weights={}", text)
        self.assertIn("parser: layout=summative_formative", text)

    def test_rejected_footer_probe(self):
        html = ff.page('<table id="tblTotals"><tr><th>Category</th><th>Points</th><th>Perc</th></tr>'
                       "<tr><td>Assessments</td><td>43.25</td><td>84.8%</td></tr></table>")
        text = self.probe(html)
        self.assert_safe(text)
        self.assertIn("not a footer: no Perc of Grade or Summative/Formative Perc column", text)
        self.assertIn("parser: no totals", text)

    def test_probe_cli_writes_and_publishes_nothing(self):
        src = Path(scraper.__file__).read_text()
        body = src.split("def probe_gradebook_totals():", 1)[1].split("\ndef ", 1)[0]
        for call in ("persist_grades(", "publish_existing(", "post(", "write_text(", "open(",
                     "OUTPUT_FILE", "json.dump"):
            self.assertNotIn(call, body)
        workflow = (ROOT / ".github" / "workflows" / "scrape.yml").read_text()
        self.assertIn("probe_gradebook_totals:", workflow)
        self.assertIn("python scraper.py --probe-gradebook-totals", workflow)


if __name__ == "__main__":
    unittest.main()
