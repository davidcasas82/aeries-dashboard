import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import glance  # noqa: E402
import scraper  # noqa: E402

MONDAY = datetime(2026, 9, 14)
FRIDAY = datetime(2026, 9, 18)
SATURDAY = datetime(2026, 9, 19)
SUNDAY = datetime(2026, 9, 20)
FIXTURES = Path(__file__).parent / "fixtures"


def _patched(today):
    return (
        patch.object(scraper, "pacific_today_dt", return_value=today),
        patch.object(scraper, "pacific_today", return_value=today.date()),
    )


def kid_fixture():
    return {
        "name": "Student A",
        "sn": "1",
        "classes": [
            {"period": 4, "course_name": "US History", "teacher": "Healey", "percent": "88", "mark": "B+"},
            {"period": 5, "course_name": "Algebra 2", "teacher": "Byun", "percent": "81", "mark": "B-"},
            {"period": 6, "course_name": "Biology", "teacher": "Chung", "percent": "93", "mark": "A"},
            {"period": 7, "course_name": "PE Course 1", "teacher": "Gray", "percent": "", "mark": ""},
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
        ],
        "class_trends": {
            "Algebra 2": {"delta_7d": -2.0, "pct_history": [{"pct": 83}, {"pct": 81}]},
            "Biology": {"delta_7d": 3.0, "pct_history": [{"pct": 90}, {"pct": 93}]},
            "US History": {"delta_7d": 0.0, "pct_history": [{"pct": 88}, {"pct": 88}]},
        },
        "ai_summary": {},
    }


def view_for(student, export=None, today=MONDAY, history_context=None):
    if export is not None:
        scraper.attach_classroom_export(student, export, today=today)
    if history_context is None:
        history_context = {"classes": {
            name: {"delta_7d": t.get("delta_7d")}
            for name, t in (student.get("class_trends") or {}).items()
        }}
    # Trends live on the student payload; build_student_view reads class_trends.
    p1, p2 = _patched(today)
    with p1, p2:
        return scraper.build_student_view(student, history_context=history_context)


class WeekendRuleTests(unittest.TestCase):
    def test_weekend_window(self):
        self.assertFalse(glance.is_weekend_night(MONDAY))
        self.assertTrue(glance.is_weekend_night(FRIDAY))
        self.assertEqual(
            glance.weekend_dates(FRIDAY)["Monday"].isoformat(),
            "2026-09-21",
        )
        self.assertIsNone(glance.weekend_dates(MONDAY))

    def test_friday_never_labels_monday_due_tonight(self):
        student = {
            "name": "Student A",
            "sn": "1",
            "classes": [
                {"period": 5, "course_name": "Algebra 2", "teacher": "Byun", "percent": "81", "mark": "B-"},
                {"period": 6, "course_name": "Biology", "teacher": "Chung", "percent": "93", "mark": "A"},
                {"period": 8, "course_name": "English", "teacher": "Lee", "percent": "89", "mark": "B+"},
            ],
            "assignments_by_class": [
                {"class_name": "5- Algebra 2- Fall", "period": 5, "assignments": [
                    {"description": "Practice set 2", "due_date": "09/21/2026", "points_earned": None,
                     "points_possible": 10, "grading_complete": False},
                ]},
                {"class_name": "6- Biology- Fall", "period": 6, "assignments": [
                    {"description": "Lab reflection", "due_date": "09/19/2026", "points_earned": None,
                     "points_possible": 10, "grading_complete": False},
                ]},
                {"class_name": "8- English- Fall", "period": 8, "assignments": [
                    {"description": "Reading draft", "due_date": "09/20/2026", "points_earned": None,
                     "points_possible": 10, "grading_complete": False},
                ]},
            ],
            "class_trends": {},
            "ai_summary": {},
            "classroom": {
                "captured_at": "2026-09-18T01:00:00.000Z",
                "courses": [],
            },
        }
        g = view_for(student, today=FRIDAY)["glance"]
        tonight_titles = [i["title"] for i in g["tonight"]["items"]]
        self.assertEqual(tonight_titles, [])
        self.assertNotIn("Due tonight", json.dumps(g))
        self.assertNotIn("due tonight", json.dumps(g).lower())
        weekend = g["tonight"]["weekend"]
        self.assertEqual(weekend["heading"], "This weekend — turn in before Monday")
        labels = [(i["label"], i["title"]) for i in weekend["items"]]
        self.assertEqual(labels, [
            ("Saturday", "Lab reflection"),
            ("Sunday", "Reading draft"),
            ("Monday", "Practice set 2"),
        ])

    def test_saturday_and_sunday_keep_monday_in_weekend_bucket(self):
        student = {
            "name": "Student A",
            "sn": "1",
            "classes": [{"period": 5, "course_name": "Algebra 2", "teacher": "Byun", "percent": "81", "mark": "B-"}],
            "assignments_by_class": [
                {"class_name": "5- Algebra 2- Fall", "period": 5, "assignments": [
                    {"description": "Practice set 2", "due_date": "09/21/2026", "points_earned": None,
                     "points_possible": 10, "grading_complete": False},
                ]},
            ],
            "class_trends": {},
            "ai_summary": {},
        }
        for day in (SATURDAY, SUNDAY):
            g = view_for(student, today=day)["glance"]
            self.assertEqual(g["tonight"]["items"], [])
            self.assertEqual(g["tonight"]["weekend"]["items"][0]["label"], "Monday")
            self.assertEqual(g["tonight"]["weekend"]["items"][0]["title"], "Practice set 2")


class FixtureGlanceTests(unittest.TestCase):
    def setUp(self):
        export = json.loads((FIXTURES / "classroom_export_sample.json").read_text())
        self.view = view_for(kid_fixture(), export, today=MONDAY)
        self.g = self.view["glance"]

    def test_standing_every_class_and_no_fake_flat_arrow(self):
        by_name = {c["course"]: c for c in self.g["standing"]}
        self.assertEqual(by_name["Algebra 2"]["mark"], "B-")
        self.assertEqual(by_name["Algebra 2"]["percent"], "81%")
        self.assertEqual(by_name["Algebra 2"]["trend"], "down")
        self.assertEqual(by_name["Biology"]["trend"], "up")
        self.assertEqual(by_name["US History"]["trend"], "flat")
        self.assertEqual(by_name["PE Course 1"]["trend"], "none")
        self.assertEqual(by_name["PE Course 1"]["trend_text"], "no baseline")
        self.assertEqual(by_name["PE Course 1"]["trend_symbol"], "•")
        self.assertEqual(by_name["PE Course 1"]["mark"], "—")

    def test_tonight_is_class_level_bullets_and_suppresses_already_in(self):
        items = self.g["tonight"]["items"]
        titles = [i["title"] for i in items]
        self.assertIn("Unit 3 Quiz Review", titles)
        self.assertTrue(any(i["kind"] == "forecast" for i in items))
        self.assertNotIn("3.2 Practice", titles)
        self.assertNotIn("Section 3.2 Practice", titles)
        self.assertGreaterEqual(self.g["suppressed"], 1)
        self.assertLessEqual(len(items), 3)
        self.assertIsNone(self.g["tonight"]["weekend"])

    def test_forecast_uses_newest_teacher_note(self):
        forecast = next(i for i in self.g["tonight"]["items"] if i["kind"] == "forecast")
        self.assertEqual(forecast["title"], "Quiz Friday")
        self.assertIn("3.1-3.3", forecast["drawer"]["teacher_words"])
        self.assertTrue(forecast["drawer"]["link"].startswith("https://classroom.google.com/"))

    def test_drawer_has_teacher_wording_both_statuses_and_link(self):
        quiz = next(i for i in self.g["tonight"]["items"] if i["title"] == "Unit 3 Quiz Review")
        d = quiz["drawer"]
        self.assertIn("review packet", d["teacher_words"])
        self.assertIn("Classroom", d["classroom"] + "x")
        self.assertTrue(d["classroom"])
        self.assertTrue(d["aeries"])
        self.assertTrue(d["link"].startswith("https://classroom.google.com/"))
        self.assertNotIn("work", d)

    def test_standing_drawer_lists_all_class_work(self):
        alg = next(c for c in self.g["standing"] if c["course"] == "Algebra 2")
        self.assertEqual(alg["drawer"]["kicker"], "All the work")
        names = [w["name"] for w in alg["drawer"]["work"]]
        self.assertIn("3.2 Practice", names)
        self.assertIn("3.1 Practice", names)
        self.assertIn("Unit 3 Quiz Review", names)
        scored = next(w for w in alg["drawer"]["work"] if w["name"] == "3.1 Practice")
        self.assertEqual(scored["score"], "18/20")
        self.assertEqual(scored["when"], "due Sep 8")
        missing = next(w for w in alg["drawer"]["work"] if w["name"] == "3.2 Practice")
        self.assertTrue(missing["missing"])
        self.assertTrue(missing["turned_in"])
        self.assertIn("turned in", missing["status"])
        self.assertRegex(missing["status"], r"Sep \d+")
        self.assertEqual(missing["when"], "due Sep 10")
        quiz = next(w for w in alg["drawer"]["work"] if w["name"] == "Unit 3 Quiz Review")
        self.assertEqual(quiz["score"], "awaiting")
        self.assertIn("review packet", quiz["description"])
        self.assertIn(quiz["when"], ("due tomorrow", "due Sep 15", "due Sep 16"))
        blob = json.dumps(alg["drawer"]["work"])
        for banned in ("rubric", "drive", "text_excerpt", "materials", "attachments"):
            self.assertNotIn(banned, blob)

    def test_empty_class_has_no_work_rows(self):
        pe = next(c for c in self.g["standing"] if c["course"] == "PE Course 1")
        self.assertEqual(pe["drawer"]["work"], [])
        self.assertEqual(pe["drawer"]["kicker"], "All the work")

    def test_empty_night_copy(self):
        student = {
            "name": "Student A",
            "sn": "1",
            "classes": [{"period": 1, "course_name": "Study Hall", "teacher": "X", "percent": "95", "mark": "A"}],
            "assignments_by_class": [
                {"class_name": "1- Study Hall- Fall", "period": 1, "assignments": [
                    {"description": "Done", "due_date": "09/10/2026", "points_earned": 5,
                     "points_possible": 5, "percentage": 100, "grading_complete": True},
                ]},
            ],
            "class_trends": {"Study Hall": {"delta_7d": 1.0}},
            "ai_summary": {},
        }
        g = view_for(student, today=MONDAY)["glance"]
        self.assertTrue(g["tonight"]["empty"])
        self.assertEqual(g["tonight"]["empty_line"], "Nothing verified needs action.")
        self.assertEqual(g["verified_count"], 0)


class ClassWorkDrawerTests(unittest.TestCase):
    def test_awaiting_score_and_description_from_payload(self):
        student = kid_fixture()
        student["assignments_by_class"][2]["assignments"].append({
            "description": "Microscope sketch",
            "due_date": "09/10/2026",
            "points_earned": None,
            "points_possible": 10,
            "grading_complete": False,
            "comment": "Use the circle template.",
        })
        g = view_for(student, today=MONDAY)["glance"]
        bio = next(c for c in g["standing"] if c["course"] == "Biology")
        names = [w["name"] for w in bio["drawer"]["work"]]
        self.assertIn("Cell lab", names)
        self.assertIn("Microscope sketch", names)
        row = next(w for w in bio["drawer"]["work"] if w["name"] == "Microscope sketch")
        self.assertEqual(row["score"], "awaiting")
        self.assertEqual(row["description"], "Use the circle template.")
        self.assertFalse(row["missing"])
        self.assertEqual(row["when"], "4 days late · due Sep 10")
        scored = next(w for w in bio["drawer"]["work"] if w["name"] == "Cell lab")
        self.assertEqual(scored["score"], "28/30")
        self.assertEqual(scored["when"], "due Sep 12")

    def test_due_today_tomorrow_and_turned_in_date(self):
        student = {
            "name": "Student A",
            "sn": "1",
            "classes": [{"period": 5, "course_name": "Algebra 2", "teacher": "Byun", "percent": "81", "mark": "B-"}],
            "assignments_by_class": [
                {"class_name": "5- Algebra 2- Fall", "period": 5, "assignments": [
                    {"description": "Warmup", "due_date": "09/14/2026", "points_earned": None,
                     "points_possible": 5, "grading_complete": False},
                    {"description": "Quiz review", "due_date": "09/15/2026", "points_earned": None,
                     "points_possible": 10, "grading_complete": False},
                    {"description": "Notes check", "due_date": "09/10/2026", "points_earned": None,
                     "points_possible": 5, "grading_complete": False, "aeries_missing": True,
                     "status": "missing",
                     "classroom": {"state": "TURNED_IN", "state_label": "Turned in",
                                   "turned_in_on": "2026-09-11"}},
                ]},
            ],
            "class_trends": {},
            "ai_summary": {},
        }
        g = view_for(student, today=MONDAY)["glance"]
        alg = next(c for c in g["standing"] if c["course"] == "Algebra 2")
        by_name = {w["name"]: w for w in alg["drawer"]["work"]}
        self.assertEqual(by_name["Warmup"]["when"], "due today")
        self.assertEqual(by_name["Quiz review"]["when"], "due tomorrow")
        self.assertEqual(by_name["Notes check"]["when"], "due Sep 10")
        self.assertEqual(by_name["Notes check"]["status"], "turned in Sep 11 · Aeries missing")
        tonight = json.dumps(g["tonight"])
        self.assertNotIn("4 days late", tonight)

    def test_classroom_or_aeries_in_is_not_late(self):
        """Drawer late uses the same already-in check as Tonight."""
        student = {
            "name": "Student A",
            "sn": "1",
            "classes": [{"period": 5, "course_name": "Algebra 2", "teacher": "Byun", "percent": "81", "mark": "B-"}],
            "assignments_by_class": [
                {"class_name": "5- Algebra 2- Fall", "period": 5, "assignments": [
                    {"description": "Classroom in", "due_date": "09/10/2026", "points_earned": None,
                     "points_possible": 10, "grading_complete": False, "aeries_missing": True,
                     "status": "missing",
                     "classroom": {"state": "TURNED_IN", "state_label": "Turned in on Classroom",
                                   "turned_in_on": "2026-09-11"}},
                    {"description": "Aeries handed in", "due_date": "09/10/2026", "points_earned": None,
                     "points_possible": 10, "grading_complete": False, "date_completed": "09/11/2026"},
                    {"description": "Still out", "due_date": "09/10/2026", "points_earned": None,
                     "points_possible": 10, "grading_complete": False, "aeries_missing": True,
                     "status": "missing"},
                ]},
            ],
            "class_trends": {},
            "ai_summary": {},
        }
        g = view_for(student, today=MONDAY)["glance"]
        alg = next(c for c in g["standing"] if c["course"] == "Algebra 2")
        by_name = {w["name"]: w for w in alg["drawer"]["work"]}
        self.assertEqual(by_name["Classroom in"]["when"], "due Sep 10")
        self.assertIn("turned in", by_name["Classroom in"]["status"])
        self.assertNotIn("late", by_name["Classroom in"]["when"])
        self.assertEqual(by_name["Aeries handed in"]["when"], "due Sep 10")
        self.assertIn("turned in", by_name["Aeries handed in"]["status"])
        self.assertEqual(by_name["Still out"]["when"], "4 days late · due Sep 10")
        self.assertEqual(by_name["Still out"]["status"], "missing")
        tonight_titles = [i["title"] for i in g["tonight"]["items"]]
        self.assertNotIn("Classroom in", tonight_titles)
        self.assertNotIn("Aeries handed in", tonight_titles)


class ForecastNewestTests(unittest.TestCase):
    def test_oldest_announcements_do_not_win(self):
        classes = [{
            "period": 5,
            "course_name": "Algebra 2",
            "mark": "B-",
            "percent": "81",
            "delta_7d": None,
            "upcoming": [],
            "missing": [],
            "classroom": {
                "link": "https://classroom.google.com/c/c-alg",
                "announcements": [
                    {"text": "Welcome quiz was last month.", "posted_on": "2026-08-12", "link": ""},
                    {"text": "Old test Tuesday is long gone.", "posted_on": "2026-08-18", "link": ""},
                    {"text": "Quiz Friday covers 3.1-3.3. Bring a calculator.", "posted_on": "2026-09-12", "link": ""},
                ],
            },
        }]
        forecast = glance.pick_forecast(classes, MONDAY)
        self.assertIsNotNone(forecast)
        self.assertIn("Quiz Friday", forecast["title"])
        self.assertNotIn("Welcome", forecast["drawer"]["teacher_words"])
        self.assertNotIn("Old test", forecast["drawer"]["teacher_words"])


if __name__ == "__main__":
    unittest.main()
