"""Calendar, sanity, view-model, and timeout helpers."""

import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scraper  # noqa: E402


CALENDAR = {
    "years": [
        {"id": "2026-27", "first_day": "2026-08-13", "last_day": "2027-05-28"},
    ],
    "term_cutovers": {
        "q1_last_md": "10-12",
        "fall_last_md": "01-04",
        "q3_last_md": "03-15",
    },
}


class StudentLogLabelTests(unittest.TestCase):
    def test_never_includes_name_or_sn(self):
        label = scraper.student_log_label(1, {"name": "Daniel", "sn": "1087"})
        self.assertEqual(label, "student 1")
        self.assertNotIn("Daniel", label)
        self.assertNotIn("1087", label)

    def test_redact_probe_text(self):
        out = scraper.redact_probe_text(
            "Hello Daniel SN 1087 parent@example.com",
            {"name": "Daniel", "sn": "1087"},
        )
        self.assertNotIn("Daniel", out)
        self.assertNotIn("1087", out)
        self.assertIn("[redacted]", out)


class TimeoutSessionTests(unittest.TestCase):
    def test_sets_default_timeout(self):
        session = scraper.TimeoutSession(timeout=12)

        def fake_request(self, method, url, **kwargs):
            return kwargs.get("timeout")

        with patch.object(scraper.requests.Session, "request", fake_request):
            self.assertEqual(session.request("GET", "https://example.com"), 12)
            self.assertEqual(
                session.request("GET", "https://example.com", timeout=3),
                3,
            )


class PreferredTermsTests(unittest.TestCase):
    def test_uses_passed_today_not_utc_default(self):
        terms = scraper.preferred_gradebook_terms(
            today=date(2026, 10, 12),
            calendar=CALENDAR,
        )
        self.assertIn("q1", terms)
        later = scraper.preferred_gradebook_terms(
            today=date(2026, 10, 13),
            calendar=CALENDAR,
        )
        self.assertIn("q2", later)
        self.assertNotIn("q1", later)

    def test_fall_cutover_in_january(self):
        still_fall = scraper.preferred_gradebook_terms(
            today=date(2027, 1, 4),
            calendar=CALENDAR,
        )
        self.assertIn("fall", still_fall)
        spring = scraper.preferred_gradebook_terms(
            today=date(2027, 1, 5),
            calendar=CALENDAR,
        )
        self.assertIn("spring", spring)

    def test_calendar_cutover_override(self):
        cal = {
            **CALENDAR,
            "term_cutovers": {"q1_last_md": "09-01", "fall_last_md": "01-04", "q3_last_md": "03-15"},
        }
        terms = scraper.preferred_gradebook_terms(today=date(2026, 9, 2), calendar=cal)
        self.assertIn("q2", terms)


class AttendanceYearTests(unittest.TestCase):
    def test_active_session_uses_calendar_year(self):
        label = scraper.target_attendance_year_label(
            today=date(2026, 9, 1),
            calendar=CALENDAR,
        )
        self.assertEqual(label, "2026-2027")

    def test_mid_summer_uses_completed_year(self):
        label = scraper.target_attendance_year_label(
            today=date(2027, 6, 15),
            calendar=CALENDAR,
        )
        self.assertEqual(label, "2026-2027")


class UrgencyTests(unittest.TestCase):
    def test_c_range_without_missing_is_critical(self):
        urgency, issue = scraper.infer_urgency_from_analytics(
            75, 0, None, "low_performance"
        )
        self.assertEqual(urgency, "critical")
        self.assertEqual(issue, "low_grade")

    def test_b_with_missing_is_watch(self):
        urgency, _ = scraper.infer_urgency_from_analytics(88, 1, None, "completion_gap")
        self.assertEqual(urgency, "watch")


class PortalSanityTests(unittest.TestCase):
    def test_empty_class_list_after_prior_classes(self):
        prior = {"classes": [{"course_name": "Bio"}], "assignments_by_class": []}
        now = {"classes": [], "assignments_by_class": []}
        self.assertTrue(scraper.portal_looks_incomplete(now, prior))

    def test_assignment_drop_over_half(self):
        prior = {
            "classes": [{"course_name": "Bio"}],
            "assignments_by_class": [{"assignments": [{}] * 12}],
        }
        now = {
            "classes": [{"course_name": "Bio"}],
            "assignments_by_class": [{"assignments": [{}] * 3}],
        }
        reason = scraper.portal_looks_incomplete(now, prior)
        self.assertIn("assignment count dropped", reason)

    def test_ok_when_stable(self):
        prior = {
            "classes": [{"course_name": "Bio"}],
            "assignments_by_class": [{"assignments": [{}] * 12}],
        }
        now = {
            "classes": [{"course_name": "Bio"}],
            "assignments_by_class": [{"assignments": [{}] * 11}],
        }
        self.assertIsNone(scraper.portal_looks_incomplete(now, prior))

    def test_skipped_outside_session(self):
        prior = {"classes": [{"course_name": "Bio"}]}
        now = {"classes": []}
        self.assertIsNone(scraper.portal_looks_incomplete(now, prior, in_session=False))


class ViewModelTests(unittest.TestCase):
    def test_tonight_prefers_due_today(self):
        today = datetime(2026, 9, 1)
        due = today.strftime("%m/%d/%Y")
        yesterday = datetime(2026, 8, 31).strftime("%m/%d/%Y")
        student = {
            "name": "Kid",
            "sn": "1",
            "classes": [
                {
                    "period": 1,
                    "course_name": "Bio",
                    "teacher": "Lee",
                    "percent": "92",
                    "mark": "A",
                    "missing_count": 0,
                }
            ],
            "assignments_by_class": [
                {
                    "class_name": "1- Bio- Fall",
                    "period": 1,
                    "assignments": [
                        {
                            "description": "Lab writeup",
                            "due_date": due,
                            "points_earned": None,
                            "points_possible": 10,
                            "grading_complete": False,
                        },
                        {
                            "description": "Old missing",
                            "due_date": yesterday,
                            "points_earned": None,
                            "points_possible": 5,
                            "grading_complete": True,
                        },
                    ],
                }
            ],
            "class_trends": {},
            "ai_summary": {},
        }
        with patch.object(scraper, "pacific_today_dt", return_value=today):
            with patch.object(scraper, "pacific_today", return_value=today.date()):
                view = scraper.build_student_view(student)
        self.assertTrue(view["tonight"])
        self.assertEqual(view["tonight"][0]["name"], "Lab writeup")
        self.assertEqual(view["classes"][0]["urgency"], "watch")
        self.assertEqual(view["classes"][0]["do_tonight"], "Lab writeup")
        # Aeries says 0 missing, so the old grading-complete blank is pending, not missing
        self.assertEqual(view["classes"][0]["missing_count"], 0)

    def test_in_class_work_done_today_is_not_a_to_do(self):
        today = datetime(2026, 9, 1)
        due = today.strftime("%m/%d/%Y")
        student = {
            "name": "Kid",
            "sn": "1",
            "classes": [
                {
                    "period": 6,
                    "course_name": "Physics & Eng",
                    "teacher": "Chung",
                    "percent": "100",
                    "mark": "A",
                    "missing_count": 0,
                }
            ],
            "assignments_by_class": [
                {
                    "class_name": "6- Physics & Eng- Fall",
                    "period": 6,
                    "assignments": [
                        {
                            "description": "Unit 0 Warm Ups",
                            "due_date": due,
                            "date_completed": due,
                            "points_earned": None,
                            "points_possible": 3,
                            "grading_complete": True,
                        },
                    ],
                }
            ],
            "class_trends": {},
            "ai_summary": {},
        }
        with patch.object(scraper, "pacific_today_dt", return_value=today):
            with patch.object(scraper, "pacific_today", return_value=today.date()):
                view = scraper.build_student_view(student)
        self.assertEqual(view["tonight"], [])
        self.assertEqual(view["classes"][0]["do_tonight"], "")
        self.assertEqual(view["classes"][0]["missing_count"], 0)
        self.assertEqual(view["classes"][0]["urgency"], "strong")


if __name__ == "__main__":
    unittest.main()
