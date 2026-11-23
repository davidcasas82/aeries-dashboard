"""Progress sentences and impact lines, on fixture grades only."""

import unittest
from datetime import date

import progress


TODAY = date(2026, 11, 22)


def _snap(day, pct, mark, scores=None):
    row = {"pct": pct, "mark": mark}
    if scores is not None:
        row["scores"] = scores
    return {"date": day, "classes": {"Algebra": row}}


def _points(start, steps):
    """One point every 14 days, then the last day."""
    snaps = []
    day = date(2026, 9, 11)
    pct = start
    for offset, delta in enumerate(steps):
        pct += delta
        snaps.append(_snap((day if offset == 0 else day.fromordinal(day.toordinal() + offset * 14)).isoformat(), pct, "A" if pct >= 90 else "B" if pct >= 80 else "C"))
    return snaps


class ProgressTests(unittest.TestCase):
    def test_two_fortnight_drops_are_a_multi_week_decline(self):
        snaps = [
            _snap("2026-09-11", 100, "A"),
            _snap("2026-09-25", 100, "A"),
            _snap("2026-10-09", 94, "A"),
            _snap("2026-10-23", 93, "A"),
            _snap("2026-11-06", 93, "A"),
            _snap("2026-11-20", 84, "B"),
            _snap("2026-11-22", 84, "B"),
        ]
        points = progress._series(snaps, "Algebra")
        flagged, drops, under = progress.decline_flag(points, TODAY)
        self.assertTrue(flagged)
        self.assertGreaterEqual(drops, 2)
        self.assertGreaterEqual(under, 5)

    def test_one_cliff_is_not_a_multi_week_decline(self):
        snaps = [
            _snap("2026-09-11", 95, "A"),
            _snap("2026-11-17", 95, "A"),
            _snap("2026-11-18", 85, "B"),
            _snap("2026-11-22", 85, "B"),
        ]
        points = progress._series(snaps, "Algebra")
        flagged, _drops, _under = progress.decline_flag(points, TODAY)
        self.assertFalse(flagged)

    def test_was_line_uses_the_day_a_recent_letter_began(self):
        snaps = [
            _snap("2026-11-16", 78.6, "C"),
            _snap("2026-11-17", 81.3, "B"),
            _snap("2026-11-21", 81.3, "B"),
            _snap("2026-11-22", 76.3, "C"),
        ]
        line = progress.was_now(progress._series(snaps, "Algebra"), TODAY)
        self.assertEqual(line["text"], "was B 81% Nov 17 → now C 76%")
        self.assertEqual(line["direction"], "down")

    def test_unchanged_class_has_no_was_line(self):
        snaps = [_snap("2026-11-16", 90, "A"), _snap("2026-11-22", 90, "A")]
        self.assertIsNone(progress.was_now(progress._series(snaps, "Algebra"), TODAY))

    def test_a_covered_move_lists_each_items_impact(self):
        insight = {"layout": "percent_of_grade", "categories": [{"name": "Tests", "weight_pct": 100}]}
        student = {
            "classes": [{"course_name": "Algebra", "counted_insight": insight}],
            "assignments_by_class": [{
                "class_name": "Algebra",
                "period": 1,
                "assignments": [
                    {"description": "Quiz", "category": "Tests", "points_earned": 10, "points_possible": 10, "score_raw": "10 / 10"},
                    {"description": "Exam", "category": "Tests", "points_earned": 5, "points_possible": 10, "score_raw": "5 / 10"},
                ],
            }],
        }
        snaps = [
            _snap("2026-11-21", 100, "A", {"quiz": [10, 10, "2026-11-01"]}),
            _snap("2026-11-22", 75, "C", {"quiz": [10, 10, "2026-11-01"], "exam": [5, 10, "2026-11-22"]}),
        ]
        view = progress.build_student(student["classes"] and {
            "name": "Kid",
            "sn": "s1",
            "classes": student["classes"],
            "assignments_by_class": student["assignments_by_class"],
        }, snaps, [], TODAY)
        recent = view["classes"][0]["recent"]
        self.assertTrue(recent[0]["impacts"])
        self.assertIn("Exam", recent[0]["impacts"][0]["text"])
        self.assertIn("down", recent[0]["impacts"][0]["text"])
        self.assertNotIn("s1", view["summary"])

    def test_a_move_without_scores_says_impact_was_not_kept(self):
        student = {"name": "Kid", "sn": "s1", "classes": [{"course_name": "Algebra", "percent": 85, "mark": "B"}], "assignments_by_class": []}
        snaps = [_snap("2026-11-17", 95, "A"), _snap("2026-11-18", 85, "B")]
        view = progress.build_student(student, snaps, [], TODAY)
        move = view["classes"][0]["recent"][0]
        self.assertEqual(move["impacts"], [])
        self.assertIn("Impact not kept", move["missing"])
        self.assertIn("impact was not saved", view["summary"])

    def test_a_score_map_weeks_later_does_not_invent_a_share(self):
        insight = {"layout": "percent_of_grade", "categories": [{"name": "Tests", "weight_pct": 100}]}
        student = {
            "name": "Kid", "sn": "s1",
            "classes": [{"course_name": "Algebra", "percent": 93, "mark": "A", "counted_insight": insight}],
            "assignments_by_class": [{
                "class_name": "Algebra", "period": 1,
                "assignments": [{"description": "Quiz", "category": "Tests", "points_earned": 8.5, "points_possible": 10, "score_raw": "8.5 / 10"}],
            }],
        }
        snaps = [
            _snap("2026-10-21", 97, "A"),
            _snap("2026-10-22", 93, "A"),
            _snap("2026-11-19", 93, "A", {"quiz": [8.5, 10, "2026-10-22"]}),
        ]
        log_rows = [{"class": "Algebra", "title": "Quiz", "first_seen": "2026-10-22", "baseline": False, "earned": 8.5, "possible": 10}]
        view = progress.build_student(student, snaps, log_rows, TODAY)
        earlier = view["classes"][0]["earlier"]
        self.assertTrue(earlier)
        self.assertEqual(earlier[0]["impacts"], [])
        self.assertIn("Quiz", earlier[0]["missing"])
        self.assertIn("impact not kept", earlier[0]["missing"])

    def test_summary_names_a_multi_week_decline_without_calling_it_a_risk(self):
        snaps = []
        day = date(2026, 9, 11)
        for pct, mark in ((100, "A"), (100, "A"), (93, "A"), (93, "A"), (84, "B")):
            snaps.append(_snap(day.isoformat(), pct, mark))
            day = day.fromordinal(day.toordinal() + 14)
        snaps.append(_snap("2026-11-22", 84, "B"))
        student = {"name": "Kid", "sn": "s1", "classes": [{"course_name": "Algebra"}], "assignments_by_class": []}
        view = progress.build_student(student, snaps, [], TODAY)
        self.assertIn("multi-week decline", view["summary"])
        self.assertNotIn("biggest risk", view["summary"].lower())
        self.assertNotIn("easiest win", view["summary"].lower())


if __name__ == "__main__":
    unittest.main()
