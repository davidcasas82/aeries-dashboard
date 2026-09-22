#!/usr/bin/env python3
"""One-off grok-4 look-next draft. Does not publish. Does not change the page.

No student names or student numbers in stdout or output files.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import glance  # noqa: E402
import scraper  # noqa: E402

OUT = Path(os.environ.get("LOOK_NEXT_OUT", "/tmp/look-next-draft.json"))

LOOK_NEXT_SYSTEM = """You write the Where to look paragraph under Due soon on a parent grade dashboard.

Today's date and timezone are in PRECOMPUTED_ANALYTICS. Trust LOOK_NEXT_FACTS for what the page already shows.

Write one short paragraph (1–3 sentences) that is more useful than a single past-due count. Connect facts already in LOOK_NEXT_FACTS:
- what is in Due soon (class, title, weekday date)
- what is past due on a class chip (class and count; you may name titles)
- what is unsubmitted and due outside the next 5 school days (class, title, weekday date)

Rules:
- Facts only. Do not invent a cause, a reason, a skill, effort, or psychology.
- Never write a “lowest class, at N%” sentence. Do not rank classes by grade.
- A turned-in, returned, scored, or date-completed item is not still coming up. Do not describe a finished test as upcoming.
- Do not name a student. Third person, class and assignment titles only.
- Do not add Classroom or Drive links.
- Prefer empty string over filler if LOOK_NEXT_FACTS has nothing to say.

Respond ONLY with JSON: {"paragraph": "..." }"""


def _redact(data):
    for i, student in enumerate(data.get("students") or []):
        student["name"] = f"Student {i}"
        student.pop("sn", None)
        student.pop("student_number", None)
    return data


def _look_next_facts(view_classes, today):
    today_d = glance._as_date(today)
    window = set(glance.school_days_from(today_d))
    due_soon, _suppressed = glance.collect_due_soon(view_classes, today_d)
    past = []
    outside = []
    for cls in view_classes or []:
        course = (cls.get("course_name") or "").strip()
        if not course:
            continue
        past_titles = []
        for item in glance._class_raw_work(cls):
            name = glance._work_name(item)
            if not name or glance._work_done(item):
                continue
            due = glance._work_due_key(item)
            if glance._work_bucket(item, today_d) == "past_due":
                past_titles.append({
                    "title": name,
                    "due": glance._weekday_date(due) if due else "",
                })
            elif due is not None and due > today_d and due not in window:
                outside.append({
                    "course": course,
                    "title": name,
                    "due": glance._weekday_date(due),
                })
        if past_titles:
            past.append({"course": course, "count": len(past_titles), "items": past_titles})
    past.sort(key=lambda r: (-r["count"], r["course"].lower()))
    outside.sort(key=lambda r: (r["due"], r["course"].lower(), r["title"].lower()))
    return {
        "due_soon": [
            {"course": r.get("course"), "title": r.get("title"), "due": r.get("label")}
            for r in due_soon
        ],
        "past_due_on_chips": past,
        "outside_five_school_days": outside,
        "deterministic_line": glance.look_next_paragraph(view_classes, today_d),
    }


def _load_latest():
    token = scraper.family_unlock()
    payload = scraper.family_request("GET", "/v1/grades/latest", token)
    latest = payload.get("latest") or payload
    return _redact(latest)


def _analytics(student, today):
    student = dict(student)
    student["name"] = "Student"
    student.pop("sn", None)
    # Freeze "today" for analytics the same way glance does.
    with (
        __import__("unittest.mock").mock.patch.object(scraper, "pacific_today", return_value=today.date()),
        __import__("unittest.mock").mock.patch.object(scraper, "pacific_today_dt", return_value=today),
    ):
        analytics = scraper.build_class_analytics(student, history_context=None)
    analytics["student_name"] = "Student"
    return analytics


def _complete(facts, analytics):
    key = os.environ.get("GROK_API_KEY") or scraper.GROK_API_KEY
    if not key:
        raise SystemExit("GROK_API_KEY is not set")
    user = (
        "PRECOMPUTED_ANALYTICS (source of truth — do not redo arithmetic):\n"
        f"{json.dumps(analytics, indent=2)}\n\n"
        "LOOK_NEXT_FACTS (what the live page already shows):\n"
        f"{json.dumps(facts, indent=2)}\n\n"
        "Write the Where to look paragraph from these facts only."
    )
    resp = requests.post(
        scraper.GROK_API_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json={
            "model": scraper.GROK_MODEL,
            "messages": [
                {"role": "system", "content": LOOK_NEXT_SYSTEM},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.3,
            "max_tokens": 400,
        },
        timeout=120,
    )
    if resp.status_code != 200:
        raise SystemExit(f"Grok API returned {resp.status_code}")
    content = resp.json()["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    paragraph = (parsed.get("paragraph") or "").strip()
    low = paragraph.lower()
    if "lowest class" in low:
        raise SystemExit("Grok wrote a lowest-class sentence")
    return paragraph


def main():
    today = datetime(2026, 9, 22)
    data = _load_latest()
    samples = []
    for i, student in enumerate(data.get("students") or []):
        view_classes = (student.get("view") or {}).get("classes") or []
        facts = _look_next_facts(view_classes, today)
        analytics = _analytics(student, today)
        paragraph = _complete(facts, analytics)
        samples.append({
            "index": i,
            "paragraph": paragraph,
            "deterministic_line": facts["deterministic_line"],
            "due_soon_titles": [r["title"] for r in facts["due_soon"]],
            "past_due_classes": [
                {"course": r["course"], "count": r["count"]} for r in facts["past_due_on_chips"]
            ],
        })
        print(f"INDEX {i}")
        print(paragraph)
    OUT.write_text(json.dumps({
        "model": scraper.GROK_MODEL,
        "draft": True,
        "live": False,
        "samples": samples,
    }, indent=2) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
