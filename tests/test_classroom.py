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

    def test_maps_real_schedule_shapes(self):
        classes = [
            {"period": 0, "course_name": "Bus of Gaming", "teacher": "Licciardo, D"},
            {"period": 1, "course_name": "Mkt Adv GrphDes", "teacher": "Chung, L"},
            {"period": 2, "course_name": "Engineering Geo", "teacher": "Stadel, A"},
            {"period": 3, "course_name": "COMM 1 (IVC)", "teacher": "ROP, &"},
            {"period": 4, "course_name": "PE Course 1", "teacher": "Gray-Burrell, T"},
            {"period": 5, "course_name": "Eng 9 Entre", "teacher": "McCarty Ku, A"},
            {"period": 6, "course_name": "Tech of Biology", "teacher": "Johnson, D"},
            {"period": 7, "course_name": "Point Break", "teacher": "Healey, M"},
            {"period": 8, "course_name": "WrldHis by Dsgn", "teacher": "Healey, M"},
        ]
        courses = [
            {"id": "pe", "name": "Period 4 - Coach Gray's PE Class", "section": "9th Grade - High School"},
            {"id": "mkt", "name": "26-27 Marketing, Advertising & Graphic Design", "section": ""},
            {"id": "eng", "name": "Eng 9 Entre - McCarty Ku - 5", "section": "5"},
            {"id": "p8", "name": "Period 8", "section": "8"},
            {"id": "geo", "name": "2 • Geometry • Stadel", "section": "2"},
            {"id": "bio", "name": "Tech of Biology - Johnson - 6", "section": "6"},
            {"id": "pb", "name": "Healey - 7", "section": "7"},
            {"id": "bog", "name": "Bus of Gaming - Licciardo - 0", "section": "0"},
            {"id": "scuba", "name": "SCUBA Leaders 2026-27", "section": ""},
            {"id": "code", "name": "Coding Quarter 2", "section": ""},
        ]
        mapping = classroom.map_courses_to_classes(courses, classes)
        got = {cid: cm["period"] for cid, cm in mapping.items()}
        self.assertEqual(
            got, {"pe": 4, "mkt": 1, "eng": 5, "p8": 8, "geo": 2, "bio": 6, "pb": 7, "bog": 0}
        )

    def test_rescues_unique_weak_match(self):
        classes = [
            {"period": 2, "course_name": "Span 1 (IVC)", "teacher": "ROP, &"},
            {"period": 4, "course_name": "US Hist Media", "teacher": "Healey, M"},
        ]
        courses = [
            {"id": "sp", "name": "Intro to Spanish", "section": ""},
            {"id": "p4", "name": "Period 4", "section": "4"},
            {"id": "lma", "name": "LMA Counseling", "section": ""},
            {"id": "old", "name": "2025 Marketing, Advertising & Graphic Design", "section": ""},
        ]
        mapping = classroom.map_courses_to_classes(courses, classes)
        self.assertEqual({cid: cm["period"] for cid, cm in mapping.items()}, {"sp": 2, "p4": 4})

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

    def test_title_similarity_on_real_teacher_titles(self):
        sim = classroom.title_similarity
        # Date prefixes and "LT x.y -" prefixes are noise; the words match.
        self.assertGreaterEqual(
            sim("Aug. 24-28: Adobe Illustrator - Poster About Myself",
                "LT 1.1 - Poster of Myself in Adobe Illustrator"), 0.8)
        # Codes decide between #1 and #2.
        self.assertGreaterEqual(
            sim("Sept. 2-9: Multiview Sketching Practice #1", "Multiview Sketching Practice #1"), 0.9)
        self.assertLess(
            sim("Sept. 2-9: Multiview Sketching Practice #1", "Multiview Sketching Practice #2"), 0.6)
        # A code-only Aeries title matches the Classroom item carrying that code.
        self.assertGreaterEqual(sim("A2- LT 1.8 packet page 17-19 + pg 16 bottom half", "LT 1.8"), 0.8)
        self.assertLess(sim("A2- LT 1.8 packet page 17-19 + pg 16 bottom half", "LT 1.7"), 0.6)
        self.assertGreaterEqual(sim("Submit CYU • 1.1 • Geometry Definitions", "1.1 Definitions"), 0.8)
        self.assertLess(sim("Submit CYU • 1.3 • Transversal & Parallel Lines",
                            "1.3a Prove Vertical Angles are Congruent"), 0.6)
        self.assertGreaterEqual(sim("Mini Golf Course Design Submission Form", "Mini Golf Course Design"), 0.9)
        self.assertGreaterEqual(sim("Anatomy of a Game", "Anatomy of a Great Game"), 0.75)
        self.assertGreaterEqual(sim("Unit 2 Socratic Seminar Prep", "LT1 & LT2 Unit 2 Socratic Seminar"), 0.8)
        self.assertLess(sim("Gym Etiquette and Push, Pull, Legs Quiz", "Weight Room Rules Quiz"), 0.6)

    def test_codes_buy_due_date_slack(self):
        items = [
            {"type": "assignment", "title": "Submit CYU • 1.1 • Geometry Definitions", "due": "2026-08-29"},
            {"type": "assignment", "title": "Submit CYU • 1.3 • Transversal & Parallel Lines", "due": "2026-09-05"},
        ]
        rows = [
            {"description": "1.1 Definitions", "due_date": "09/09/2026"},
            {"description": "1.3a Prove Vertical Angles are Congruent", "due_date": "09/09/2026"},
        ]
        self.assertEqual(classroom.match_items_to_assignments(items, rows), {0: 0})

    def test_closest_due_date_wins_a_tie(self):
        items = [{"type": "assignment", "title": "Sept. 2-9: Multiview Sketching Practice #1", "due": "2026-09-09"}]
        rows = [
            {"description": "Multiview Sketching Practice #2", "due_date": "09/11/2026"},
            {"description": "Multiview Sketching Practice #1", "due_date": "09/09/2026"},
        ]
        self.assertEqual(classroom.match_items_to_assignments(items, rows), {0: 1})


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


FIXTURE_V2 = Path(__file__).parent / "fixtures" / "classroom_export_v2_sample.json"


def load_export_v2():
    return json.loads(FIXTURE_V2.read_text())


def geometry_student():
    return {
        "name": "Kid",
        "sn": "2",
        "classes": [
            {"period": 2, "course_name": "Engineering Geo", "teacher": "Stadel, A", "percent": "91", "mark": "A-"},
        ],
        "assignments_by_class": [
            {
                "class_name": "2- Engineering Geo- Fall",
                "period": 2,
                "assignments": [
                    {
                        "number": 4,
                        "description": "1.1 Definitions",
                        "due_date": "09/09/2026",
                        "points_earned": 9.0,
                        "points_possible": 10,
                        "percentage": 90.0,
                        "grading_complete": True,
                    }
                ],
            }
        ],
        "class_trends": {},
        "ai_summary": {},
    }


class ExportV2ShapeTests(unittest.TestCase):
    """Script v3 over-fetches; classroom.py must keep the extra fields, not drop them."""

    def setUp(self):
        self.student = geometry_student()
        self.block = classroom.attach_classroom(
            self.student, load_export_v2(), TODAY, scraper.assignments_for_class, overrides={}
        )
        self.geo = self.block["courses"][0]
        self.items = {i["id"]: i for i in self.geo["items"]}

    def test_block_carries_script_version_drive_index_and_text_stats(self):
        self.assertEqual(self.block["export_version"], 2)
        self.assertEqual(self.block["script_version"], "3.0")
        self.assertEqual(self.block["doc_text"], {
            "total": 3, "with_text": 2, "reused": 1, "failed": 0, "skipped_time_budget": 1,
        })
        folders = self.block["drive_folders"]
        self.assertEqual([f["name"] for f in folders], ["2 • Geometry • Stadel"])
        self.assertTrue(folders[0]["current_year"])
        self.assertEqual(self.block["school_year"], "2026-27")
        kid_file = folders[0]["files"][0]
        self.assertEqual(kid_file["id"], "d-kid-11")
        self.assertEqual(kid_file["title"], "CYU 1.1")
        self.assertTrue(kid_file["owned_by_student"])
        self.assertEqual(kid_file["modified_at"], "2026-08-29T03:00:00.000Z")
        self.assertEqual(self.block["notes"], ["rubrics: not exposed by the Classroom service"])

    def test_course_carries_teachers_topics_and_description(self):
        self.assertEqual(self.geo["aeries_period"], 2)
        self.assertEqual(self.geo["teachers"], [
            {"name": "A. Stadel", "is_owner": True},
            {"name": "Co Teacher", "is_owner": False},
        ])
        self.assertEqual(self.geo["topics"], [{"id": "tp-1", "name": "Unit 1: Foundations"}])
        self.assertEqual(self.geo["room"], "B204")
        self.assertEqual(self.geo["description_heading"], "Engineering Geometry, Period 2")
        self.assertIn("Late work accepted", self.geo["description"])

    def test_item_keeps_rubric_category_state_and_material_ids(self):
        cyu = self.items["w-cyu13"]
        self.assertEqual(cyu["work_type"], "ASSIGNMENT")
        self.assertEqual(cyu["state"], "PUBLISHED")
        self.assertEqual(cyu["grade_category"], {"name": "Practice", "weight": 200000, "default_denominator": 10})
        rubric = cyu["rubric"]
        self.assertEqual([c["title"] for c in rubric["criteria"]], ["Angle pairs named", "Work shown"])
        self.assertEqual([c["max_points"] for c in rubric["criteria"]], [6, 4])
        self.assertEqual(rubric["max_points"], 10)
        self.assertEqual(rubric["criteria"][0]["levels"][1], {"title": "Partial", "description": "Some missing", "points": 3})
        self.assertNotIn("id", rubric)
        doc = cyu["materials"][0]
        self.assertEqual(doc["id"], "d-cyu13")
        self.assertEqual(doc["share_mode"], "VIEW")
        self.assertEqual(doc["modified_at"], "2026-09-09T20:00:00.000Z")
        self.assertNotIn("owned_by_student", doc)  # false is omitted, true is kept
        self.assertEqual(cyu["materials"][1]["kind"], "youtube")
        self.assertIn("alternate interior", cyu["instructions"])
        # Skipped-for-time note survives so the scraper can fetch the text itself later.
        syllabus = self.items["m-syll"]["materials"][0]
        self.assertEqual(syllabus["note"], "text skipped: time budget")
        self.assertNotIn("text_excerpt", syllabus)

    def test_submission_keeps_history_draft_grade_answer_and_owned_attachment(self):
        done = self.items["w-cyu11"]["submission"]
        self.assertEqual(done["state"], "RETURNED")
        self.assertEqual(done["turned_in_on"], "2026-08-28")
        self.assertEqual(done["assigned_grade"], 9)
        self.assertEqual(done["draft_grade"], 9)
        self.assertEqual(done["updated_on"], "2026-09-05")
        kinds = [(h["kind"], h.get("state", h.get("points_earned"))) for h in done["history"]]
        self.assertEqual(kinds, [("state", "CREATED"), ("state", "TURNED_IN"), ("grade", 9), ("state", "RETURNED")])
        self.assertEqual(done["history"][2]["max_points"], 10)
        self.assertEqual(done["history"][2]["on"], "2026-09-05")
        att = done["attachments"][0]
        self.assertEqual(att["id"], "d-kid-11")
        self.assertTrue(att["owned_by_student"])
        self.assertEqual(att["title"], "CYU 1.1")
        self.assertIn("A point has no dimension", att["text_excerpt"])
        question = self.items["q-1"]
        self.assertEqual(question["type"], "question")
        self.assertEqual(question["choices"], ["Alternate interior", "Same-side interior", "Linear pair"])
        self.assertEqual(question["submission"]["answer"], "Alternate interior")
        self.assertEqual(question["submission"]["history"][0]["state"], "TURNED_IN")

    def test_aeries_row_stamp_gains_rubric_and_category(self):
        # 1.1 matched the Aeries "1.1 Definitions" row; the new fields ride along
        # only when present, so the stamp of a v1 export is unchanged.
        row = self.student["assignments_by_class"][0]["assignments"][0]
        self.assertEqual(row["classroom"]["id"], "w-cyu11")
        self.assertEqual(row["classroom"]["grade_category"], "Practice")
        self.assertNotIn("rubric", row["classroom"])
        cyu = self.items["w-cyu13"]
        stamp = classroom._assignment_link_payload(cyu)
        self.assertEqual([c["title"] for c in stamp["rubric"]], ["Angle pairs named", "Work shown"])
        self.assertEqual(stamp["rubric"][0]["max_points"], 6)
        self.assertEqual(stamp["rubric"][0]["levels"][1]["title"], "Partial")
        self.assertIn("alternate interior", stamp["materials"][0]["text_excerpt"])
        self.assertEqual(stamp["grade_category_weight"], 200000)

    def test_class_context_and_grok_see_rubric_and_teachers(self):
        geo_meta = self.student["classes"][0]
        rows = scraper.assignments_for_class(self.student, geo_meta)
        ctx = classroom.class_context(self.student, geo_meta, TODAY, assignments=rows)
        self.assertEqual(ctx["teachers"], ["A. Stadel", "Co Teacher"])
        self.assertEqual(ctx["topics"], ["Unit 1: Foundations"])
        self.assertEqual(ctx["current_topic"], "Unit 1: Foundations")
        self.assertEqual(ctx["school_year"], "2026-27")
        self.assertEqual(ctx["drive"]["name"], "2 • Geometry • Stadel")
        self.assertEqual(ctx["drive"]["files"][0]["title"], "CYU 1.1")
        self.assertTrue(ctx["drive"]["files"][0]["owned_by_student"])
        self.assertEqual(ctx["materials"][0]["title"], "Syllabus")
        upcoming = next(e for e in ctx["classroom_only"] if e["id"] == "w-cyu13")
        self.assertEqual(upcoming["days_until_due"], 1)
        self.assertEqual(upcoming["grade_category"], "Practice")
        self.assertEqual([c["title"] for c in upcoming["rubric"]], ["Angle pairs named", "Work shown"])
        self.assertEqual(upcoming["materials"][0]["kind"], "drive")
        g = classroom.grok_context(ctx)
        first = g["classroom_only_upcoming"][0]
        self.assertEqual(first["rubric_criteria"], ["Angle pairs named", "Work shown"])
        self.assertNotIn("http", json.dumps(g))

    def test_no_student_name_anywhere_in_v2_payload(self):
        dumped = json.dumps(self.student)
        self.assertNotIn("Firstname", dumped)
        self.assertNotIn("Lastname", dumped)

    def test_v1_export_has_no_v2_keys(self):
        student = student_with_aeries()
        block = attach(student)
        alg = next(c for c in block["courses"] if c["id"] == "c-alg")
        for key in ("teachers", "topics", "room", "description"):
            self.assertNotIn(key, alg)
        practice = next(i for i in alg["items"] if i["id"] == "w-32")
        for key in ("rubric", "grade_category", "work_type", "state", "choices"):
            self.assertNotIn(key, practice)
        for key in ("history", "draft_grade", "answer"):
            self.assertNotIn(key, practice["submission"])
        for key in ("drive_folders", "doc_text"):
            self.assertNotIn(key, block)

    def test_compact_rubric_edge_cases(self):
        self.assertIsNone(classroom.compact_rubric(None))
        self.assertIsNone(classroom.compact_rubric({"criteria": []}))
        r = classroom.compact_rubric({"criteria": [{"title": "Only title", "levels": []}]})
        self.assertEqual(r, {"criteria": [{"title": "Only title", "description": "", "max_points": None, "levels": []}],
                             "max_points": None})


class SchoolYearWindowTests(unittest.TestCase):
    def test_bounds_include_pre_first_day_window(self):
        year_id, start, end = classroom.school_year_bounds(TODAY)
        self.assertEqual(year_id, "2026-27")
        self.assertEqual(start.isoformat(), "2026-07-23")
        self.assertEqual(end.isoformat(), "2027-05-28")

    def test_old_year_items_and_folders_drop_from_product(self):
        export = load_export_v2()
        export["courses"][0]["items"].append({
            "id": "w-old-year",
            "type": "assignment",
            "title": "Last year final",
            "due": "2025-05-20",
            "assigned_at": "2025-05-01T15:00:00.000Z",
            "updated_at": "2025-05-20T15:00:00.000Z",
            "materials": [],
            "submission": {"state": "RETURNED", "late": False},
        })
        student = geometry_student()
        block = classroom.attach_classroom(
            student, export, TODAY, scraper.assignments_for_class, overrides={}
        )
        ids = [i["id"] for i in block["courses"][0]["items"]]
        self.assertNotIn("w-old-year", ids)
        self.assertIn("w-cyu13", ids)
        self.assertIn("m-syll", ids)
        self.assertEqual([f["name"] for f in block["drive_folders"]], ["2 • Geometry • Stadel"])
        self.assertEqual(block["school_year"], "2026-27")

    def test_undated_item_stays(self):
        self.assertTrue(classroom.item_in_school_year(
            {"title": "No dates"}, date(2026, 7, 23), date(2027, 5, 28)
        ))

    def test_drive_folder_flag_beats_created_at(self):
        start, end = date(2026, 7, 23), date(2027, 5, 28)
        self.assertTrue(classroom.drive_folder_in_year(
            {"current_year": True, "created_at": "2025-08-12T15:00:00.000Z"}, start, end
        ))
        self.assertFalse(classroom.drive_folder_in_year(
            {"current_year": False, "created_at": "2026-08-12T15:00:00.000Z"}, start, end
        ))


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
        self.assertEqual(view["classroom_year"], "2026-27")
        self.assertEqual(view["classroom_unmatched"], ["Robotics Club"])

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
