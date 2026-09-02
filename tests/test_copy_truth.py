"""Due-label, phantom-0, and stale-copy rewrite tests."""

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scraper  # noqa: E402


TODAY = datetime(2026, 8, 27)


def _asgn(**kwargs):
    row = {
        "description": kwargs.get("description", "Work"),
        "due_date": kwargs.get("due_date", "08/19/2026"),
        "points_earned": kwargs.get("points_earned"),
        "points_possible": kwargs.get("points_possible"),
        "percentage": kwargs.get("percentage"),
        "score_raw": kwargs.get("score_raw", ""),
    }
    row.update({k: v for k, v in kwargs.items() if k not in row})
    return row


class DueLabelTests(unittest.TestCase):
    def test_today_ungraded(self):
        a = _asgn(due_date="08/27/2026")
        self.assertEqual(scraper.assignment_due_state(a, TODAY, kind="upcoming"), "today")
        self.assertEqual(scraper.assignment_due_label(a, TODAY, kind="upcoming"), "due today")

    def test_future_weekday_and_date(self):
        a = _asgn(due_date="09/09/2026")
        self.assertEqual(scraper.assignment_due_state(a, TODAY, kind="upcoming"), "upcoming")
        self.assertEqual(
            scraper.assignment_due_label(a, TODAY, kind="upcoming"),
            "due Wed Sep 9",
        )
        self.assertNotRegex(scraper.assignment_due_label(a, TODAY, kind="upcoming"), r"^due \w+$")

    def test_past_scored_uses_score_not_due(self):
        a = _asgn(
            description="Anatomy of a Great Game",
            due_date="08/19/2026",
            points_earned=5.0,
            points_possible=5.0,
            percentage=100.0,
            score_raw="5 / 5",
        )
        self.assertEqual(scraper.assignment_due_state(a, TODAY, kind="recent"), "completed")
        self.assertEqual(scraper.assignment_due_label(a, TODAY, kind="recent"), "5/5")
        self.assertNotIn("due", scraper.assignment_due_label(a, TODAY, kind="recent"))

    def test_past_missing_overdue_with_date(self):
        a = _asgn(due_date="08/19/2026")
        self.assertEqual(scraper.assignment_due_state(a, TODAY, kind="missing"), "overdue")
        self.assertEqual(
            scraper.assignment_due_label(a, TODAY, kind="missing", is_missing=True),
            "overdue Aug 19",
        )

    def test_past_pending_awaiting_score(self):
        a = _asgn(due_date="08/19/2026")
        self.assertEqual(scraper.assignment_due_state(a, TODAY, kind="pending"), "pending")
        self.assertEqual(
            scraper.assignment_due_label(a, TODAY, kind="pending"),
            "awaiting score · was due Wed Aug 19",
        )

    def test_format_assignment_entry_includes_due_label_and_state(self):
        a = _asgn(
            description="Anatomy of a Great Game",
            due_date="08/19/2026",
            points_earned=5.0,
            points_possible=5.0,
        )
        entry = scraper.format_assignment_entry(a, TODAY, "recent")
        self.assertEqual(entry["due_label"], "5/5")
        self.assertEqual(entry["due_state"], "completed")


class PhantomZeroTests(unittest.TestCase):
    def test_scored_work_does_not_make_placeholder_zero_real(self):
        class_meta = {"percent": "0.0", "mark": "", "missing_count": 0}
        assignments = [_asgn(points_earned=1.0, points_possible=3.0, percentage=33.33)]
        self.assertFalse(scraper.class_has_real_grade(class_meta, assignments))
        self.assertFalse(scraper.class_has_real_grade(class_meta))
        self.assertTrue(scraper.is_phantom_zero_grade(class_meta, assignments))

    def test_letter_mark_is_real_even_at_zero(self):
        class_meta = {"percent": "0.0", "mark": "F", "missing_count": 0}
        self.assertTrue(scraper.class_has_real_grade(class_meta, []))
        self.assertFalse(scraper.is_phantom_zero_grade(class_meta, []))

    def test_positive_percent_is_real(self):
        class_meta = {"percent": "95.0", "mark": "A"}
        self.assertTrue(scraper.class_has_real_grade(class_meta, []))
        self.assertFalse(scraper.is_phantom_zero_grade(class_meta, []))

    def test_analyze_class_omits_phantom_zero_as_current_grade(self):
        class_meta = {
            "period": 3,
            "course_name": "Rapid Prototype",
            "percent": "0.0",
            "mark": "",
            "missing_count": 0,
        }
        assignments = [_asgn(
            description="Article: Design Principle Jigsaw",
            due_date="08/20/2026",
            points_earned=1.0,
            points_possible=3.0,
            percentage=33.33,
        )]
        analyzed = scraper.analyze_class(class_meta, assignments, TODAY)
        self.assertTrue(analyzed["phantom_zero"])
        self.assertIsNone(analyzed["current_grade_pct"])
        self.assertFalse(analyzed["posted_vs_counted"]["official_is_posted"])

    def test_posted_percent_not_overwritten_when_real(self):
        class_meta = {
            "period": 1,
            "course_name": "Mkt Adv GrphDes",
            "percent": "95.0",
            "mark": "A",
            "missing_count": 0,
        }
        assignments = [_asgn(
            due_date="08/20/2026",
            points_earned=5.0,
            points_possible=5.0,
            percentage=100.0,
        )]
        analyzed = scraper.analyze_class(class_meta, assignments, TODAY)
        self.assertFalse(analyzed["phantom_zero"])
        self.assertEqual(analyzed["current_grade_pct"], 95.0)
        self.assertEqual(analyzed["current_grade_mark"], "A")


class StaleCopyRewriteTests(unittest.TestCase):
    def test_scored_aug19_not_due_weekday(self):
        a = _asgn(
            description="Anatomy of a Great Game",
            due_date="08/19/2026",
            points_earned=5.0,
            points_possible=5.0,
        )
        entry = scraper.format_assignment_entry(a, TODAY, "recent")
        snap = "Anatomy of a Great Game · due Wed"
        rewritten = scraper.rewrite_stale_due_copy(snap, [entry])
        self.assertNotIn("due Wed", rewritten)
        self.assertIn("5/5", rewritten)

    def test_weekday_only_without_name_uses_completed_fallback(self):
        a = _asgn(
            description="0.1 Simplify Expressions",
            due_date="08/20/2026",
            points_earned=8.0,
            points_possible=10.0,
        )
        entry = scraper.format_assignment_entry(a, TODAY, "recent")
        snap = "Algebra Boot Camp assignments · due Thu"
        rewritten = scraper.rewrite_stale_due_copy(snap, [entry])
        self.assertNotRegex(rewritten, r"\bdue Thu\b")
        self.assertIn("8/10", rewritten)

    def test_future_due_label_kept(self):
        a = _asgn(description="LT 1.1 - CER # 1", due_date="09/09/2026")
        entry = scraper.format_assignment_entry(a, TODAY, "upcoming")
        snap = "LT 1.1 - CER # 1 · due Wed Sep 9"
        self.assertEqual(scraper.rewrite_stale_due_copy(snap, [entry]), snap)

    def test_phantom_zero_headline(self):
        text = "Engineering Geo at 0% needs attention; Leah holds A's"
        out = scraper.rewrite_phantom_zero_copy(text)
        self.assertNotIn("at 0%", out)
        self.assertNotIn("0% needs attention", out)
        self.assertIn("no grade yet", out.lower())

    def test_sanitize_briefing_kills_live_lies(self):
        analytics = {
            "classes": [
                {
                    "course_name": "Rapid Prototype",
                    "phantom_zero": True,
                    "recent_scores": [
                        scraper.format_assignment_entry(
                            _asgn(
                                description="Article: Design Principle Jigsaw",
                                due_date="08/20/2026",
                                points_earned=1.0,
                                points_possible=3.0,
                            ),
                            TODAY,
                            "recent",
                        )
                    ],
                    "upcoming": [],
                    "missing_assignments": [],
                    "pending_grade": [],
                },
                {
                    "course_name": "Bus of Gaming",
                    "phantom_zero": False,
                    "recent_scores": [
                        scraper.format_assignment_entry(
                            _asgn(
                                description="Anatomy of a Great Game",
                                due_date="08/19/2026",
                                points_earned=5.0,
                                points_possible=5.0,
                            ),
                            TODAY,
                            "recent",
                        )
                    ],
                    "upcoming": [],
                    "missing_assignments": [],
                    "pending_grade": [],
                },
            ]
        }
        summary = {
            "headline": "Daniel needs attention in Rapid Prototype at 0% while holding an A in Bus of Gaming",
            "wins": [],
            "classes": [
                {
                    "class_name": "Rapid Prototype",
                    "snap": "LT 1.1 - Poster of Myself in Adobe Illustrator · due Fri",
                    "story": "Grade at 0% after 33% on Article: Design Principle Jigsaw.",
                },
                {
                    "class_name": "Bus of Gaming",
                    "snap": "Anatomy of a Great Game · due Wed",
                    "story": "",
                },
            ],
        }
        out = scraper.sanitize_briefing_copy(summary, analytics)
        self.assertNotIn("at 0%", out["headline"])
        self.assertNotIn("due Wed", out["classes"][1]["snap"])
        self.assertIn("5/5", out["classes"][1]["snap"])
        self.assertNotIn("0%", out["classes"][0]["story"])


class FirstGradeDeltaTests(unittest.TestCase):
    def test_phantom_history_point(self):
        self.assertTrue(scraper._is_phantom_history_point({"pct": 0.0, "mark": ""}))
        self.assertFalse(scraper._is_phantom_history_point({"pct": 95.0, "mark": "A"}))
        self.assertFalse(scraper._is_phantom_history_point({"pct": 0.0, "mark": "F"}))

    def test_pct_delta_skips_phantom_baseline(self):
        self.assertIsNone(scraper._pct_delta(95.0, {"pct": 0.0, "mark": ""}))
        self.assertEqual(scraper._pct_delta(95.0, {"pct": 90.0, "mark": "A"}), 5.0)


class EmptyHighWeightTests(unittest.TestCase):
    def test_extracts_empty_70_and_zero_weight(self):
        insight = {
            "categories": [
                {"name": "Assessments", "weight_pct": 70, "empty": True, "reason": "empty_0_0"},
                {"name": "Daily Assignments", "weight_pct": 0, "empty": False, "reason": "zero_weight"},
                {"name": "Assignments", "weight_pct": 20, "empty": False, "reason": None},
            ]
        }
        empty_high, zero_weight = scraper.extract_grade_mix_flags(insight)
        self.assertEqual(empty_high[0]["name"], "Assessments")
        self.assertEqual(empty_high[0]["weight_pct"], 70)
        self.assertEqual(zero_weight[0]["name"], "Daily Assignments")


class ClassworkIsNotHomeworkTests(unittest.TestCase):
    """Most work happens in class. Missing = Aeries flag only; turned-in is never a to-do."""

    def _exit_ticket(self, **extra):
        # Blank score, teacher ticked Grading Completed, Aeries stamped Date Completed.
        return _asgn(
            description="Exit Ticket",
            due_date="08/19/2026",
            points_possible=4.0,
            score_raw="/ 4",
            grading_complete=True,
            date_completed="08/19/2026",
            category="Formative",
            **extra,
        )

    def test_grading_complete_blank_is_not_missing_when_aeries_says_zero(self):
        class_meta = {"percent": "100.0", "mark": "A", "missing_count": 0}
        self.assertEqual(
            scraper.select_missing_assignments([self._exit_ticket()], class_meta, TODAY),
            [],
        )
        self.assertFalse(scraper.is_missing_assignment(self._exit_ticket(), TODAY, class_meta))

    def test_aeries_count_picks_not_turned_in_first(self):
        turned_in = self._exit_ticket()
        never_handed_in = _asgn(
            description="Homework 3",
            due_date="08/24/2026",
            points_possible=10.0,
            grading_complete=False,
        )
        class_meta = {"percent": "88.0", "mark": "B", "missing_count": 1}
        picked = scraper.select_missing_assignments([turned_in, never_handed_in], class_meta, TODAY)
        self.assertEqual([a["description"] for a in picked], ["Homework 3"])

    def test_blank_score_status_is_awaiting_not_counts(self):
        a = scraper.annotate_assignment_status(self._exit_ticket(), {"Formative": 30})
        self.assertEqual(a["status"], "turned_in")
        self.assertFalse(a["counts_toward_grade"])
        entry = scraper.format_assignment_entry(a, TODAY, "pending")
        self.assertTrue(entry.get("turned_in"))
        self.assertEqual(entry["due_label"], "awaiting score · was due Wed Aug 19")

    def test_tonight_plan_skips_turned_in_and_adds_due_tomorrow(self):
        analytics = [
            {
                "course_name": "Physics & Eng",
                "upcoming": [
                    {"name": "Warm Ups", "days_until_due": 0, "turned_in": True, "due_date": "08/27/2026"},
                    {"name": "Lab Report", "days_until_due": 1, "due_date": "08/28/2026"},
                ],
                "missing_assignments": [
                    {"name": "Exit Ticket", "turned_in": True, "due_date": "08/19/2026"},
                ],
            },
            {
                "course_name": "English",
                "upcoming": [
                    {"name": "Essay draft", "days_until_due": 0, "due_date": "08/27/2026"},
                ],
                "missing_assignments": [],
            },
        ]
        plan = scraper.build_tonight_plan(analytics)
        names = [(i["name"], i["reason"]) for i in plan["items"]]
        self.assertEqual(names, [("Essay draft", "due_today"), ("Lab Report", "due_tomorrow")])
        self.assertNotIn("Exit Ticket", plan["label"])
        self.assertNotIn("Warm Ups", plan["label"])


class ModelAndPromptTests(unittest.TestCase):
    def test_not_mini(self):
        self.assertEqual(scraper.GROK_MODEL, "grok-4")
        self.assertNotIn("mini", scraper.GROK_MODEL)

    def test_prompt_drops_due_tue_example(self):
        src = Path(scraper.__file__).read_text()
        self.assertNotIn("Anatomy of a Great Game · due Tue", src)
        self.assertNotIn('Use due_weekday / days_until_due for "due Tue"', src)
        self.assertIn("due_label", src)
        self.assertNotIn("/teacher/", src)


if __name__ == "__main__":
    unittest.main()
