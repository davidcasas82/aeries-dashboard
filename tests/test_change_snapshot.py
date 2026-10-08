import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scraper  # noqa: E402

TODAY = datetime(2026, 9, 25)
YESTERDAY = "2026-09-24"


def _student():
    return {
        "classes": [{
            "period": 1,
            "course_name": "Algebra",
            "percent": 90,
            "mark": "A",
        }],
        "assignments_by_class": [{
            "class_name": "1 Algebra",
            "period": 1,
            "assignments": [
                {
                    "description": "Worksheet",
                    "due_date": "09/24/2026",
                    "points_earned": None,
                    "aeries_missing": True,
                },
                {
                    "description": "Quiz",
                    "due_date": "09/20/2026",
                    "points_earned": None,
                    "date_completed": "9/20/2026",
                },
                {
                    "description": "Poster",
                    "due_date": "09/18/2026",
                    "points_earned": None,
                },
                {
                    "description": "Lab",
                    "due_date": "09/24/2026",
                    "points_earned": None,
                },
                {
                    "description": "Warmup",
                    "due_date": "09/22/2026",
                    "points_earned": None,
                    "score_raw": "NA",
                },
            ],
        }],
    }


def _yesterday():
    return {
        "date": YESTERDAY,
        "classes": {
            "Algebra": {
                "pct": 86,
                "mark": "B",
                "items": [
                    {"title": "Worksheet", "due": "2026-09-24", "state": "open"},
                    {"title": "Quiz", "due": "2026-09-20", "state": "open"},
                    {"title": "Poster", "due": "2026-09-18", "state": "open"},
                    {"title": "Lab", "due": "2026-09-24", "state": "open"},
                    {"title": "Warmup", "due": "2026-09-22", "state": "turned_in"},
                ],
            }
        },
    }


class ChangeSnapshotTests(unittest.TestCase):
    def test_evening_snapshot_records_item_states(self):
        with mock.patch.object(scraper, "pacific_today", return_value=TODAY.date()), \
                mock.patch.object(scraper, "pacific_today_dt", return_value=TODAY):
            snap = scraper.build_student_snapshot(
                _student(), [], captured_at=TODAY, include_items=True,
            )
        items = {row["title"]: row for row in snap["classes"]["Algebra"]["items"]}
        self.assertEqual(items["Worksheet"]["state"], "missing")
        self.assertEqual(items["Quiz"]["state"], "turned_in")
        self.assertEqual(items["Poster"]["state"], "open")
        self.assertEqual(items["Warmup"]["state"], "marked")
        self.assertEqual(items["Warmup"]["mark_code"], "NA")
        self.assertEqual(snap["classes"]["Algebra"]["pct"], 90)

    def test_diff_events_from_yesterday(self):
        with mock.patch.object(scraper, "pacific_today", return_value=TODAY.date()), \
                mock.patch.object(scraper, "pacific_today_dt", return_value=TODAY):
            snap = scraper.build_student_snapshot(
                _student(), [], captured_at=TODAY, include_items=True,
                prior_items={"Algebra": {"worksheet", "quiz", "poster", "lab", "warmup"}},
            )
        events = scraper.diff_change_events(snap, _yesterday(), TODAY.date())
        kinds = {(event["type"], event.get("title") or event.get("class_name")) for event in events}
        self.assertIn(("turned_in", "Quiz"), kinds)
        self.assertIn(("newly_missing", "Worksheet"), kinds)
        self.assertIn(("lapsed", "Lab"), kinds)
        self.assertIn(("marked", "Warmup"), kinds)
        self.assertIn(("pct_move", "Algebra"), kinds)
        self.assertNotIn(("lapsed", "Poster"), kinds)

    def test_streak_stays_silent_until_two_weeks_of_item_snapshots(self):
        snaps = []
        for offset in range(6):
            day = datetime(2026, 9, 20 + offset).date().isoformat()
            snaps.append({
                "date": day,
                "classes": {
                    "Algebra": {
                        "items": [{"title": "Quiz", "due": "2026-09-20", "state": "turned_in"}],
                    }
                },
            })
        self.assertEqual(scraper.ungraded_streak_events(snaps, datetime(2026, 9, 25).date()), [])

    def test_streak_after_two_weeks_turned_in_with_no_score(self):
        snaps = []
        for offset in range(15):
            day = datetime(2026, 9, 11 + offset).date()
            snaps.append({
                "date": day.isoformat(),
                "classes": {
                    "Algebra": {
                        "pct": 90,
                        "items": [{"title": "Quiz", "due": "2026-09-11", "state": "turned_in"}],
                    }
                },
            })
        events = scraper.ungraded_streak_events(snaps, datetime(2026, 9, 25).date())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "ungraded_streak")
        self.assertGreaterEqual(events[0]["days"], scraper.UNGRADED_STREAK_WEEKDAYS)

    def test_older_days_drop_the_assignment_list(self):
        history = {"students": {}}
        old = {
            "date": "2026-09-01",
            "classes": {"Algebra": {"pct": 80, "mark": "B", "items": [{"title": "Quiz", "state": "open"}]}},
        }
        recent = {
            "date": "2026-09-25",
            "classes": {"Algebra": {"pct": 90, "mark": "A", "items": [{"title": "Quiz", "state": "turned_in"}]}},
        }
        scraper.upsert_student_snapshot(history, "1", "", old)
        scraper.upsert_student_snapshot(history, "1", "", recent)
        snaps = history["students"]["1"]["snapshots"]
        self.assertNotIn("items", snaps[0]["classes"]["Algebra"])
        self.assertIn("items", snaps[1]["classes"]["Algebra"])
        self.assertEqual(snaps[0]["classes"]["Algebra"]["pct"], 80)

    def test_earlier_scrape_keeps_the_evening_note(self):
        history = {"students": {}}
        evening = {
            "date": "2026-09-25",
            "change_note_date": "2026-09-25",
            "since_yesterday": "Quiz in Algebra was turned in.",
            "change_events": [{"type": "turned_in", "title": "Quiz", "class_name": "Algebra"}],
            "classes": {
                "Algebra": {
                    "pct": 90,
                    "mark": "A",
                    "items": [{"title": "Quiz", "due": "2026-09-20", "state": "turned_in"}],
                }
            },
        }
        scraper.upsert_student_snapshot(history, "1", "", evening)
        afternoon = {
            "date": "2026-09-25",
            "classes": {"Algebra": {"pct": 91, "mark": "A", "missing_count": 0, "missing_names": []}},
        }
        scraper.upsert_student_snapshot(history, "1", "", afternoon, replace_items=False)
        saved = history["students"]["1"]["snapshots"][0]
        self.assertEqual(saved["classes"]["Algebra"]["pct"], 91)
        self.assertEqual(saved["classes"]["Algebra"]["items"][0]["state"], "turned_in")
        self.assertEqual(saved["since_yesterday"], "Quiz in Algebra was turned in.")
        self.assertEqual(scraper.latest_since_yesterday(history, "1"), "Quiz in Algebra was turned in.")

    def test_phrase_uses_only_the_events(self):
        text = scraper.phrase_since_yesterday([
            {"type": "turned_in", "title": "Quiz", "class_name": "Algebra"},
            {"type": "pct_move", "class_name": "Algebra", "delta": -3.5, "pct": 86},
        ])
        self.assertIn("Quiz in Algebra was turned in", text)
        self.assertIn("Algebra is down 3.5 points", text)
        self.assertNotIn("because", text.lower())

    def test_evening_window_is_after_the_afternoon_scrape(self):
        self.assertFalse(scraper.is_evening_snapshot(datetime(2026, 9, 25, 16, 45)))
        self.assertTrue(scraper.is_evening_snapshot(datetime(2026, 9, 25, 20, 7)))
        self.assertTrue(scraper.is_evening_snapshot(datetime(2026, 11, 5, 19, 7)))


if __name__ == "__main__":
    unittest.main()
