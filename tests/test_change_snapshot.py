import json
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

    def test_percent_only_prior_does_not_dump_lapses(self):
        with mock.patch.object(scraper, "pacific_today", return_value=TODAY.date()), \
                mock.patch.object(scraper, "pacific_today_dt", return_value=TODAY):
            snap = scraper.build_student_snapshot(
                _student(), [], captured_at=TODAY, include_items=True,
                prior_items={"Algebra": {"worksheet", "quiz", "poster", "lab", "warmup"}},
            )
        prior = {
            "date": YESTERDAY,
            "classes": {"Algebra": {"pct": 86, "mark": "B"}},
        }
        events = scraper.diff_change_events(snap, prior, TODAY.date())
        kinds = {event["type"] for event in events}
        self.assertNotIn("lapsed", kinds)
        self.assertNotIn("turned_in", kinds)
        self.assertNotIn("newly_missing", kinds)
        self.assertNotIn("marked", kinds)
        self.assertIn("pct_move", kinds)

    def test_already_past_due_open_item_does_not_lapse_again(self):
        first_night = {
            "date": "2026-09-24",
            "classes": {
                "Algebra": {
                    "pct": 90,
                    "items": [{"title": "Lab", "due": "2026-09-24", "state": "open"}],
                }
            },
        }
        second_night = {
            "date": "2026-09-25",
            "classes": {
                "Algebra": {
                    "pct": 90,
                    "items": [{"title": "Lab", "due": "2026-09-24", "state": "open"}],
                }
            },
        }
        third_night = {
            "date": "2026-09-26",
            "classes": {
                "Algebra": {
                    "pct": 90,
                    "items": [{"title": "Lab", "due": "2026-09-24", "state": "open"}],
                }
            },
        }
        first = scraper.diff_change_events(second_night, first_night, datetime(2026, 9, 25).date())
        self.assertEqual([event["type"] for event in first], ["lapsed"])
        again = scraper.diff_change_events(third_night, second_night, datetime(2026, 9, 26).date())
        self.assertFalse(any(event["type"] == "lapsed" for event in again))

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
        self.assertNotIn("\n", text)

    def test_phrase_names_two_changes_then_a_count(self):
        text = scraper.phrase_since_yesterday([
            {"type": "lapsed", "title": "Lab", "class_name": "Algebra", "due": "2026-10-02"},
            {"type": "turned_in", "title": "Quiz", "class_name": "Algebra"},
            {"type": "newly_missing", "title": "Worksheet", "class_name": "Algebra"},
            {"type": "marked", "title": "Warmup", "class_name": "Algebra", "mark_code": "NA"},
            {"type": "pct_move", "class_name": "Biology", "delta": -4, "pct": 80},
        ])
        self.assertIn("Lab in Algebra was due Oct 2 and is still open", text)
        self.assertIn("Quiz in Algebra was turned in", text)
        self.assertIn("and 3 more", text)
        self.assertNotIn("Worksheet", text)
        self.assertNotIn("Warmup", text)
        self.assertNotIn("Biology", text)
        self.assertNotIn("2026-10-02", text)
        self.assertNotIn("\n", text)
        self.assertEqual(text.count("."), 1)

    def test_model_reply_longer_than_one_short_line_is_discarded(self):
        events = [{"type": "turned_in", "title": "Quiz", "class_name": "Algebra"}]
        plain = scraper.phrase_since_yesterday(events)
        wall = (
            "Lab (2026-10-02), Quiz (2026-10-01), and Poster (2026-09-18) have lapsed. "
            "Worksheet was turned in."
        )
        long_line = "Quiz in Algebra was turned in, and " + ("more " * 40)
        kept = "Quiz in Algebra was turned in."
        cases = {
            "two sentences": wall,
            "over 140 characters": long_line.strip(),
            "iso date": "Lab was due 2026-10-02 and is still open.",
            "kept": kept,
        }
        for name, sentence in cases.items():
            response = mock.Mock()
            response.status_code = 200
            response.json.return_value = {
                "choices": [{"message": {"content": json.dumps({"sentence": sentence})}}],
            }
            with mock.patch.object(scraper, "GROK_API_KEY", "test-key"), \
                    mock.patch.object(scraper.requests, "post", return_value=response) as post:
                text = scraper.generate_since_yesterday(events)
            sent = json.loads(post.call_args.kwargs["json"]["messages"][1]["content"].split("\n", 1)[1])
            self.assertNotIn("due", sent[0])
            if name == "kept":
                self.assertEqual(text, kept)
            else:
                self.assertEqual(text, plain, name)
                self.assertNotIn("2026-", text)

    def test_evening_window_is_after_the_afternoon_scrape(self):
        self.assertFalse(scraper.is_evening_snapshot(datetime(2026, 9, 25, 16, 45)))
        self.assertTrue(scraper.is_evening_snapshot(datetime(2026, 9, 25, 20, 7)))
        self.assertTrue(scraper.is_evening_snapshot(datetime(2026, 11, 5, 19, 7)))


if __name__ == "__main__":
    unittest.main()
