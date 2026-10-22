"""scraper.assignments_for_class: the period decides, then exact name, then one unambiguous contains."""

import os
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
