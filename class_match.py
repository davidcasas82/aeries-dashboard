"""Matching a ClassSummary row to its GradebookDetails assignments. Stdlib only.

The scrape and the Mac mini runner both need it, and the runner's SSD venv has no requests or
bs4, so it lives here rather than in scraper.py.
"""

import re


def extract_period_from_label(label):
    m = re.search(r"<<\s*(\d+)-", label or "")
    if m:
        return int(m.group(1))
    m2 = re.match(r"^(\d+)-", label or "")
    return int(m2.group(1)) if m2 else None


def _norm_course_name(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def assignments_for_class(student_data, class_meta):
    """Match GradebookDetails groups to a ClassSummary row by period and/or name."""
    period = class_meta.get("period") if class_meta else None
    course_n = _norm_course_name((class_meta or {}).get("course_name"))
    matched = []
    seen = set()
    for ca in student_data.get("assignments_by_class") or []:
        label = ca.get("class_name") or ""
        group_period = ca.get("period")
        if group_period is None:
            group_period = extract_period_from_label(label)
        name_n = _norm_course_name(label)
        hit = False
        if period is not None and group_period is not None and int(group_period) == int(period):
            hit = True
        elif course_n and name_n and (course_n in name_n or name_n in course_n):
            hit = True
        if not hit:
            continue
        key = (group_period, name_n or label)
        if key in seen:
            continue
        seen.add(key)
        matched.extend(ca.get("assignments") or [])
    return matched
