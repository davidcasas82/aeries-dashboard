"""Calendar, sanity, view-model, and timeout helpers."""

import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

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


def _ok_login_response(url="https://tustinusd.aeries.net/student/Dashboard.aspx"):
    resp = MagicMock()
    resp.status_code = 200
    resp.url = url
    resp.text = "ok"
    return resp


class LoginTimeoutRetryTests(unittest.TestCase):
    """login() retries portal timeouts; does not retry bad credentials."""

    def _patch_session(self, session):
        return patch.object(scraper, "TimeoutSession", return_value=session)

    def test_retries_read_timeout_then_succeeds(self):
        session = MagicMock()
        ok = _ok_login_response()
        session.get.side_effect = [requests.exceptions.ReadTimeout("timed out"), ok]
        session.post.return_value = ok

        with self._patch_session(session), patch.object(scraper.time, "sleep") as sleep:
            result = scraper.login()

        self.assertIs(result, session)
        self.assertEqual(session.get.call_count, 2)
        sleep.assert_called_once_with(scraper.LOGIN_RETRY_DELAY_SEC)

    def test_retries_connect_timeout_then_succeeds(self):
        session = MagicMock()
        ok = _ok_login_response()
        session.get.side_effect = [requests.exceptions.ConnectTimeout("connect timed out"), ok]
        session.post.return_value = ok

        with self._patch_session(session), patch.object(scraper.time, "sleep") as sleep:
            result = scraper.login()

        self.assertIs(result, session)
        self.assertEqual(session.get.call_count, 2)
        sleep.assert_called_once()

    def test_retries_post_read_timeout_then_succeeds(self):
        session = MagicMock()
        ok = _ok_login_response()
        session.get.return_value = ok
        session.post.side_effect = [requests.exceptions.ReadTimeout("timed out"), ok]

        with self._patch_session(session), patch.object(scraper.time, "sleep"):
            result = scraper.login()

        self.assertIs(result, session)
        self.assertEqual(session.post.call_count, 2)

    def test_raises_after_three_read_timeouts(self):
        session = MagicMock()
        session.get.side_effect = requests.exceptions.ReadTimeout("timed out")

        with self._patch_session(session), patch.object(scraper.time, "sleep") as sleep:
            with self.assertRaises(requests.exceptions.ReadTimeout):
                scraper.login()

        self.assertEqual(session.get.call_count, scraper.LOGIN_TIMEOUT_ATTEMPTS)
        self.assertEqual(sleep.call_count, scraper.LOGIN_TIMEOUT_ATTEMPTS - 1)
        self.assertEqual(session.post.call_count, 0)

    def test_does_not_retry_login_failed_html(self):
        session = MagicMock()
        login_url = f"{scraper.BASE_URL}/student/LoginParent.aspx"
        session.get.return_value = _ok_login_response(login_url)
        failed = _ok_login_response(login_url)
        failed.text = '<div class="error">Invalid email or password</div>'
        session.post.return_value = failed

        with self._patch_session(session), patch.object(scraper.time, "sleep") as sleep:
            with self.assertRaises(scraper.ScrapeError) as ctx:
                scraper.login()

        self.assertIn("Login failed", str(ctx.exception))
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(session.post.call_count, 1)
        sleep.assert_not_called()


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

    def test_view_splits_awaiting_from_missing_and_flags_newly_missing(self):
        today = datetime(2026, 9, 1)
        fmt = lambda d: d.strftime("%m/%d/%Y")  # noqa: E731
        student = {
            "name": "Kid",
            "sn": "1",
            "classes": [
                {
                    "period": 3,
                    "course_name": "Rapid Prototype",
                    "teacher": "Ireland",
                    "percent": "90",
                    "mark": "A",
                    "missing_count": 1,
                }
            ],
            "assignments_by_class": [
                {
                    "class_name": "3- Rapid Prototype- Fall",
                    "period": 3,
                    "assignments": [
                        {
                            # Never handed in; the portal's 1 missing lands here
                            "description": "Homework 2",
                            "due_date": fmt(datetime(2026, 8, 28)),
                            "points_earned": None,
                            "points_possible": 10,
                            "grading_complete": False,
                        },
                        {
                            # Handed in 15 days ago, still unscored: awaiting, and stale
                            "description": "Poster",
                            "due_date": fmt(datetime(2026, 8, 17)),
                            "date_completed": fmt(datetime(2026, 8, 17)),
                            "points_earned": None,
                            "points_possible": 10,
                            "grading_complete": True,
                        },
                        {
                            # Blank from a few days ago: awaiting, not stale
                            "description": "Exit Ticket",
                            "due_date": fmt(datetime(2026, 8, 27)),
                            "date_completed": fmt(datetime(2026, 8, 27)),
                            "points_earned": None,
                            "points_possible": 4,
                            "grading_complete": True,
                        },
                    ],
                }
            ],
            "class_trends": {},
            "ai_summary": {},
        }
        history = {
            "students": {
                "1": {
                    "name": "Kid",
                    "snapshots": [
                        {
                            "date": "2026-08-31",
                            "classes": {
                                "Rapid Prototype": {
                                    "period": 3,
                                    "pct": 90.0,
                                    "mark": "A",
                                    "missing_count": 0,
                                    "missing_names": [],
                                }
                            },
                        }
                    ],
                }
            }
        }
        with patch.object(scraper, "pacific_today_dt", return_value=today):
            with patch.object(scraper, "pacific_today", return_value=today.date()):
                analytics = scraper.build_class_analytics(student)
                ctx = scraper.build_history_context("1", student, analytics["classes"], history)
                view = scraper.build_student_view(student, history_context=ctx)
        row = view["classes"][0]
        self.assertEqual([a["name"] for a in row["missing"]], ["Homework 2"])
        self.assertEqual(row["missing_count"], 1)
        self.assertEqual([a["name"] for a in row["awaiting"]], ["Poster", "Exit Ticket"])
        self.assertEqual(row["awaiting_count"], 2)
        self.assertEqual(row["stale_awaiting_count"], 1)
        poster, ticket = row["awaiting"]
        self.assertTrue(poster["stale"])
        self.assertEqual(poster["days_past_due"], 15)
        self.assertTrue(poster["turned_in"])
        self.assertNotIn("stale", ticket)
        self.assertEqual(ctx["classes"]["Rapid Prototype"]["newly_missing"], ["Homework 2"])
        self.assertEqual(row["newly_missing"], ["Homework 2"])

    def test_newly_missing_is_empty_without_a_prior_snapshot(self):
        analytics = [
            {"course_name": "Bio", "current_grade_pct": 90.0, "missing_assignments": [{"name": "Lab"}]}
        ]
        with patch.object(scraper, "pacific_today", return_value=date(2026, 9, 1)):
            ctx = scraper.build_history_context("1", {"name": "Kid"}, analytics, {"students": {}})
        self.assertEqual(ctx["classes"]["Bio"]["newly_missing"], [])
        # Already missing in the last snapshot: chronic, not new
        history = {"students": {"1": {"snapshots": [
            {"date": "2026-08-31", "classes": {"Bio": {"pct": 90.0, "mark": "A", "missing_count": 1, "missing_names": ["Lab"]}}}
        ]}}}
        with patch.object(scraper, "pacific_today", return_value=date(2026, 9, 1)):
            ctx = scraper.build_history_context("1", {"name": "Kid"}, analytics, history)
        self.assertEqual(ctx["classes"]["Bio"]["newly_missing"], [])
        self.assertEqual(ctx["classes"]["Bio"]["chronic_missing"], ["Lab"])


if __name__ == "__main__":
    unittest.main()
