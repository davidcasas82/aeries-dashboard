import json
import os
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import classroom  # noqa: E402
import scraper  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "classroom_export_sample.json"
TODAY = datetime(2026, 9, 14)  # Monday


def load_export():
    return json.loads(FIXTURE.read_text())


def student_with_aeries():
    return {
        "name": "Kid",
        "sn": "1",
        "classes": [
            {"period": 4, "course_name": "US History", "teacher": "Healey", "percent": "88", "mark": "B+"},
            {"period": 5, "course_name": "Algebra 2", "teacher": "Byun", "percent": "81", "mark": "B-"},
            {"period": 6, "course_name": "Biology", "teacher": "Chung", "percent": "93", "mark": "A"},
        ],
        "assignments_by_class": [
            {
                "class_name": "4- US History- Fall",
                "period": 4,
                "assignments": [
                    {
                        "number": 3,
                        "description": "DBQ Outline",
                        "due_date": "09/11/2026",
                        "points_earned": 13.0,
                        "points_possible": 15,
                        "percentage": 86.7,
                        "grading_complete": True,
                    }
                ],
            },
            {
                "class_name": "5- Algebra 2- Fall",
                "period": 5,
                "assignments": [
                    {
                        "number": 7,
                        "description": "3.2 Practice",
                        "due_date": "09/10/2026",
                        "points_earned": None,
                        "points_possible": 20,
                        "grading_complete": True,
                        "aeries_missing": True,
                        "status": "missing",
                    },
                    {
                        "number": 6,
                        "description": "3.1 Practice",
                        "due_date": "09/08/2026",
                        "points_earned": 18.0,
                        "points_possible": 20,
                        "percentage": 90.0,
                        "grading_complete": True,
                    },
                ],
            },
            {"class_name": "6- Biology- Fall", "period": 6, "assignments": []},
        ],
        "class_trends": {},
        "ai_summary": {},
    }


def attach(student, export=None):
    return classroom.attach_classroom(
        student, export or load_export(), TODAY, scraper.assignments_for_class, overrides={}
    )


class CourseMappingTests(unittest.TestCase):
    def test_period_parsing(self):
        self.assertEqual(classroom.classroom_period({"name": "P5- Alg 2 Byun Period 5"}), 5)
        self.assertEqual(classroom.classroom_period({"name": "US Hist Media - Healey - 4 4"}), 4)
        self.assertEqual(classroom.classroom_period({"name": "Tech of Biology - Period 6 Period 6"}), 6)
        self.assertEqual(classroom.classroom_period({"name": "English 9", "section": "Period 2"}), 2)
        self.assertIsNone(classroom.classroom_period({"name": "Robotics Club"}))

    def test_maps_by_teacher_period_and_name(self):
        student = student_with_aeries()
        mapping = classroom.map_courses_to_classes(load_export()["courses"], student["classes"])
        self.assertEqual(mapping["c-alg"]["period"], 5)
        self.assertEqual(mapping["c-hist"]["period"], 4)
        self.assertNotIn("c-club", mapping)

    def test_override_wins(self):
        student = student_with_aeries()
        mapping = classroom.map_courses_to_classes(
            load_export()["courses"], student["classes"], overrides={"c-club": 6}
        )
        self.assertEqual(mapping["c-club"]["course_name"], "Biology")

    def test_one_aeries_class_per_course(self):
        classes = [
            {"period": 1, "course_name": "Spanish 2", "teacher": "Ruiz"},
            {"period": 2, "course_name": "Spanish 2", "teacher": "Ruiz"},
        ]
        courses = [
            {"id": "a", "name": "Spanish 2 Ruiz", "section": "Period 2"},
            {"id": "b", "name": "Spanish 2 Ruiz", "section": "Period 1"},
        ]
        mapping = classroom.map_courses_to_classes(courses, classes)
        self.assertEqual(mapping["a"]["period"], 2)
        self.assertEqual(mapping["b"]["period"], 1)


class TextAndDateTests(unittest.TestCase):
    def test_strip_student_prefix(self):
        self.assertEqual(
            classroom.strip_student_name("Firstname Lastname - Section 3.2 Practice"),
            "Section 3.2 Practice",
        )
        self.assertEqual(classroom.strip_student_name("Section 3.2 Practice"), "Section 3.2 Practice")
        # A course-like prefix with digits is not a name
        self.assertEqual(classroom.strip_student_name("P5 Alg - Quiz Review"), "P5 Alg - Quiz Review")
        self.assertEqual(
            classroom.strip_student_name("Firstname Lastname: Lab Notes", names=["Firstname"]),
            "Lab Notes",
        )
        self.assertEqual(
            classroom.strip_student_name("Lab Notes (Firstname)", names=["Firstname"]),
            "Lab Notes",
        )

    def test_due_converts_utc_instant_to_pacific_day(self):
        self.assertEqual(classroom.due_to_pacific_date("2026-09-11T06:59:00.000Z"), date(2026, 9, 10))
        self.assertEqual(classroom.due_to_pacific_date("2026-09-11"), date(2026, 9, 11))
        self.assertIsNone(classroom.due_to_pacific_date(""))

    def test_title_similarity(self):
        self.assertGreaterEqual(classroom.title_similarity("Section 3.2 Practice", "3.2 Practice"), 0.75)
        self.assertLess(classroom.title_similarity("DBQ Outline", "Unit 3 Quiz Review"), 0.6)


class AttachTests(unittest.TestCase):
    def test_matches_items_to_aeries_rows(self):
        student = student_with_aeries()
        block = attach(student)
        self.assertEqual(block["unmatched_courses"], ["Robotics Club"])
        alg = next(c for c in block["courses"] if c["id"] == "c-alg")
        self.assertEqual(alg["aeries_period"], 5)
        practice = next(i for i in alg["items"] if i["id"] == "w-32")
        self.assertEqual(practice["aeries_match"]["number"], 7)
        self.assertEqual(practice["due_date"], "09/10/2026")
        self.assertIn("Show all work", practice["instructions"])
        self.assertIn("Rubric", practice["instructions"])

        row = student["assignments_by_class"][1]["assignments"][0]
        self.assertEqual(row["classroom"]["state"], "TURNED_IN")
        self.assertEqual(row["classroom"]["state_label"], "Turned in on Classroom")
        self.assertEqual(row["classroom"]["turned_in_on"], "2026-09-10")
        self.assertTrue(row["classroom"]["link"].startswith("https://classroom.google.com/"))

        hist_row = student["assignments_by_class"][0]["assignments"][0]
        self.assertTrue(hist_row["classroom"]["late"])

    def test_student_name_never_reaches_payload(self):
        student = student_with_aeries()
        attach(student)
        dumped = json.dumps(student)
        self.assertNotIn("Firstname", dumped)
        self.assertNotIn("Lastname", dumped)

    def test_reattach_clears_stale_stamps(self):
        student = student_with_aeries()
        attach(student)
        export = load_export()
        export["courses"] = [c for c in export["courses"] if c["id"] != "c-alg"]
        attach(student, export)
        row = student["assignments_by_class"][1]["assignments"][0]
        self.assertNotIn("classroom", row)


class ClassContextTests(unittest.TestCase):
    def setUp(self):
        self.student = student_with_aeries()
        attach(self.student)
        self.alg = self.student["classes"][1]
        self.alg_rows = scraper.assignments_for_class(self.student, self.alg)

    def test_signals(self):
        ctx = classroom.class_context(self.student, self.alg, TODAY, assignments=self.alg_rows)
        self.assertEqual(ctx["course_name"], "P5- Alg 2 Byun Period 5")
        only = ctx["classroom_only"]
        self.assertEqual([e["title"] for e in only], ["Unit 3 Quiz Review"])
        self.assertEqual(only[0]["days_until_due"], 1)
        self.assertEqual(only[0]["due_date"], "09/15/2026")
        self.assertEqual(only[0]["state_label"], "Not turned in on Classroom")
        self.assertEqual(
            [e["title"] for e in ctx["turned_in_aeries_missing"]], ["Section 3.2 Practice"]
        )
        self.assertEqual(ctx["not_started_due_soon"], [{"title": "Unit 3 Quiz Review", "days_until_due": 1}])
        self.assertEqual(len(ctx["announcements"]), 1)
        self.assertIn("Quiz Friday", ctx["announcements"][0]["text"])
        self.assertEqual(ctx["materials"][0]["title"], "Unit 3 notes")

    def test_old_unsubmitted_work_stays_out_of_window(self):
        ctx = classroom.class_context(self.student, self.alg, TODAY, assignments=self.alg_rows)
        self.assertNotIn("Syllabus signature", [e["title"] for e in ctx["classroom_only"]])

    def test_class_without_course_has_no_context(self):
        bio = self.student["classes"][2]
        self.assertIsNone(classroom.class_context(self.student, bio, TODAY, assignments=[]))

    def test_grok_context_has_no_links(self):
        ctx = classroom.class_context(self.student, self.alg, TODAY, assignments=self.alg_rows)
        g = classroom.grok_context(ctx)
        self.assertNotIn("http", json.dumps(g))
        self.assertEqual(g["turned_in_on_classroom_but_aeries_missing"], ["Section 3.2 Practice"])
        self.assertEqual(g["classroom_only_upcoming"][0]["title"], "Unit 3 Quiz Review")


class ScraperIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.student = student_with_aeries()
        scraper.attach_classroom_export(self.student, load_export(), today=TODAY)

    def _patched(self):
        return (
            patch.object(scraper, "pacific_today_dt", return_value=TODAY),
            patch.object(scraper, "pacific_today", return_value=TODAY.date()),
        )

    def test_analytics_carry_classroom_facts_and_tonight_uses_classroom_only_work(self):
        p1, p2 = self._patched()
        with p1, p2:
            analytics = scraper.build_class_analytics(self.student)
        alg = next(c for c in analytics["classes"] if c["period"] == 5)
        self.assertIn("classroom", alg)
        self.assertEqual(alg["classroom"]["turned_in_on_classroom_but_aeries_missing"], ["Section 3.2 Practice"])
        quiz = next(u for u in alg["upcoming"] if u["name"] == "Unit 3 Quiz Review")
        self.assertEqual(quiz["source"], "classroom")
        self.assertEqual(quiz["days_until_due"], 1)
        missing = alg["missing_assignments"][0]
        self.assertEqual(missing["classroom"]["state_label"], "Turned in on Classroom")
        self.assertTrue(missing["turned_in_classroom"])
        self.assertIn("Classroom", analytics["data_scope"])
        # Handed in on Classroom is not a to-do even though Aeries still flags it missing,
        # so the Classroom-only review due tomorrow is what is left for tonight.
        items = analytics["tonight_plan"]["items"]
        self.assertEqual([i["name"] for i in items], ["Unit 3 Quiz Review"])
        self.assertEqual(items[0]["source"], "classroom")
        self.assertEqual(items[0]["reason"], "due_tomorrow")

    def test_aeries_missing_without_classroom_turn_in_stays_a_todo(self):
        student = student_with_aeries()
        export = load_export()
        practice = next(i for i in export["courses"][0]["items"] if i["id"] == "w-32")
        practice["submission"]["state"] = "CREATED"
        practice["submission"]["turned_in_at"] = ""
        scraper.attach_classroom_export(student, export, today=TODAY)
        p1, p2 = self._patched()
        with p1, p2:
            analytics = scraper.build_class_analytics(student)
        names = [i["name"] for i in analytics["tonight_plan"]["items"]]
        self.assertEqual(names[0], "3.2 Practice")
        alg = next(c for c in analytics["classes"] if c["period"] == 5)
        self.assertEqual(alg["classroom"]["turned_in_on_classroom_but_aeries_missing"], [])

    def test_view_exposes_classroom_block_per_class(self):
        p1, p2 = self._patched()
        with p1, p2:
            view = scraper.build_student_view(self.student)
        alg = next(c for c in view["classes"] if c["period"] == 5)
        self.assertEqual(alg["classroom"]["classroom_only"][0]["title"], "Unit 3 Quiz Review")
        self.assertEqual(alg["missing"][0]["classroom"]["state"], "TURNED_IN")
        bio = next(c for c in view["classes"] if c["period"] == 6)
        self.assertIsNone(bio["classroom"])
        self.assertEqual(view["classroom_captured_at"], "2026-09-14T05:10:00.000Z")

    def test_no_export_is_a_no_op(self):
        student = student_with_aeries()
        self.assertIsNone(scraper.attach_classroom_export(student, None, today=TODAY))
        self.assertNotIn("classroom", student)

    def test_fetch_without_service_account_is_empty(self):
        with patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_JSON": ""}):
            self.assertEqual(classroom.fetch_classroom_exports(), {})
        with patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_JSON": "not json"}):
            self.assertEqual(classroom.fetch_classroom_exports(), {})


if __name__ == "__main__":
    unittest.main()
