import json
import re
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import glance  # noqa: E402
import scraper  # noqa: E402

MONDAY = datetime(2026, 9, 14)
TUESDAY = datetime(2026, 9, 22)
THURSDAY = datetime(2026, 9, 17)
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
        focus_titles = [i["title"] for i in g["tonight"]["items"]]
        today_titles = [i["title"] for i in (g["tonight"].get("today") or [])]
        self.assertEqual(focus_titles, ["Practice set 2"])
        self.assertNotIn("Lab reflection", focus_titles)
        self.assertNotIn("Reading draft", focus_titles)
        later_titles = [i["title"] for i in (g["tonight"].get("later") or [])]
        self.assertEqual(later_titles, [])
        self.assertIsNone(g["bands"]["later"])
        self.assertNotIn("Practice set 2", today_titles)
        self.assertNotIn("Due tonight", json.dumps(g))
        self.assertNotIn("due tonight", json.dumps(g).lower())
        self.assertNotIn("Turn in", json.dumps(g["bands"]))
        self.assertNotIn("lowest", json.dumps(g["tonight"]).lower())
        self.assertIsNone(g["tonight"]["weekend"])
        self.assertIsNone(g["bands"]["today"])
        bio = next(c for c in g["standing"] if c["course"] == "Biology")
        self.assertEqual(
            next(w["bucket"] for w in bio["drawer"]["work"] if w["name"] == "Lab reflection"),
            "coming_up",
        )

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
            focus_titles = [i["title"] for i in g["tonight"]["items"]]
            today_titles = [i["title"] for i in (g["tonight"].get("today") or [])]
            self.assertIn("Practice set 2", focus_titles)
            self.assertNotIn("Practice set 2", today_titles)
            row = next(i for i in g["tonight"]["items"] if i["title"] == "Practice set 2")
            self.assertIn("Monday, Sep 21", row["label"])
            self.assertNotIn("due tonight", row["label"].lower())
            self.assertIsNone(g["tonight"]["weekend"])


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
        self.assertEqual(by_name["Algebra 2"]["grade_class"], "grade-b")
        self.assertEqual(by_name["Biology"]["grade_class"], "grade-a")
        self.assertEqual(by_name["PE Course 1"]["grade_class"], "grade-none")

    def test_tonight_is_class_level_bullets_and_suppresses_already_in(self):
        items = self.g["tonight"]["items"]
        titles = [i["title"] for i in items]
        self.assertEqual(titles, ["Unit 3 Quiz Review"])
        self.assertFalse(any(i["kind"] == "class" for i in items))
        self.assertNotIn("3.2 Practice", titles)
        self.assertNotIn("Section 3.2 Practice", titles)
        self.assertFalse(any("lowest" in (i.get("label") or "").lower() for i in items))
        alg = next(c for c in self.g["standing"] if c["course"] == "Algebra 2")
        self.assertEqual(
            next(w["bucket"] for w in alg["drawer"]["work"] if w["name"] == "Unit 3 Quiz Review"),
            "coming_up",
        )
        self.assertGreaterEqual(self.g["suppressed"], 1)
        self.assertIsNone(self.g["tonight"]["weekend"])

    def test_forecast_uses_newest_teacher_note(self):
        forecast = glance.pick_forecast(self.view["classes"], MONDAY)
        self.assertIsNotNone(forecast)
        self.assertEqual(forecast["title"], "Quiz Friday")
        self.assertEqual(forecast["label"], "Due Friday, Sep 18")
        self.assertNotIn("Coming", forecast["label"])
        self.assertNotIn("Quiz Friday", [i["title"] for i in self.g["tonight"]["items"]])

    def test_drawer_has_teacher_wording_and_statuses_without_classroom_url(self):
        quiz = next(w for w in next(c for c in self.g["standing"] if c["course"] == "Algebra 2")["drawer"]["work"] if w["name"] == "Unit 3 Quiz Review")
        self.assertEqual(quiz["bucket"], "coming_up")
        blob = json.dumps(self.g)
        self.assertNotIn("classroom.google.com", blob)
        self.assertNotIn("drive.google.com", blob)
        self.assertNotIn("Open in Classroom", blob)

    def test_standing_drawer_lists_all_class_work(self):
        alg = next(c for c in self.g["standing"] if c["course"] == "Algebra 2")
        self.assertEqual(alg["drawer"]["kicker"], "0 missing · 1 coming up · 2 turned in")
        names = [w["name"] for w in alg["drawer"]["work"]]
        self.assertIn("3.2 Practice", names)
        self.assertIn("3.1 Practice", names)
        self.assertIn("Unit 3 Quiz Review", names)
        scored = next(w for w in alg["drawer"]["work"] if w["name"] == "3.1 Practice")
        self.assertEqual(scored["score"], "18/20")
        self.assertTrue(scored["done"])
        self.assertEqual(scored["when"], "Due Tuesday, Sep 8")
        missing = next(w for w in alg["drawer"]["work"] if w["name"] == "3.2 Practice")
        self.assertFalse(missing["missing"])
        self.assertTrue(missing["turned_in"])
        self.assertEqual(missing["bucket"], "turned_in")
        self.assertEqual(missing["status"], "Aeries missing")
        self.assertEqual(missing["when"], "Due Thursday, Sep 10 / turned in Thursday, Sep 10")
        quiz = next(w for w in alg["drawer"]["work"] if w["name"] == "Unit 3 Quiz Review")
        self.assertEqual(quiz["score"], "awaiting")
        self.assertEqual(quiz["bucket"], "coming_up")
        self.assertIn("review packet", quiz["description"])
        self.assertEqual(quiz["when"], "Due tomorrow · Tuesday, Sep 15")
        self.assertEqual(scored["score_class"], "grade-a")
        names = [w["name"] for w in alg["drawer"]["work"]]
        self.assertLess(names.index("Unit 3 Quiz Review"), names.index("3.1 Practice"))
        self.assertLess(names.index("Unit 3 Quiz Review"), names.index("3.2 Practice"))
        self.assertEqual(alg["count_line"], "1 coming up · 2 turned in")
        self.assertEqual(alg["drawer"]["kicker"], "0 missing · 1 coming up · 2 turned in")
        blob = json.dumps(alg["drawer"]["work"])
        self.assertNotIn("classroom.google.com", blob)
        self.assertNotIn("drive.google.com", blob)
        for row in alg["drawer"]["work"]:
            for banned in ("rubric", "text_excerpt", "materials", "attachments", "topic"):
                self.assertNotIn(banned, row)

    def test_empty_class_has_no_work_rows(self):
        pe = next(c for c in self.g["standing"] if c["course"] == "PE Course 1")
        self.assertEqual(pe["drawer"]["work"], [])
        self.assertEqual(pe["count_line"], "")
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
        self.assertEqual(g["bands"]["due_soon"]["empty_line"], "Nothing due in the next 5 school days.")
        self.assertEqual(g["bands"]["due_soon"]["heading"], "Due soon")
        self.assertNotIn("coming up.", g["bands"]["due_soon"]["empty_line"].lower())
        self.assertEqual(g["tonight"]["items"], [])
        self.assertIsNone(g["bands"]["later"])
        self.assertIsNone(g["bands"]["today"])
        self.assertEqual(g["facts"], [])
        self.assertFalse(any("still to do" in json.dumps(g["bands"]).lower() for _ in [0]))
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
        self.assertEqual(row["bucket"], "past_due")
        self.assertEqual(row["when"], "4 days late · was due Thursday, Sep 10")
        scored = next(w for w in bio["drawer"]["work"] if w["name"] == "Cell lab")
        self.assertEqual(scored["score"], "28/30")
        self.assertEqual(scored["score_class"], "grade-a")
        self.assertEqual(scored["bucket"], "turned_in")
        self.assertEqual(scored["when"], "Due Saturday, Sep 12")
        names = [w["name"] for w in bio["drawer"]["work"]]
        self.assertLess(names.index("Microscope sketch"), names.index("Cell lab"))
        self.assertEqual(bio["count_line"], "1 past due · 1 turned in")
        self.assertNotIn("coming up", bio["count_line"])

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
        self.assertEqual(by_name["Warmup"]["when"], "Due today · Monday, Sep 14")
        self.assertEqual(by_name["Quiz review"]["when"], "Due tomorrow · Tuesday, Sep 15")
        self.assertEqual(
            by_name["Notes check"]["when"],
            "Due Thursday, Sep 10 / turned in Friday, Sep 11",
        )
        focus_titles = [i["title"] for i in g["tonight"]["items"]]
        today_titles = [i["title"] for i in (g["tonight"].get("today") or [])]
        self.assertEqual(focus_titles, ["Warmup", "Quiz review"])
        self.assertEqual(today_titles, [])
        self.assertEqual(g["tonight"]["items"][0]["label"], "Due today · Monday, Sep 14")
        self.assertEqual(g["tonight"]["items"][1]["label"], "Due tomorrow · Tuesday, Sep 15")
        self.assertFalse(any(l.lower() in ("due tonight", "coming monday") for l in [i["label"] for i in g["tonight"]["items"]]))
        self.assertEqual(by_name["Quiz review"]["bucket"], "coming_up")
        self.assertEqual(by_name["Notes check"]["status"], "Aeries missing")
        self.assertEqual(by_name["Notes check"]["bucket"], "turned_in")
        self.assertEqual(by_name["Warmup"]["bucket"], "coming_up")
        self.assertEqual(by_name["Quiz review"]["bucket"], "coming_up")
        self.assertEqual(alg["count_line"], "2 coming up · 1 turned in")
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
        self.assertEqual(
            by_name["Classroom in"]["when"],
            "Due Thursday, Sep 10 / turned in Friday, Sep 11",
        )
        self.assertEqual(by_name["Classroom in"]["status"], "Aeries missing")
        self.assertNotIn("late", by_name["Classroom in"]["when"])
        self.assertEqual(by_name["Classroom in"]["bucket"], "turned_in")
        self.assertEqual(
            by_name["Aeries handed in"]["when"],
            "Due Thursday, Sep 10 / turned in Friday, Sep 11",
        )
        self.assertEqual(by_name["Aeries handed in"]["bucket"], "turned_in")
        self.assertEqual(by_name["Still out"]["when"], "4 days late · was due Thursday, Sep 10")
        self.assertEqual(by_name["Still out"]["status"], "missing")
        self.assertEqual(by_name["Still out"]["bucket"], "missing")
        names = [w["name"] for w in alg["drawer"]["work"]]
        self.assertEqual(names[0], "Still out")
        self.assertEqual(alg["count_line"], "1 missing · 2 turned in")
        self.assertEqual(alg["drawer"]["kicker"], "1 missing · 2 turned in")
        tonight_titles = [i["title"] for i in g["tonight"]["items"]]
        self.assertNotIn("Classroom in", tonight_titles)
        self.assertNotIn("Aeries handed in", tonight_titles)
        self.assertNotIn("Still out", tonight_titles)

    def test_score_colors_use_earned_over_possible_bands(self):
        student = {
            "name": "Student A",
            "sn": "1",
            "classes": [{"period": 5, "course_name": "Algebra 2", "teacher": "Byun", "percent": "81", "mark": "B-"}],
            "assignments_by_class": [
                {"class_name": "5- Algebra 2- Fall", "period": 5, "assignments": [
                    {"description": "Perfect ten", "due_date": "09/10/2026", "points_earned": 10,
                     "points_possible": 10, "grading_complete": True},
                    {"description": "Half ten", "due_date": "09/10/2026", "points_earned": 5,
                     "points_possible": 10, "grading_complete": True},
                    {"description": "Five one", "due_date": "09/10/2026", "points_earned": 5.1,
                     "points_possible": 10, "grading_complete": True},
                    {"description": "Four fifths", "due_date": "09/10/2026", "points_earned": 4,
                     "points_possible": 5, "grading_complete": True},
                    {"description": "Nine tenths", "due_date": "09/10/2026", "points_earned": 9,
                     "points_possible": 10, "grading_complete": True},
                ]},
            ],
            "class_trends": {},
            "ai_summary": {},
        }
        g = view_for(student, today=MONDAY)["glance"]
        alg = next(c for c in g["standing"] if c["course"] == "Algebra 2")
        by_name = {w["name"]: w for w in alg["drawer"]["work"]}
        self.assertEqual(by_name["Perfect ten"]["score"], "10/10")
        self.assertEqual(by_name["Perfect ten"]["score_class"], "grade-a")
        self.assertEqual(by_name["Half ten"]["score"], "5/10")
        self.assertEqual(by_name["Half ten"]["score_class"], "grade-f")
        self.assertEqual(by_name["Five one"]["score"], "5.1/10")
        self.assertEqual(by_name["Five one"]["score_class"], "grade-f")
        self.assertEqual(by_name["Four fifths"]["score"], "4/5")
        self.assertEqual(by_name["Four fifths"]["score_class"], "grade-b")
        self.assertEqual(by_name["Nine tenths"]["score"], "9/10")
        self.assertEqual(by_name["Nine tenths"]["score_class"], "grade-a")

    def test_past_due_is_not_coming_up(self):
        student = {
            "name": "Student A",
            "sn": "1",
            "classes": [{"period": 5, "course_name": "Algebra 2", "teacher": "Byun", "percent": "81", "mark": "B-"}],
            "assignments_by_class": [
                {"class_name": "5- Algebra 2- Fall", "period": 5, "assignments": [
                    {"description": "Still out", "due_date": "09/10/2026", "points_earned": None,
                     "points_possible": 10, "grading_complete": False, "aeries_missing": True,
                     "status": "missing"},
                    {"description": "Late packet", "due_date": "09/10/2026", "points_earned": None,
                     "points_possible": 10, "grading_complete": False},
                    {"description": "Due today", "due_date": "09/14/2026", "points_earned": None,
                     "points_possible": 5, "grading_complete": False},
                    {"description": "Done notes", "due_date": "09/08/2026", "points_earned": 10,
                     "points_possible": 10, "grading_complete": True},
                ]},
            ],
            "class_trends": {},
            "ai_summary": {},
        }
        g = view_for(student, today=MONDAY)["glance"]
        alg = next(c for c in g["standing"] if c["course"] == "Algebra 2")
        by_name = {w["name"]: w for w in alg["drawer"]["work"]}
        self.assertEqual(by_name["Late packet"]["bucket"], "past_due")
        self.assertEqual(by_name["Still out"]["bucket"], "missing")
        self.assertEqual(by_name["Due today"]["bucket"], "coming_up")
        self.assertEqual(by_name["Done notes"]["bucket"], "turned_in")
        names = [w["name"] for w in alg["drawer"]["work"]]
        self.assertEqual(names, ["Late packet", "Still out", "Due today", "Done notes"])
        self.assertEqual(alg["count_line"], "1 past due · 1 missing · 1 coming up · 1 turned in")
        self.assertNotIn("coming up", by_name["Late packet"]["when"].lower())
        self.assertEqual(alg["drawer"]["kicker"], "1 past due · 1 missing · 1 coming up · 1 turned in")


class DueWhenLabelTests(unittest.TestCase):
    def test_labels_are_weekday_plus_date(self):
        today = datetime(2026, 9, 21).date()
        self.assertEqual(
            glance.due_when_label(datetime(2026, 9, 21).date(), today),
            "Due today · Monday, Sep 21",
        )
        self.assertEqual(
            glance.due_when_label(datetime(2026, 9, 22).date(), today),
            "Due tomorrow · Tuesday, Sep 22",
        )
        self.assertEqual(
            glance.due_when_label(datetime(2026, 9, 21).date(), today, kind="weekend"),
            "This weekend · due Monday, Sep 21",
        )
        self.assertEqual(
            glance.due_when_label(datetime(2026, 9, 19).date(), today),
            "2 days late · was due Saturday, Sep 19",
        )
        today_label = glance.due_when_label(today, today)
        self.assertNotIn("Coming", today_label)
        self.assertNotIn("tonight", today_label.lower())
        tomorrow = glance.due_when_label(datetime(2026, 9, 22).date(), today)
        self.assertIn("Tuesday", tomorrow)
        self.assertNotEqual(tomorrow.lower(), "due tomorrow")


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
        self.assertEqual(forecast["label"], "Due Friday, Sep 18")

    def test_forecast_today_is_due_today_not_coming_weekday(self):
        classes = [{
            "period": 5,
            "course_name": "Algebra 2",
            "mark": "B-",
            "percent": "81",
            "delta_7d": None,
            "upcoming": [],
            "missing": [],
            "classroom": {
                "announcements": [
                    {"text": "Test Monday in class. Bring a pencil.", "posted_on": "2026-09-19", "link": ""},
                ],
            },
        }]
        forecast = glance.pick_forecast(classes, datetime(2026, 9, 21))
        self.assertIsNotNone(forecast)
        self.assertEqual(forecast["label"], "Due today · Monday, Sep 21")
        self.assertNotIn("Coming", forecast["label"])
        self.assertNotIn("tonight", forecast["label"].lower())


class TonightSplitTests(unittest.TestCase):
    def test_due_today_test_note_goes_to_today_not_focus(self):
        student = {
            "name": "Student A",
            "sn": "1",
            "classes": [
                {"period": 5, "course_name": "Algebra 2", "teacher": "Byun", "percent": "76", "mark": "C"},
                {"period": 8, "course_name": "English", "teacher": "Lee", "percent": "72", "mark": "C"},
            ],
            "assignments_by_class": [
                {"class_name": "5- Algebra 2- Fall", "period": 5, "assignments": [
                    {"description": "LT 1.8", "due_date": "09/09/2026", "points_earned": 8,
                     "points_possible": 10, "grading_complete": True},
                ]},
                {"class_name": "8- English- Fall", "period": 8, "assignments": []},
            ],
            "class_trends": {},
            "ai_summary": {},
            "classroom": {
                "courses": [{
                    "name": "P5- Algebra 2",
                    "period": 5,
                    "items": [],
                }],
            },
        }
        student["classroom"] = {"courses": []}
        view = view_for(student, today=datetime(2026, 9, 21))
        view["classes"][0]["classroom"] = {
            "announcements": [{
                "text": "On Monday, you'll be working quietly in class after you're done with the test.",
                "posted_on": "2026-09-19",
                "link": "https://classroom.google.com/c/x/p/y",
            }],
            "classroom_only": [{
                "title": "A2- LT 2.4 Practice",
                "due_date": "09/18/2026",
                "state": "CREATED",
                "state_label": "Not turned in on Classroom",
            }],
        }
        g = glance.build_glance(view["classes"], datetime(2026, 9, 21))
        focus_titles = [i["title"] for i in g["tonight"]["items"]]
        self.assertNotIn("test", [t.lower() for t in focus_titles])
        self.assertEqual(g["tonight"]["today"], [])
        self.assertNotIn("A2- LT 2.4 Practice", focus_titles)
        self.assertFalse(any(i.get("kind") == "class" for i in g["tonight"]["items"]))
        self.assertFalse(any("lowest" in (i.get("label") or "").lower() for i in g["tonight"]["items"]))
        self.assertEqual(g["bands"]["due_soon"]["empty_line"], "Nothing due in the next 5 school days.")
        blob = json.dumps(g)
        self.assertNotIn("classroom.google.com", blob)
        self.assertNotIn("Student A", blob)

    def test_submitted_test_says_turned_in_and_stays_off_focus(self):
        classes = [{
            "period": 5,
            "course_name": "Algebra 2",
            "mark": "C",
            "percent": "76",
            "assignments": [{
                "name": "Chapter test",
                "due_date": "09/21/2026",
                "points_earned": None,
                "points_possible": 50,
                "classroom": {"state": "TURNED_IN", "state_label": "Turned in",
                              "turned_in_on": "2026-09-21"},
            }],
            "classroom": {
                "announcements": [{
                    "text": "Test Monday in class.",
                    "posted_on": "2026-09-19",
                }],
            },
        }, {
            "period": 8,
            "course_name": "English",
            "mark": "B",
            "percent": "88",
            "assignments": [],
            "classroom": {},
        }]
        g = glance.build_glance(classes, datetime(2026, 9, 21))
        focus_titles = [i["title"].lower() for i in g["tonight"]["items"]]
        self.assertFalse(any("test" in t for t in focus_titles))
        self.assertEqual(g["tonight"]["today"], [])
        self.assertNotIn("50", json.dumps(g["tonight"]))

    def test_due_soon_holds_the_five_school_day_window(self):
        classes = [{
            "period": 5,
            "course_name": "Algebra 2",
            "mark": "B-",
            "percent": "81",
            "assignments": [
                {"name": "Tomorrow set", "due_date": "09/15/2026", "points_earned": None},
                {"name": "Two-day set", "due_date": "09/16/2026", "points_earned": None},
                {"name": "Three-day set", "due_date": "09/17/2026", "points_earned": None},
                {"name": "Later unit", "due_date": "10/02/2026", "points_earned": None},
                {"name": "Old packet", "due_date": "09/10/2026", "points_earned": None},
                {"name": "Already in", "due_date": "09/15/2026", "points_earned": None,
                 "classroom": {"state": "TURNED_IN"}},
            ],
            "classroom": {},
        }]
        g = glance.build_glance(classes, MONDAY)
        titles = [i["title"] for i in g["tonight"]["items"]]
        self.assertEqual(titles, ["Tomorrow set", "Two-day set", "Three-day set"])
        self.assertEqual(g["tonight"]["later"], [])
        self.assertNotIn("Later unit", titles)
        self.assertNotIn("Old packet", titles)
        self.assertNotIn("Already in", titles)
        self.assertFalse(any(i.get("kind") == "class" for i in g["tonight"]["items"]))
        self.assertNotIn("lowest", json.dumps(g["tonight"]).lower())
        self.assertNotIn("still to do", json.dumps(g["bands"]).lower())
        self.assertEqual(g["bands"]["due_soon"]["subtitle"], "Not turned in, due in the next 5 school days")
        self.assertIn("Tuesday, Sep 15", g["tonight"]["items"][0]["label"])
        self.assertEqual(g["look_next"], "Algebra 2 has 1 past due on the class chip.")

    def test_focus_lists_every_due_soon_assignment(self):
        assignments = [
            {"name": f"Packet {n}", "due_date": "09/15/2026", "points_earned": None}
            for n in range(1, 6)
        ] + [
            {"name": f"Lab {n}", "due_date": "09/16/2026", "points_earned": None}
            for n in range(1, 3)
        ]
        classes = [{
            "period": 5,
            "course_name": "Algebra 2",
            "mark": "B-",
            "percent": "81",
            "assignments": assignments,
            "classroom": {},
        }]
        g = glance.build_glance(classes, MONDAY)
        titles = [i["title"] for i in g["tonight"]["items"]]
        self.assertEqual(
            titles,
            ["Packet 1", "Packet 2", "Packet 3", "Packet 4", "Packet 5", "Lab 1", "Lab 2"],
        )
        self.assertEqual(titles, [i["title"] for i in g["bands"]["focus"]["items"]])
        blob = json.dumps(g["bands"]["focus"])
        self.assertIsNone(re.search(r"and \d+ more", blob, re.I))
        self.assertNotIn("lowest", blob.lower())

    def test_empty_bands_are_omitted(self):
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
        self.assertEqual(g["bands"]["due_soon"]["items"], [])
        self.assertEqual(g["bands"]["due_soon"]["empty_line"], "Nothing due in the next 5 school days.")
        self.assertEqual(g["bands"]["due_soon"]["heading"], "Due soon")
        self.assertNotIn("coming up.", g["bands"]["due_soon"]["empty_line"].lower())
        self.assertNotIn("still to do", json.dumps(g["bands"]).lower())
        self.assertNotIn("turn_in", g["bands"])
        self.assertIsNone(g["bands"]["later"])
        self.assertIsNone(g["bands"]["today"])
        self.assertEqual(g["facts"], [])
        self.assertEqual(g.get("look_next") or "", "")
        self.assertTrue(g["tonight"]["empty"])

    def test_lowest_class_never_becomes_a_focus_line(self):
        classes = [{
            "period": 5,
            "course_name": "Algebra II Business Management",
            "mark": "C",
            "percent": "76",
            "assignments": [],
            "classroom": {},
        }, {
            "period": 8,
            "course_name": "English",
            "mark": "C",
            "percent": "72.8",
            "assignments": [],
            "classroom": {},
        }]
        g = glance.build_glance(classes, datetime(2026, 9, 21))
        home = json.dumps({
            "bands": g["bands"], "tonight": g["tonight"],
            "facts": g["facts"], "look_next": g["look_next"],
        }).lower()
        self.assertNotIn("lowest", home)
        self.assertNotIn("is the lowest class", home)
        self.assertFalse(any(i.get("kind") == "class" for i in g["tonight"]["items"]))
        self.assertEqual(g["bands"]["due_soon"]["empty_line"], "Nothing due in the next 5 school days.")
        self.assertIsNone(g["bands"]["later"])
        self.assertIsNone(g["bands"]["today"])
        self.assertNotIn("76%", home)
        self.assertEqual(g.get("facts") or [], [])
        self.assertEqual(g.get("look_next") or "", "")

    def test_fact_lines_combine_look_reasons_without_titles(self):
        classes = [{
            "period": 5,
            "course_name": "Algebra 2",
            "mark": "C",
            "percent": "76",
            "assignments": [
                {"name": "Old packet", "due_date": "09/10/2026", "points_earned": None},
                {"name": "Warmup", "due_date": "09/21/2026", "points_earned": None},
            ],
            "classroom": {},
        }, {
            "period": 8,
            "course_name": "English",
            "mark": "B",
            "percent": "88",
            "assignments": [
                {"name": "Essay", "due_date": "09/08/2026", "points_earned": None},
                {"name": "Draft", "due_date": "09/09/2026", "points_earned": None},
            ],
            "classroom": {},
        }]
        g = glance.build_glance(classes, datetime(2026, 9, 21))
        self.assertEqual(g.get("facts") or [], [])
        self.assertEqual(g["look_next"], "English has 2 past due on the class chip.")
        home = json.dumps({
            "bands": g["bands"], "tonight": g["tonight"],
            "facts": g["facts"], "look_next": g["look_next"],
        }).lower()
        self.assertNotIn("is the lowest class", home)
        self.assertNotIn("lowest", home)
        self.assertNotIn("76%", home)
        self.assertEqual([i["title"] for i in g["tonight"]["items"]], ["Warmup"])
        self.assertEqual(g["tonight"]["today"], [])
        self.assertIsNone(g["bands"]["later"])
        self.assertIsNone(g["bands"]["today"])


class DueSoonTests(unittest.TestCase):
    def test_tuesday_window_is_that_day_plus_next_four_school_days(self):
        days = glance.school_days_from(TUESDAY)
        self.assertEqual(
            [d.isoformat() for d in days],
            ["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28"],
        )
        self.assertTrue(glance.in_due_soon_window(datetime(2026, 9, 22).date(), TUESDAY))
        self.assertTrue(glance.in_due_soon_window(datetime(2026, 9, 28).date(), TUESDAY))
        self.assertFalse(glance.in_due_soon_window(datetime(2026, 9, 26).date(), TUESDAY))
        self.assertFalse(glance.in_due_soon_window(datetime(2026, 9, 29).date(), TUESDAY))

    def test_friday_includes_the_following_monday(self):
        days = glance.school_days_from(FRIDAY)
        self.assertEqual(
            [d.isoformat() for d in days],
            ["2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"],
        )
        self.assertTrue(glance.in_due_soon_window(datetime(2026, 9, 21).date(), FRIDAY))

    def test_saturday_starts_monday(self):
        days = glance.school_days_from(SATURDAY)
        self.assertEqual(
            [d.isoformat() for d in days],
            ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"],
        )
        sunday = glance.school_days_from(SUNDAY)
        self.assertEqual([d.isoformat() for d in sunday], [d.isoformat() for d in days])

    def test_turned_in_item_inside_window_is_absent(self):
        classes = [{
            "period": 5,
            "course_name": "Algebra 2",
            "mark": "B-",
            "percent": "81",
            "assignments": [{
                "name": "Warmup",
                "due_date": "09/22/2026",
                "points_earned": None,
                "classroom": {"state": "TURNED_IN", "turned_in_on": "2026-09-22"},
            }, {
                "name": "Returned quiz",
                "due_date": "09/23/2026",
                "points_earned": None,
                "classroom": {"state": "RETURNED"},
            }, {
                "name": "Scored set",
                "due_date": "09/24/2026",
                "points_earned": 9,
                "points_possible": 10,
            }, {
                "name": "Handed in",
                "due_date": "09/25/2026",
                "points_earned": None,
                "date_completed": "09/22/2026",
            }],
            "classroom": {},
        }]
        g = glance.build_glance(classes, TUESDAY)
        self.assertEqual([i["title"] for i in g["tonight"]["items"]], [])
        self.assertEqual(g["bands"]["due_soon"]["empty_line"], "Nothing due in the next 5 school days.")
        self.assertGreaterEqual(g["suppressed"], 4)

    def test_past_due_unfinished_item_is_absent(self):
        classes = [{
            "period": 6,
            "course_name": "Biology",
            "mark": "A",
            "percent": "93",
            "assignments": [{
                "name": "Old lab",
                "due_date": "09/18/2026",
                "points_earned": None,
            }],
            "classroom": {},
        }]
        g = glance.build_glance(classes, TUESDAY)
        self.assertEqual([i["title"] for i in g["tonight"]["items"]], [])
        self.assertEqual(g["look_next"], "Biology has 1 past due on the class chip.")
        bio = next(c for c in g["standing"] if c["course"] == "Biology")
        self.assertEqual(bio["counts"]["past_due"], 1)
        self.assertIn("past due", bio["count_line"])

    def test_pe_questionnaire_due_today_not_turned_in_is_present(self):
        classes = [{
            "period": 1,
            "course_name": "Engineering Geo",
            "mark": "A",
            "percent": "94",
            "assignments": [
                {"name": f"Geo {n}", "due_date": "09/22/2026", "points_earned": None}
                for n in range(1, 6)
            ],
            "classroom": {},
        }, {
            "period": 7,
            "course_name": "PE Course 1",
            "mark": "A",
            "percent": "100",
            "assignments": [],
            "classroom": {
                "classroom_only": [{
                    "title": "Coach C's PE Questionnaire",
                    "due_date": "09/22/2026",
                    "state": "CREATED",
                    "state_label": "Assigned",
                }],
            },
        }]
        g = glance.build_glance(classes, TUESDAY)
        titles = [i["title"] for i in g["tonight"]["items"]]
        self.assertIn("Coach C's PE Questionnaire", titles)
        self.assertEqual(sorted(titles), sorted([f"Geo {n}" for n in range(1, 6)] + ["Coach C's PE Questionnaire"]))
        self.assertEqual(len(titles), 6)
        self.assertGreater(len(titles), glance.BAND_LIMIT)
        self.assertEqual(titles, [i["title"] for i in g["bands"]["due_soon"]["items"]])
        pe = next(i for i in g["tonight"]["items"] if i["title"] == "Coach C's PE Questionnaire")
        self.assertIn("Tuesday, Sep 22", pe["label"])
        self.assertNotIn("BAND_LIMIT", json.dumps(g["bands"]["due_soon"]))

    def test_no_band_limit_slice(self):
        classes = [{
            "period": 5,
            "course_name": "Algebra 2",
            "mark": "B-",
            "percent": "81",
            "assignments": [
                {"name": f"Packet {n}", "due_date": "09/22/2026", "points_earned": None}
                for n in range(1, 9)
            ],
            "classroom": {},
        }]
        g = glance.build_glance(classes, TUESDAY)
        titles = [i["title"] for i in g["tonight"]["items"]]
        self.assertEqual(len(titles), 8)
        self.assertGreater(len(titles), glance.BAND_LIMIT)
        self.assertEqual(titles, [f"Packet {n}" for n in range(1, 9)])
        self.assertIsNone(g["bands"]["later"])
        self.assertIsNone(g["bands"]["today"])
        self.assertEqual(g["facts"], [])

    def test_look_next_names_outside_window_when_no_past_due(self):
        classes = [{
            "period": 8,
            "course_name": "English",
            "mark": "B",
            "percent": "88",
            "assignments": [{
                "name": "Editorial",
                "due_date": "10/02/2026",
                "points_earned": None,
            }],
            "classroom": {},
        }]
        g = glance.build_glance(classes, TUESDAY)
        self.assertEqual(g["tonight"]["items"], [])
        self.assertEqual(
            g["look_next"],
            "English Editorial is due Friday, Oct 2, on the class chip.",
        )
        self.assertEqual(glance.LOOK_NEXT_HEADING, "Where to look")
        self.assertNotIn("lowest", (g["look_next"] or "").lower())
        self.assertNotIn("ai_summary", json.dumps(g))
        self.assertNotIn("lowest class", (g["look_next"] or "").lower())


class TeacherProseCardTests(unittest.TestCase):
    def test_stamp_prose_reaches_the_card_and_title_only_stays_empty(self):
        student = {
            "name": "Student A",
            "sn": "1",
            "classes": [{"period": 5, "course_name": "Algebra 2", "teacher": "Byun", "percent": "81", "mark": "B-"}],
            "assignments_by_class": [
                {"class_name": "5- Algebra 2- Fall", "period": 5, "assignments": [
                    {"description": "LT 1.4", "due_date": "09/10/2026", "points_earned": 10,
                     "points_possible": 10, "grading_complete": True,
                     "classroom": {
                         "instructions": "Name the angle pairs and justify each one.\n\nA linear pair adds to 180.",
                         "description": "Name the angle pairs and justify each one.",
                         "excerpt": "A linear pair adds to 180.",
                         "topic": "Unit 2",
                     }},
                    {"description": "Warmup", "due_date": "09/12/2026", "points_earned": None,
                     "points_possible": 5, "grading_complete": False,
                     "classroom": {"topic": "Unit 2", "instructions": "Warmup"}},
                    {"description": "Notes check", "due_date": "09/08/2026", "points_earned": 5,
                     "points_possible": 5, "grading_complete": True},
                ]},
            ],
            "class_trends": {},
            "ai_summary": {},
            "classroom": {"courses": [], "classroom_only": []},
        }
        g = view_for(student, today=MONDAY)["glance"]
        alg = next(c for c in g["standing"] if c["course"] == "Algebra 2")
        by_name = {w["name"]: w for w in alg["drawer"]["work"]}
        self.assertIn("Name the angle pairs", by_name["LT 1.4"]["description"])
        self.assertIn("linear pair", by_name["LT 1.4"]["description"])
        self.assertEqual(by_name["Warmup"]["description"], "")
        self.assertEqual(by_name["Notes check"]["description"], "")
        self.assertNotIn("Unit 2", by_name["LT 1.4"]["description"])
        blob = json.dumps(alg["drawer"]["work"])
        self.assertNotIn("classroom.google.com", blob)
        self.assertNotIn("drive.google.com", blob)

    def test_classroom_only_card_gets_body_without_new_unmatched_cards(self):
        student = kid_fixture()
        export = json.loads((FIXTURES / "classroom_export_sample.json").read_text())
        g = view_for(student, export=export, today=MONDAY)["glance"]
        alg = next(c for c in g["standing"] if c["course"] == "Algebra 2")
        names = [w["name"] for w in alg["drawer"]["work"]]
        self.assertIn("Unit 3 Quiz Review", names)
        self.assertNotIn("Syllabus signature", names)
        self.assertNotIn("Unit 3 notes", names)
        quiz = next(w for w in alg["drawer"]["work"] if w["name"] == "Unit 3 Quiz Review")
        self.assertIn("review packet", quiz["description"])
        practice = next(w for w in alg["drawer"]["work"] if w["name"] == "3.2 Practice")
        self.assertIn("Show all work", practice["description"])
        self.assertIn("Rubric", practice["description"])


if __name__ == "__main__":
    unittest.main()
