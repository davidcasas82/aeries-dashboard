import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scraper  # noqa: E402

PT = scraper.PACIFIC
EVENING = datetime(2026, 10, 22, 20, 7, tzinfo=PT)
NEXT_EVENING = datetime(2026, 10, 23, 20, 7, tzinfo=PT)


def scored(title, earned, due, possible=10, category="Homework"):
    return {
        "description": title,
        "category": category,
        "points_earned": earned,
        "points_possible": possible,
        "percentage": None,
        "date_assigned": "",
        "due_date": due,
        "date_completed": "",
    }


def student(*classes):
    """classes: (period, course name, [assignments])"""
    return {
        "classes": [{"period": p, "course_name": name} for p, name, _ in classes],
        "assignments_by_class": [
            {"class_name": f"{p} {name}", "period": p, "assignments": rows}
            for p, name, rows in classes
        ],
    }


def log_rows(doc, key="s1"):
    return [
        (row["class"], row["title"], row["first_seen"], row["scrape"], row["baseline"])
        for row in doc["students"][key]["items"]
    ]


class ScoredLogTests(unittest.TestCase):
    def test_first_run_is_baseline_then_only_new_scores_are_logged(self):
        doc = {}
        day1 = student((1, "Algebra", [
            scored("Quiz 1", 8, "10/20/2026"),
            scored("Worksheet", None, "10/23/2026"),
        ]))
        counts = scraper.apply_scored_log(doc, [("student 1", "s1", day1)], EVENING)
        self.assertEqual(counts["new"], {"student 1": 1})
        self.assertEqual(log_rows(doc), [("Algebra", "Quiz 1", "2026-10-22", "evening", True)])

        day2 = student((1, "Algebra", [
            scored("Quiz 1", 8, "10/20/2026"),
            scored("Worksheet", 9, "10/23/2026"),
        ]))
        counts = scraper.apply_scored_log(doc, [("student 1", "s1", day2)], NEXT_EVENING)
        self.assertEqual(counts, {"new": {"student 1": 1}, "dropped": 0, "total": 2})
        self.assertEqual(log_rows(doc), [
            ("Algebra", "Quiz 1", "2026-10-22", "evening", True),
            ("Algebra", "Worksheet", "2026-10-23", "evening", False),
        ])

    def test_rerun_of_the_same_scrape_adds_nothing(self):
        doc = {}
        day = student((1, "Algebra", [scored("Quiz 1", 8, "10/20/2026")]))
        scraper.apply_scored_log(doc, [("student 1", "s1", day)], EVENING)
        counts = scraper.apply_scored_log(doc, [("student 1", "s1", day)], EVENING)
        self.assertEqual(counts["new"], {"student 1": 0})
        self.assertEqual(counts["total"], 1)

    def test_scrape_slot_comes_from_the_pacific_hour(self):
        self.assertEqual(scraper.scrape_slot(datetime(2026, 10, 22, 2, 7, tzinfo=PT)), "overnight")
        self.assertEqual(scraper.scrape_slot(datetime(2026, 10, 22, 16, 45, tzinfo=PT)), "afternoon")
        self.assertEqual(scraper.scrape_slot(datetime(2026, 10, 22, 20, 7, tzinfo=PT)), "evening")
        self.assertEqual(scraper.scrape_slot(datetime(2026, 11, 2, 19, 7, tzinfo=PT)), "evening")
        self.assertEqual(scraper.scrape_slot(datetime(2026, 10, 22, 11, 0, tzinfo=PT)), "daytime")

    def test_afternoon_scrape_that_sees_a_score_first_is_recorded(self):
        doc = {}
        scraper.apply_scored_log(doc, [("student 1", "s1", student((1, "Algebra", [])))], EVENING)
        afternoon = datetime(2026, 10, 23, 16, 45, tzinfo=PT)
        day = student((1, "Algebra", [scored("Quiz 2", 7, "10/23/2026")]))
        scraper.apply_scored_log(doc, [("student 1", "s1", day)], afternoon)
        scraper.apply_scored_log(doc, [("student 1", "s1", day)], NEXT_EVENING)
        self.assertEqual(log_rows(doc), [("Algebra", "Quiz 2", "2026-10-23", "afternoon", False)])
        self.assertEqual(doc["students"]["s1"]["items"][0]["first_seen_at"], "2026-10-23T16:45-07:00")

    def test_quarter_reset_keeps_logged_entries(self):
        doc = {}
        q1 = student((1, "Algebra", [scored("Quiz 1", 8, "10/20/2026")]))
        scraper.apply_scored_log(doc, [("student 1", "s1", q1)], EVENING)
        q2 = student((1, "Algebra", [scored("Quiz 1", 6, "11/10/2026")]))
        counts = scraper.apply_scored_log(
            doc, [("student 1", "s1", q2)], datetime(2026, 11, 10, 20, 7, tzinfo=PT)
        )
        self.assertEqual(counts["new"], {"student 1": 1})
        self.assertEqual(log_rows(doc), [
            ("Algebra", "Quiz 1", "2026-10-22", "evening", True),
            ("Algebra", "Quiz 1", "2026-11-10", "evening", False),
        ])

    def test_same_title_in_two_classes_is_two_entries(self):
        doc = {}
        scraper.apply_scored_log(doc, [("student 1", "s1", student(
            (1, "Algebra", []), (2, "Biology", []),
        ))], EVENING)
        day = student(
            (1, "Algebra", [scored("Warm-up", 5, "10/23/2026", possible=5)]),
            (2, "Biology", [scored("Warm-up", 5, "10/23/2026", possible=5)]),
        )
        counts = scraper.apply_scored_log(doc, [("student 1", "s1", day)], NEXT_EVENING)
        self.assertEqual(counts["new"], {"student 1": 2})
        self.assertEqual(log_rows(doc), [
            ("Algebra", "Warm-up", "2026-10-23", "evening", False),
            ("Biology", "Warm-up", "2026-10-23", "evening", False),
        ])

    def test_repeated_title_with_a_new_due_date_is_a_new_score(self):
        doc = {}
        scraper.apply_scored_log(doc, [("student 1", "s1", student(
            (1, "Algebra", [scored("Homework", 9, "10/20/2026")]),
        ))], EVENING)
        day = student((1, "Algebra", [
            scored("Homework", 9, "10/20/2026"),
            scored("Homework", 10, "10/23/2026"),
        ]))
        counts = scraper.apply_scored_log(doc, [("student 1", "s1", day)], NEXT_EVENING)
        self.assertEqual(counts["new"], {"student 1": 1})
        self.assertEqual(
            [(r["title"], r.get("due")) for r in doc["students"]["s1"]["items"]],
            [("Homework", "2026-10-20"), ("Homework", "2026-10-23")],
        )

    def test_renamed_item_matches_on_class_due_and_points(self):
        doc = {}
        scraper.apply_scored_log(doc, [("student 1", "s1", student(
            (1, "Algebra", [scored("Ch 3 Test", 40, "10/20/2026", possible=50)]),
        ))], EVENING)
        day = student((1, "Algebra", [scored("Chapter 3 Test", 40, "10/20/2026", possible=50)]))
        counts = scraper.apply_scored_log(doc, [("student 1", "s1", day)], NEXT_EVENING)
        self.assertEqual(counts["new"], {"student 1": 0})
        self.assertEqual(log_rows(doc), [("Algebra", "Chapter 3 Test", "2026-10-22", "evening", True)])

    def test_points_possible_edit_is_the_same_item(self):
        doc = {}
        scraper.apply_scored_log(doc, [("student 1", "s1", student(
            (1, "Algebra", [scored("Lab", 18, "10/20/2026", possible=20)]),
        ))], EVENING)
        day = student((1, "Algebra", [scored("Lab", 18, "10/20/2026", possible=25)]))
        counts = scraper.apply_scored_log(doc, [("student 1", "s1", day)], NEXT_EVENING)
        self.assertEqual(counts["new"], {"student 1": 0})
        self.assertEqual(doc["students"]["s1"]["items"][0]["possible"], 25)

    def test_entries_older_than_120_days_are_dropped(self):
        doc = {"students": {"s1": {"baseline_at": "x", "items": [
            {"class": "Algebra", "key": "old", "possible": 1, "first_seen": "2026-06-23"},
            {"class": "Algebra", "key": "edge", "possible": 1, "first_seen": "2026-06-24"},
        ]}}}
        counts = scraper.apply_scored_log(doc, [("student 1", "s1", student((1, "Algebra", [])))], EVENING)
        self.assertEqual(counts["dropped"], 1)
        self.assertEqual([r["key"] for r in doc["students"]["s1"]["items"]], ["edge"])

    def test_size_bound_drops_oldest_entries_first(self):
        items = [
            {"class": "Algebra", "key": f"k{i}", "possible": 1,
             "title": "x" * 80, "first_seen": f"2026-10-{1 + i % 20:02d}"}
            for i in range(200)
        ]
        doc = {"students": {"s1": {"items": items}}}
        dropped = scraper.trim_scored_log(doc, datetime(2026, 10, 22).date(), max_chars=10_000)
        kept = doc["students"]["s1"]["items"]
        self.assertLessEqual(len(json.dumps(doc, separators=(",", ":"))), 10_000)
        self.assertEqual(dropped + len(kept), 200)
        self.assertGreater(min(r["first_seen"] for r in kept), "2026-10-01")

    def test_full_term_stays_under_the_family_data_cap(self):
        rows = [
            scored(f"Assignment number {i} with a long teacher title", 9, "10/20/2026")
            for i in range(120)
        ]
        kid = student(*[(p, f"Course {p} name", list(rows)) for p in range(1, 8)])
        doc = {}
        scraper.apply_scored_log(doc, [("student 1", "s1", kid), ("student 2", "s2", kid)], EVENING)
        size = len(json.dumps(doc, separators=(",", ":")))
        self.assertEqual(sum(len(s["items"]) for s in doc["students"].values()), 1680)
        self.assertLess(size, scraper.SCORED_LOG_MAX_CHARS)


class PublishScoredLogTests(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(scraper, "family_data_configured", return_value=True),
            mock.patch.object(scraper, "family_unlock", return_value="token"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.kid = student((1, "Algebra", [scored("Quiz 1", 8, "10/20/2026")]))

    def test_puts_the_doc_back_under_the_version_it_read(self):
        calls = []

        def fake(method, path, token, payload=None):
            calls.append((method, path, payload and payload["version"]))
            if method == "GET":
                return {"doc": {}, "version": 0}
            return {"version": 1}

        with mock.patch.object(scraper, "family_request", side_effect=fake):
            counts = scraper.publish_scored_log([("student 1", "s1", self.kid)], now=EVENING)
        self.assertEqual(calls, [
            ("GET", "/v1/docs/aeries/scored-log", None),
            ("PUT", "/v1/docs/aeries/scored-log", 0),
        ])
        self.assertEqual(counts["new"], {"student 1": 1})

    def test_retries_once_after_a_version_conflict(self):
        conflict = requests.HTTPError(response=mock.Mock(status_code=409))
        replies = iter([
            {"doc": {}, "version": 3},
            conflict,
            {"doc": {}, "version": 4},
            {"version": 5},
        ])
        puts = []

        def fake(method, path, token, payload=None):
            reply = next(replies)
            if method == "PUT":
                puts.append(payload["version"])
            if isinstance(reply, Exception):
                raise reply
            return reply

        with mock.patch.object(scraper, "family_request", side_effect=fake):
            scraper.publish_scored_log([("student 1", "s1", self.kid)], now=EVENING)
        self.assertEqual(puts, [3, 4])

    def test_skips_when_no_student_was_freshly_scraped(self):
        with mock.patch.object(scraper, "family_request") as req:
            self.assertIsNone(scraper.publish_scored_log([], now=EVENING))
        req.assert_not_called()


if __name__ == "__main__":
    unittest.main()
