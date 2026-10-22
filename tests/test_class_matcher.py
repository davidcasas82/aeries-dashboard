"""scraper.assignments_for_class: the period decides, then exact name, then one unambiguous contains."""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scraper  # noqa: E402


def _group(name, period, titles):
    return {"class_name": name, "period": period, "assignments": [{"description": t} for t in titles]}


def _titles(rows):
    return sorted(r["description"] for r in rows)


class PeriodDecides(unittest.TestCase):
    def setUp(self):
        self.math = {"course_name": "Math", "period": 3}
        self.ap = {"course_name": "AP Math Analysis", "period": 5}
        self.student = {
            "classes": [self.math, self.ap],
            "assignments_by_class": [
                _group("Math", 3, ["Ch.4 Quiz", "HW 12"]),
                _group("AP Math Analysis", 5, ["Unit 2 Test"]),
            ],
        }

    def test_period_3_math_skips_period_5_ap_math(self):
        self.assertEqual(_titles(scraper.assignments_for_class(self.student, self.math)), ["Ch.4 Quiz", "HW 12"])

    def test_period_5_ap_math_skips_period_3_math(self):
        self.assertEqual(_titles(scraper.assignments_for_class(self.student, self.ap)), ["Unit 2 Test"])

    def test_different_period_never_matches_even_on_the_same_name(self):
        student = {"classes": [self.math], "assignments_by_class": [_group("Math", 4, ["Other section"])]}
        self.assertEqual(scraper.assignments_for_class(student, self.math), [])

    def test_period_from_the_label_counts(self):
        student = {
            "classes": [self.math, self.ap],
            "assignments_by_class": [
                {"class_name": "3- Math- Fall", "assignments": [{"description": "HW 12"}]},
                {"class_name": "5- AP Math Analysis- Fall", "assignments": [{"description": "Unit 2 Test"}]},
            ],
        }
        self.assertEqual(_titles(scraper.assignments_for_class(student, self.math)), ["HW 12"])

    def test_period_lookup_still_works(self):
        self.assertEqual(_titles(scraper.assignments_for_period(self.student, 5)), ["Unit 2 Test"])


class NoPeriods(unittest.TestCase):
    def setUp(self):
        self.math = {"course_name": "Math", "period": None}
        self.ap = {"course_name": "AP Math Analysis", "period": None}

    def test_exact_name_wins_over_contains(self):
        student = {
            "classes": [self.math, self.ap],
            "assignments_by_class": [
                _group("Math", None, ["HW 12"]),
                _group("AP Math Analysis", None, ["Unit 2 Test"]),
            ],
        }
        self.assertEqual(_titles(scraper.assignments_for_class(student, self.math)), ["HW 12"])
        self.assertEqual(_titles(scraper.assignments_for_class(student, self.ap)), ["Unit 2 Test"])

    def test_one_unambiguous_contains_match_still_lands(self):
        english = {"course_name": "English 8", "period": None}
        student = {"classes": [english], "assignments_by_class": [_group("English 8 Honors", None, ["Essay 1"])]}
        self.assertEqual(_titles(scraper.assignments_for_class(student, english)), ["Essay 1"])

    def test_ambiguous_contains_match_lands_nowhere(self):
        student = {
            "classes": [self.math],
            "assignments_by_class": [
                _group("AP Math Analysis", None, ["Unit 2 Test"]),
                _group("Applied Math", None, ["Lab 1"]),
            ],
        }
        self.assertEqual(scraper.assignments_for_class(student, self.math), [])

    def test_contains_match_claimed_by_another_class_lands_nowhere(self):
        # Only the "Math" group exists; AP Math Analysis contains "math" but so does the Math row.
        student = {"classes": [self.math, self.ap], "assignments_by_class": [_group("Math", None, ["HW 12"])]}
        self.assertEqual(scraper.assignments_for_class(student, self.ap), [])
        self.assertEqual(_titles(scraper.assignments_for_class(student, self.math)), ["HW 12"])

    def test_group_owned_by_period_is_not_taken_by_name(self):
        math3 = {"course_name": "Math", "period": 3}
        student = {"classes": [math3, self.ap], "assignments_by_class": [_group("Math", 3, ["HW 12"])]}
        self.assertEqual(scraper.assignments_for_class(student, self.ap), [])

    def test_duplicate_groups_are_counted_once(self):
        student = {
            "classes": [self.math],
            "assignments_by_class": [_group("Math", None, ["HW 12"]), _group("Math", None, ["HW 12"])],
        }
        self.assertEqual(_titles(scraper.assignments_for_class(student, self.math)), ["HW 12"])


def _quiet_match(student, meta):
    with redirect_stdout(io.StringIO()):
        return scraper.assignments_for_class(student, meta)


class CountedSummaryGroup(unittest.TestCase):
    """assignment_group_for_class picks the group whose counted insight a class row gets."""

    def setUp(self):
        self.math = {"course_name": "Math", "period": 3}
        self.ap = {"course_name": "AP Math Analysis", "period": 5}

    def test_period_match_wins(self):
        groups = [_group("AP Math Analysis", 5, ["Unit 2 Test"]), _group("Math", 3, ["HW 12"])]
        self.assertIs(scraper.assignment_group_for_class(groups, self.math), groups[1])
        self.assertIs(scraper.assignment_group_for_class(groups, self.ap), groups[0])

    def test_period_3_math_never_gets_period_5_ap_math(self):
        groups = [_group("AP Math Analysis", 5, ["Unit 2 Test"])]
        self.assertIsNone(scraper.assignment_group_for_class(groups, self.math))

    def test_no_period_exact_name_beats_contains(self):
        math = {"course_name": "Math", "period": None}
        ap = {"course_name": "AP Math Analysis", "period": None}
        groups = [_group("Math", None, ["HW 12"]), _group("AP Math Analysis", None, ["Unit 2 Test"])]
        self.assertIs(scraper.assignment_group_for_class(groups, math), groups[0])
        self.assertIs(scraper.assignment_group_for_class(groups, ap), groups[1])

    def test_ambiguous_contains_lands_nowhere(self):
        math = {"course_name": "Math", "period": None}
        groups = [_group("AP Math Analysis", None, ["Unit 2 Test"]), _group("Applied Math", None, ["Lab 1"])]
        self.assertIsNone(scraper.assignment_group_for_class(groups, math))

    def test_one_unambiguous_contains_still_lands(self):
        english = {"course_name": "English 8", "period": None}
        groups = [_group("English 8 Honors", None, ["Essay 1"])]
        self.assertIs(scraper.assignment_group_for_class(groups, english), groups[0])

    def test_group_another_class_claims_is_skipped(self):
        math = {"course_name": "Math", "period": None}
        ap = {"course_name": "AP Math Analysis", "period": None}
        groups = [_group("Math", None, ["HW 12"])]
        self.assertIsNone(scraper.assignment_group_for_class(groups, ap, [math, ap]))
        self.assertIs(scraper.assignment_group_for_class(groups, math, [math, ap]), groups[0])

    def test_attach_gradebook_insights_uses_the_rule(self):
        math = {"course_name": "Math", "period": 3, "percent": "85", "mark": "B"}
        groups = [_group("AP Math Analysis", 5, ["Unit 2 Test"])]
        groups[0]["totals"] = {"categories": []}
        scraper.attach_gradebook_insights([math], groups)
        self.assertNotIn("gradebook_totals", math)


class NoSilentZeros(unittest.TestCase):
    def setUp(self):
        getattr(scraper, "_MATCH_WARNED", set()).clear()

    def _run(self, student, meta, times=3):
        out = io.StringIO()
        with redirect_stdout(out):
            for _ in range(times):
                rows = scraper.assignments_for_class(student, meta)
        return rows, out.getvalue()

    def test_same_name_in_another_period_warns_once_with_both_periods_and_no_scores(self):
        math = {"course_name": "Math", "period": 3, "percent": "87.4", "mark": "B"}
        group = _group("Math", 4, ["HW 12"])
        group["assignments"][0].update({"points_earned": 9, "points_possible": 10})
        rows, log = self._run({"sn": "1", "classes": [math], "assignments_by_class": [group]}, math)
        self.assertEqual(rows, [])
        other = [line for line in log.splitlines() if "same-name gradebook group" in line]
        self.assertEqual(len(other), 1)
        self.assertIn("'Math'", other[0])
        self.assertIn("period 3", other[0])
        self.assertIn("period 4", other[0])
        for score in ("87.4", "9", "10", "HW 12"):
            self.assertNotIn(score, log)

    def test_class_with_a_group_but_zero_assignments_warns_once(self):
        pe = {"course_name": "PE Course 1", "period": 7}
        student = {"sn": "1", "classes": [pe], "assignments_by_class": [_group("PE Course 1", 7, [])]}
        rows, log = self._run(student, pe)
        self.assertEqual(rows, [])
        zero = [line for line in log.splitlines() if "0 assignments matched" in line]
        self.assertEqual(len(zero), 1)
        self.assertIn("'PE Course 1' (period 7)", zero[0])

    def test_ambiguous_name_that_lands_nowhere_warns(self):
        math = {"course_name": "Math", "period": None}
        student = {
            "sn": "1",
            "classes": [math],
            "assignments_by_class": [_group("AP Math Analysis", None, ["Unit 2 Test"]), _group("Applied Math", None, ["Lab 1"])],
        }
        rows, log = self._run(student, math)
        self.assertEqual(rows, [])
        self.assertEqual(log.count("0 assignments matched"), 1)

    def test_each_kid_gets_its_own_warning(self):
        pe = {"course_name": "PE Course 1", "period": 7}
        log = ""
        for sn in ("1", "2"):
            _, out = self._run({"sn": sn, "classes": [pe], "assignments_by_class": [_group("PE Course 1", 7, [])]}, pe)
            log += out
        self.assertEqual(log.count("0 assignments matched"), 2)

    def test_class_without_any_group_is_quiet(self):
        art = {"course_name": "Art", "period": 2}
        rows, log = self._run({"sn": "1", "classes": [art], "assignments_by_class": [_group("Math", 3, ["HW"])]}, art)
        self.assertEqual(rows, [])
        self.assertEqual(log, "")

    def test_a_normal_match_is_quiet(self):
        math = {"course_name": "Math", "period": 3}
        _, log = self._run({"sn": "1", "classes": [math], "assignments_by_class": [_group("Math", 3, ["HW"])]}, math)
        self.assertEqual(log, "")


class BlankPeriods(unittest.TestCase):
    def test_empty_string_period_is_no_period(self):
        art = {"course_name": "Art", "period": ""}
        student = {"classes": [art], "assignments_by_class": [_group("Art", 4, ["Sketch 1"])]}
        self.assertEqual(_titles(_quiet_match(student, art)), ["Sketch 1"])
        self.assertIs(scraper.assignment_group_for_class(student["assignments_by_class"], art, [art]),
                      student["assignments_by_class"][0])

    def test_whitespace_period_is_no_period(self):
        art = {"course_name": "Art", "period": "  "}
        student = {"classes": [art], "assignments_by_class": [_group("Art", 4, ["Sketch 1"])]}
        self.assertEqual(_titles(_quiet_match(student, art)), ["Sketch 1"])

    def test_empty_group_period_falls_back_to_the_label(self):
        art = {"course_name": "Art", "period": 4}
        student = {"classes": [art], "assignments_by_class": [
            {"class_name": "4- Art- Fall", "period": "", "assignments": [{"description": "Sketch 1"}]},
        ]}
        self.assertEqual(_titles(_quiet_match(student, art)), ["Sketch 1"])

    def test_string_periods_compare_as_numbers(self):
        art = {"course_name": "Art", "period": " 4 "}
        student = {"classes": [art], "assignments_by_class": [_group("Art", 4, ["Sketch 1"])]}
        self.assertEqual(_titles(_quiet_match(student, art)), ["Sketch 1"])


if __name__ == "__main__":
    unittest.main()
