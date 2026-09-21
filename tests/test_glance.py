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
        labels = [(i["weekend_day"], i["label"], i["title"]) for i in weekend["items"]]
        self.assertEqual(labels, [
            ("Saturday", "This weekend · due Saturday, Sep 19", "Lab reflection"),
            ("Sunday", "This weekend · due Sunday, Sep 20", "Reading draft"),
            ("Monday", "This weekend · due Monday, Sep 21", "Practice set 2"),
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
            row = g["tonight"]["weekend"]["items"][0]
            self.assertEqual(row["weekend_day"], "Monday")
            self.assertEqual(row["label"], "This weekend · due Monday, Sep 21")
            self.assertEqual(row["title"], "Practice set 2")
            self.assertNotIn("due tonight", row["label"].lower())
            self.assertNotIn("Coming", row["label"])


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
        self.assertNotIn("link", forecast["drawer"])
        self.assertEqual(forecast["label"], "Due Friday, Sep 18")
        self.assertNotIn("Coming", forecast["label"])

    def test_drawer_has_teacher_wording_and_statuses_without_classroom_url(self):
        quiz = next(i for i in self.g["tonight"]["items"] if i["title"] == "Unit 3 Quiz Review")
        d = quiz["drawer"]
        self.assertIn("review packet", d["teacher_words"])
        self.assertIn("Classroom", d["classroom"] + "x")
        self.assertTrue(d["classroom"])
        self.assertTrue(d["aeries"])
        self.assertNotIn("link", d)
        self.assertNotIn("work", d)
        blob = json.dumps(self.g)
        self.assertNotIn("classroom.google.com", blob)
        self.assertNotIn("drive.google.com", blob)
        self.assertNotIn("Open in Classroom", blob)

    def test_standing_drawer_lists_all_class_work(self):
        alg = next(c for c in self.g["standing"] if c["course"] == "Algebra 2")
        self.assertEqual(alg["drawer"]["kicker"], "0 missing · 1 not turned in · 2 turned in")
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
        self.assertEqual(quiz["bucket"], "not_turned_in")
        self.assertIn("review packet", quiz["description"])
        self.assertEqual(quiz["when"], "Due tomorrow · Tuesday, Sep 15")
        names = [w["name"] for w in alg["drawer"]["work"]]
        self.assertLess(names.index("3.1 Practice"), names.index("Unit 3 Quiz Review"))
        self.assertLess(names.index("3.2 Practice"), names.index("Unit 3 Quiz Review"))
        self.assertEqual(alg["count_line"], "1 not turned in · 2 turned in")
        self.assertEqual(alg["drawer"]["kicker"], "0 missing · 1 not turned in · 2 turned in")
        blob = json.dumps(alg["drawer"]["work"])
        for banned in ("rubric", "drive", "text_excerpt", "materials", "attachments"):
            self.assertNotIn(banned, blob)

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
        self.assertEqual(row["bucket"], "not_turned_in")
        self.assertEqual(row["when"], "4 days late · was due Thursday, Sep 10")
        scored = next(w for w in bio["drawer"]["work"] if w["name"] == "Cell lab")
        self.assertEqual(scored["score"], "28/30")
        self.assertEqual(scored["bucket"], "turned_in")
        self.assertEqual(scored["when"], "Due Saturday, Sep 12")
        names = [w["name"] for w in bio["drawer"]["work"]]
        self.assertLess(names.index("Cell lab"), names.index("Microscope sketch"))
        self.assertEqual(bio["count_line"], "1 not turned in · 1 turned in")

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
        tonight_labels = [i["label"] for i in g["tonight"]["items"]]
        self.assertTrue(any(l.startswith("Due today · Monday, Sep 14") for l in tonight_labels))
        self.assertTrue(any("Due tomorrow · Tuesday, Sep 15" == l for l in tonight_labels))
        self.assertFalse(any(l.lower() in ("due tomorrow", "coming monday", "due tonight") for l in tonight_labels))
        self.assertEqual(by_name["Notes check"]["status"], "Aeries missing")
        self.assertEqual(by_name["Notes check"]["bucket"], "turned_in")
        self.assertEqual(by_name["Warmup"]["bucket"], "not_turned_in")
        self.assertEqual(alg["count_line"], "2 not turned in · 1 turned in")
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
        self.assertEqual(names[-1], "Still out")
        self.assertEqual(alg["count_line"], "1 missing · 2 turned in")
        self.assertEqual(alg["drawer"]["kicker"], "1 missing · 2 turned in")
        tonight_titles = [i["title"] for i in g["tonight"]["items"]]
        self.assertNotIn("Classroom in", tonight_titles)
        self.assertNotIn("Aeries handed in", tonight_titles)


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


if __name__ == "__main__":
    unittest.main()
