import json
import re
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import briefing  # noqa: E402
import scraper  # noqa: E402

TODAY = datetime(2026, 9, 14)  # Monday
FIXTURES = Path(__file__).parent / "fixtures"


def _patched():
    return (
        patch.object(scraper, "pacific_today_dt", return_value=TODAY),
        patch.object(scraper, "pacific_today", return_value=TODAY.date()),
    )


def kid_one():
    return {
        "name": "Student A",
        "sn": "1",
        "classes": [
            {"period": 4, "course_name": "US History", "teacher": "Healey", "percent": "88", "mark": "B+"},
            {"period": 5, "course_name": "Algebra 2", "teacher": "Byun", "percent": "81", "mark": "B-"},
            {"period": 6, "course_name": "Biology", "teacher": "Chung", "percent": "93", "mark": "A"},
            {"period": 7, "course_name": "PE Course 1", "teacher": "Gray", "percent": "", "mark": ""},
            {"period": 8, "course_name": "Spanish 1", "teacher": "Ruiz", "percent": "", "mark": ""},
        ],
        "assignments_by_class": [
            {"class_name": "4- US History- Fall", "period": 4, "assignments": [
                {"number": 3, "description": "DBQ Outline", "due_date": "09/11/2026", "points_earned": 13.0,
                 "points_possible": 15, "percentage": 86.7, "grading_complete": True},
            ]},
            {"class_name": "5- Algebra 2- Fall", "period": 5, "assignments": [
                {"number": 7, "description": "3.2 Practice", "due_date": "09/10/2026", "points_earned": None,
                 "points_possible": 20, "grading_complete": True, "aeries_missing": True, "status": "missing"},
                {"number": 6, "description": "3.1 Practice", "due_date": "09/08/2026", "points_earned": 18.0,
                 "points_possible": 20, "percentage": 90.0, "grading_complete": True},
            ]},
            {"class_name": "6- Biology- Fall", "period": 6, "assignments": [
                {"number": 1, "description": "Cell lab", "due_date": "09/12/2026", "points_earned": 28.0,
                 "points_possible": 30, "percentage": 93.3, "grading_complete": True},
            ]},
            {"class_name": "7- PE Course 1- Fall", "period": 7, "assignments": []},
            {"class_name": "8- Spanish 1- Fall", "period": 8, "assignments": []},
        ],
        "class_trends": {},
        "ai_summary": {"headline": "Old model one-liner that must not ship.", "wins": ["Biology A"], "classes": []},
    }


def view_for(student, export=None, history_context=None):
    if export is not None:
        scraper.attach_classroom_export(student, export, today=TODAY)
    p1, p2 = _patched()
    with p1, p2:
        return scraper.build_student_view(student, history_context=history_context)


class MemoShapeTests(unittest.TestCase):
    def setUp(self):
        export = json.loads((FIXTURES / "classroom_export_sample.json").read_text())
        self.student = kid_one()
        self.view = view_for(self.student, export)
        self.memo = self.view["memo"]

    def test_four_to_six_full_sentences(self):
        n = len(self.memo["sentences"])
        self.assertGreaterEqual(n, briefing.MIN_SENTENCES)
        self.assertLessEqual(n, briefing.MAX_SENTENCES)
        for s in self.memo["sentences"]:
            self.assertRegex(s, r"[.!?”]$")
            self.assertEqual(s[0], s[0].upper())
        self.assertEqual(self.memo["text"], " ".join(self.memo["sentences"]))
        self.assertEqual(self.memo["source"], "computed")
        self.assertEqual(self.memo["as_of"], "2026-09-14")

    def test_model_headline_is_gone(self):
        self.assertNotIn("headline", self.view)
        self.assertNotIn("Old model one-liner", self.memo["text"])

    def test_tonight_and_classroom_facts_lead(self):
        s = self.memo["sentences"]
        self.assertIn("Student A has Unit 3 Quiz Review (Algebra 2) due tomorrow.", s)
        handed_in = next(x for x in s if "turned in on Classroom" in x)
        self.assertIn("Section 3.2 Practice (Algebra 2)", handed_in)
        self.assertIn("Sep 10", handed_in)
        self.assertIn("not a to-do", handed_in)
        # Handed in on Classroom is never counted as missing.
        self.assertFalse(any("missing item" in x for x in s))

    def test_grades_scores_note_and_coverage(self):
        text = self.memo["text"]
        self.assertIn("Lowest grade is Algebra 2 at 81% (B-); Biology leads at 93% (A).", text)
        self.assertIn("Cell lab 28/30 in Biology", text)
        self.assertIn("DBQ Outline 13/15 in US History", text)
        self.assertIn("Teacher note in Algebra 2 (Sep 12): “Quiz Friday covers 3.1-3.3. Bring a calculator.”", text)
        self.assertIn("Two of five classes have no work posted in Aeries yet", text)

    def test_never_weekday_only_or_zero_percent_lies(self):
        text = self.memo["text"]
        self.assertNotRegex(text, r"\bdue (Mon|Tue|Wed|Thu|Fri)\b\.?(\s|$)")
        self.assertNotIn("at 0%", text)

    def test_student_name_only_as_display_label(self):
        # The payload name is the display label the page already shows; nothing else
        # from the fixture's per-student copy title leaks in.
        self.assertNotIn("Firstname", self.memo["text"])
        self.assertNotIn("Lastname", self.memo["text"])


class MemoFactTests(unittest.TestCase):
    def test_due_today_missing_and_low_score(self):
        student = {
            "name": "Student B",
            "sn": "2",
            "classes": [
                {"period": 3, "course_name": "Eng 9 Entre", "teacher": "McCarty", "percent": "76", "mark": "C"},
                {"period": 5, "course_name": "Bus of Gaming", "teacher": "Licciardo", "percent": "97", "mark": "A"},
            ],
            "assignments_by_class": [
                {"class_name": "3- Eng 9 Entre- Fall", "period": 3, "assignments": [
                    {"number": 2, "description": "Socratic Seminar Prep", "due_date": "09/14/2026",
                     "points_earned": None, "points_possible": 10, "grading_complete": False},
                    {"number": 1, "description": "Vocab Quiz 1", "due_date": "09/11/2026", "points_earned": 6.0,
                     "points_possible": 10, "percentage": 60.0, "grading_complete": True},
                    {"number": 4, "description": "Reading Log Week 2", "due_date": "09/04/2026",
                     "points_earned": None, "points_possible": 5, "grading_complete": True,
                     "aeries_missing": True, "status": "missing"},
                ]},
                {"class_name": "5- Bus of Gaming- Fall", "period": 5, "assignments": [
                    {"number": 1, "description": "Anatomy of a Great Game", "due_date": "09/10/2026",
                     "points_earned": 5.0, "points_possible": 5, "percentage": 100.0, "grading_complete": True},
                ]},
            ],
            "class_trends": {},
            "ai_summary": {},
        }
        s = view_for(student)["memo"]["sentences"]
        self.assertEqual(s[0], "Student B has Socratic Seminar Prep (Eng 9 Entre) due today.")
        self.assertEqual(s[1], "Aeries flags one missing item: Reading Log Week 2 (Eng 9 Entre) (5 points in all).")
        self.assertIn("One class is under 80%: Eng 9 Entre at 76% (C) with one missing; Bus of Gaming leads at 97% (A).", s)
        self.assertIn("Vocab Quiz 1 in Eng 9 Entre came back 6/10 (60%) on Sep 11.", s)
        self.assertLessEqual(len(s), 6)

    def test_quiet_day_pads_with_true_fillers_and_trend(self):
        student = {
            "name": "Student A",
            "sn": "1",
            "classes": [
                {"period": 1, "course_name": "Chemistry", "teacher": "Lee", "percent": "94", "mark": "A"},
                {"period": 3, "course_name": "Geometry", "teacher": "Kim", "percent": "0", "mark": ""},
            ],
            "assignments_by_class": [
                {"class_name": "1- Chemistry- Fall", "period": 1, "assignments": [
                    {"number": 2, "description": "Lab 2: Density", "due_date": "08/20/2026", "points_earned": 19.0,
                     "points_possible": 20, "percentage": 95.0, "grading_complete": True},
                ]},
                {"class_name": "3- Geometry- Fall", "period": 3, "assignments": [
                    {"number": 1, "description": "Syllabus Quiz", "due_date": "09/08/2026", "points_earned": None,
                     "points_possible": 5, "grading_complete": False},
                ]},
            ],
            "class_trends": {},
            "ai_summary": {},
        }
        hist = {"classes": {"Chemistry": {"delta_7d": 3.0, "trend_label": "improving"}}, "snapshot_count": 5}
        s = view_for(student, history_context=hist)["memo"]["sentences"]
        self.assertEqual(s[0], "Nothing is due today or tomorrow for Student A, and Aeries shows no missing work.")
        self.assertIn("Chemistry is the only graded class so far, at 94% (A).", s)
        self.assertIn("Geometry shows 0% in Aeries only because nothing has been graded yet; that is not a real grade.", s)
        self.assertIn("Chemistry is up 3 points from a week ago.", s)
        self.assertIn("One turned-in item is waiting for a teacher score; that is not missing work.", s)
        self.assertGreaterEqual(len(s), 4)

    def test_schedule_only_and_empty_term(self):
        sched = {"name": "Student A", "sn": "1", "classes": [
            {"period": 1, "course_name": "Chemistry", "teacher": "Lee", "percent": "", "mark": ""}],
            "assignments_by_class": [], "class_trends": {}, "ai_summary": {}}
        s = view_for(sched)["memo"]["sentences"]
        self.assertEqual(s, ["Schedules are up for Student A (1 class); no grades or work posted yet."])
        empty = {"name": "Student A", "sn": "1", "classes": [], "assignments_by_class": [], "class_trends": {}, "ai_summary": {}}
        s = view_for(empty)["memo"]["sentences"]
        self.assertEqual(s, ["The portal has no current classes or missing work for Student A."])

    def test_display_name_override_and_cap(self):
        analytics = {
            "coverage": {"scheduled_classes": 3, "classes_with_portal_work": 1},
            "tonight_plan": {"items": [
                {"name": "A", "class_name": "C1", "reason": "due_today"},
                {"name": "B", "class_name": "C1", "reason": "due_tomorrow"},
            ]},
            "classes": [{
                "course_name": "C1", "current_grade_pct": 72.0, "current_grade_mark": "C-",
                "missing_assignments": [{"name": "M1", "points_possible": 10}, {"name": "M2", "points_possible": 5},
                                        {"name": "M3", "points_possible": 5}, {"name": "M4", "points_possible": 5}],
                "recent_scores": [{"name": "R1", "due_date": "09/12/2026", "points_earned": 3, "points_possible": 10, "percentage": 30.0}],
                "awaiting_count": 2, "stale_awaiting_count": 1,
                "history": {"delta_7d": -4},
                "empty_high_weight_categories": [{"name": "Tests", "weight_pct": 60}],
            }],
        }
        memo = briefing.build_parent_memo(analytics, {"name": "Real Name"}, TODAY, display_name="Student Z")
        s = memo["sentences"]
        self.assertEqual(len(s), 6)
        self.assertTrue(s[0].startswith("Student Z has A (C1) due today."))
        self.assertIn("Aeries flags four missing items: M1 (C1), M2 (C1), and M3 (C1), and 1 more (25 points in all).", s)
        self.assertIn("Then B (C1) is due tomorrow.", s)
        self.assertNotIn("Real Name", memo["text"])
        # Reading order is fixed even though importance chose the set.
        order = [briefing._ORDER.index(k) for k in ("due_today", "missing", "due_tomorrow", "grades", "low_score")]
        self.assertEqual(order, sorted(order))


class RecapTests(unittest.TestCase):
    def test_snapshot_recap_uses_memo_not_headline(self):
        student = {"view": {"memo": {"text": "Memo text."}, "tonight_label": "X (C1)"}, "ai_summary": {"headline": "old"}}
        self.assertEqual(scraper._briefing_recap(student), {"memo": "Memo text.", "focus_tonight": "X (C1)"})
        self.assertIsNone(scraper._briefing_recap({}))


if __name__ == "__main__":
    unittest.main()
