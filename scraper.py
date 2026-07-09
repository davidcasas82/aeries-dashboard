import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://tustinusd.aeries.net"
EMAIL = os.getenv("AERIES_EMAIL", "")
PASSWORD = os.getenv("AERIES_PASSWORD", "")
GROK_API_KEY = os.getenv("GROK_API_KEY", "")
GROK_API_URL = "https://api.x.ai/v1/chat/completions"
GROK_MODEL = "grok-3-mini"

STUDENTS = []
for i in range(1, 10):
    student_sn = os.getenv(f"STUDENT_{i}_SN")
    if not student_sn:
        break
    STUDENTS.append({
        "sn": student_sn,
        "name": os.getenv(f"STUDENT_{i}_NAME", f"Student {i}"),
        "school_code": os.getenv(f"STUDENT_{i}_SCHOOL_CODE", "95"),
    })

OUTPUT_FILE = Path(__file__).parent / "grades_data.json"


def env_truthy(name):
    """Return True/False if env var is set to a boolean-ish value, else None."""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return None
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def is_summer_break(previous_data=None):
    """
    Summer break pauses Aeries scraping and Grok API calls.

    Source of truth in CI: GitHub Actions variable SUMMER_BREAK (true/false).
    Local fallback: summer_break field in grades_data.json.
    """
    env = env_truthy("SUMMER_BREAK")
    if env is not None:
        return env
    if previous_data is None:
        previous_data = {}
        if OUTPUT_FILE.exists():
            try:
                previous_data = json.loads(OUTPUT_FILE.read_text())
            except Exception:
                previous_data = {}
    return bool(previous_data.get("summer_break", False))


def require_scrape_config():
    if not EMAIL or not PASSWORD:
        print("ERROR: AERIES_EMAIL and AERIES_PASSWORD must be set in .env")
        sys.exit(1)
    if not STUDENTS:
        print("ERROR: No students configured in .env (need STUDENT_1_SN at minimum)")
        sys.exit(1)


def login():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    })

    login_url = f"{BASE_URL}/student/LoginParent.aspx"
    resp = session.get(login_url)
    if resp.status_code != 200:
        print(f"ERROR: Could not load login page (status {resp.status_code})")
        sys.exit(1)

    login_data = {
        "portalAccountUsername": EMAIL,
        "portalAccountPassword": PASSWORD,
        "checkCookiesEnabled": "true",
        "g-recaptcha-request-token": "",
        "submit": "",
    }

    resp = session.post(login_url, data=login_data, allow_redirects=True)

    if "LoginParent" in resp.url:
        error_match = re.search(r'class="[^"]*error[^"]*"[^>]*>([^<]+)', resp.text, re.IGNORECASE)
        if error_match:
            print(f"ERROR: Login failed — {error_match.group(1).strip()}")
        else:
            print("ERROR: Login failed — still on login page after submit")
        sys.exit(1)

    print("Logged in successfully")
    return session


def switch_student(session, school_code, student_sn):
    url = f"{BASE_URL}/student/ChangeStudent.aspx"
    params = {"SC": school_code, "SN": student_sn}
    resp = session.get(url, params=params, allow_redirects=True)
    return resp.status_code == 200


def fetch_class_summary(session):
    url = f"{BASE_URL}/student/Widgets/ClassSummary/GetClassSummary"
    params = {"IsProfile": "True"}
    resp = session.get(url, params=params)
    if resp.status_code == 200 and "json" in resp.headers.get("content-type", ""):
        return resp.json()
    return None


def fetch_gradebook_summary(session):
    """Fetch the GradebookSummary page which contains grade trend data."""
    resp = session.get(f"{BASE_URL}/student/GradebookSummary.aspx")
    if resp.status_code != 200:
        return []

    trends = []
    for match in re.finditer(
        r'var\s+\w+\s*=\s*(\[\s*\{\s*"overallDate"[\s\S]*?\]);', resp.text
    ):
        try:
            data = json.loads(match.group(1))
            trends.append(data)
        except json.JSONDecodeError:
            pass
    return trends


def fetch_all_assignments(session):
    """Fetch assignments for all classes by navigating GradebookDetails via postback."""
    resp = session.get(f"{BASE_URL}/student/GradebookDetails.aspx")
    if resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # Get form state
    all_inputs = {}
    for inp in soup.find_all("input", {"type": "hidden"}):
        name = inp.get("name", "")
        if name:
            all_inputs[name] = inp.get("value", "")

    # Get class dropdown
    class_select = soup.find("select", {"id": re.compile("dlGN")})
    if not class_select:
        return []

    options = class_select.find_all("option")
    spring_classes = [
        (opt["value"], opt.text.strip())
        for opt in options
        if "Spring" in opt.text
    ]

    all_class_assignments = []

    # First class is already loaded
    first_assignments = parse_assignment_rows(soup)
    if spring_classes:
        class_name = extract_class_name(spring_classes[0][1])
        all_class_assignments.append({
            "class_name": class_name,
            "assignments": first_assignments,
        })

    # Load remaining classes via async postback
    for class_value, class_label in spring_classes[1:]:
        class_name = extract_class_name(class_label)

        form_data = dict(all_inputs)
        form_data["__EVENTTARGET"] = "ctl00$MainContent$subGBS$dlGN"
        form_data["__EVENTARGUMENT"] = ""
        form_data["ctl00$MainContent$subGBS$dlGN"] = class_value
        form_data["ctl00$TheMasterScriptManager"] = (
            "ctl00$MainContent$subGBS$upEverything|ctl00$MainContent$subGBS$dlGN"
        )
        form_data["__ASYNCPOST"] = "true"

        resp2 = session.post(
            f"{BASE_URL}/student/GradebookDetails.aspx",
            data=form_data,
            headers={
                "X-MicrosoftAjax": "Delta=true",
                "X-Requested-With": "XMLHttpRequest",
            },
        )

        if resp2.status_code == 200 and len(resp2.text) > 500:
            frag_soup = BeautifulSoup(resp2.text, "html.parser")
            assignments = parse_assignment_rows(frag_soup)
            all_class_assignments.append({
                "class_name": class_name,
                "assignments": assignments,
            })

            # Update viewstate for next postback
            vs_match = re.search(r"__VIEWSTATE\|([^|]+)\|", resp2.text)
            if vs_match:
                all_inputs["__VIEWSTATE"] = vs_match.group(1)
            vsg_match = re.search(r"__VIEWSTATEGENERATOR\|([^|]+)\|", resp2.text)
            if vsg_match:
                all_inputs["__VIEWSTATEGENERATOR"] = vsg_match.group(1)

    return all_class_assignments


def extract_class_name(label):
    """Extract clean class name from dropdown label like '2- PE 8- Spring  1/6/2026...'"""
    match = re.match(r"\d+-\s*(.+?)-\s*Spring", label)
    return match.group(1).strip() if match else label


def parse_assignment_rows(soup):
    """Parse assignment-info rows from a BeautifulSoup object.

    Column layout (17 cells per row):
    [0]  # (with "Date Assigned: MM/DD/YYYY" embedded)
    [1]  Description
    [2]  Category
    [3]  Score fraction display (e.g. "5 / 5")
    [4]  Score earned
    [5]  "/" separator
    [6]  Score possible
    [7]  # Correct fraction display
    [8]  # Correct earned
    [9]  "/" separator
    [10] # Correct possible
    [11] Percentage (e.g. "100.00%")
    [12] Comment
    [13] Date Completed (MM/DD/YYYY)
    [14] Due Date (MM/DD/YYYY)
    [15] Grading Complete ("Yes" or "")
    [16] Documents
    """
    rows = soup.find_all("tr", class_="assignment-info")
    assignments = []

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 15:
            continue

        # Assignment number and date assigned
        cell0_text = cells[0].get_text(separator=" ", strip=True)
        num_match = re.match(r"(\d+)", cell0_text)
        assign_num = num_match.group(1) if num_match else ""
        date_assigned_match = re.search(r"Date Assigned:\s*([\d/]+)", cell0_text)
        date_assigned = date_assigned_match.group(1) if date_assigned_match else ""

        # Score
        points_earned = safe_float(cells[4].get_text(strip=True))
        points_possible = safe_float(cells[6].get_text(strip=True))

        # Percentage
        pct_text = cells[11].get_text(strip=True) if len(cells) > 11 else ""
        pct_match = re.search(r"([\d.]+)%", pct_text)
        percentage = float(pct_match.group(1)) if pct_match else None

        # Dates
        date_completed = cells[13].get_text(strip=True) if len(cells) > 13 else ""
        due_date = cells[14].get_text(strip=True) if len(cells) > 14 else ""

        # Grading complete
        grading_complete = (
            cells[15].get_text(strip=True) == "Yes" if len(cells) > 15 else False
        )

        assignment = {
            "number": assign_num,
            "description": cells[1].get_text(strip=True),
            "category": cells[2].get_text(strip=True),
            "points_earned": points_earned,
            "points_possible": points_possible,
            "percentage": percentage,
            "date_assigned": date_assigned,
            "date_completed": date_completed,
            "due_date": due_date,
            "grading_complete": grading_complete,
        }
        assignments.append(assignment)

    return assignments


def safe_float(val):
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def parse_class_summary(raw_data):
    if not raw_data:
        return []

    classes = []
    for course in raw_data:
        classes.append({
            "period": course.get("Period"),
            "course_name": course.get("CourseName", ""),
            "teacher": course.get("TeacherName", ""),
            "percent": course.get("Percent", ""),
            "mark": course.get("CurrentMark", ""),
            "mark_and_score": course.get("CurrentMarkAndScore", ""),
            "missing_count": course.get("NumMissingAssignments", 0),
            "missing_assignments": course.get("MissingAssignments", ""),
            "trend": course.get("Trend", ""),
            "room": course.get("RoomNumber", ""),
            "school_name": course.get("SchoolName", ""),
        })
    return classes


def extract_period_from_label(label):
    m = re.search(r"<<\s*(\d+)-", label or "")
    if m:
        return int(m.group(1))
    m2 = re.match(r"^(\d+)-", label or "")
    return int(m2.group(1)) if m2 else None


def parse_due_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%m/%d/%Y")
    except ValueError:
        return None


def parse_trend_html(trend_html):
    """Extract forecast direction and percentages from Aeries trend HTML."""
    if not trend_html:
        return None

    direction = "same"
    for cls, label in (
        ("up", "gradebook-trend-up"),
        ("down", "gradebook-trend-down"),
        ("same", "gradebook-trend-same"),
    ):
        if cls in trend_html:
            direction = label.replace("gradebook-trend-", "")
            break

    title_match = re.search(r'title="([^"]+)"', trend_html)
    forecast = None
    recent_avg = None
    if title_match:
        title = title_match.group(1).replace("&#37;", "%")
        forecast_match = re.search(
            r"Forecasted value of ([\d.]+)%", title, re.IGNORECASE
        )
        recent_match = re.search(
            r"average of the last four overall scores ([\d.]+)%", title, re.IGNORECASE
        )
        if forecast_match:
            forecast = float(forecast_match.group(1))
        if recent_match:
            recent_avg = float(recent_match.group(1))

    return {
        "direction": direction,
        "forecast_pct": forecast,
        "recent_four_avg_pct": recent_avg,
    }


def is_missing_assignment(assignment, today):
    due = parse_due_date(assignment.get("due_date"))
    if not due or due >= today:
        return False
    return assignment.get("points_earned") is None


def assignments_for_period(student_data, period):
    grouped = {}
    for ca in student_data.get("assignments_by_class", []):
        p = extract_period_from_label(ca.get("class_name", ""))
        if p is None:
            continue
        grouped.setdefault(p, []).extend(ca.get("assignments", []))
    return grouped.get(period, [])


def build_category_breakdown(assignments):
    """Average score by assignment category for graded work."""
    buckets = {}
    for a in assignments:
        if a.get("points_earned") is None or a.get("percentage") is None:
            continue
        cat = (a.get("category") or "Other").strip() or "Other"
        buckets.setdefault(cat, []).append(a["percentage"])

    breakdown = {}
    for cat, pcts in buckets.items():
        breakdown[cat] = {
            "avg_pct": round(sum(pcts) / len(pcts), 1),
            "count": len(pcts),
        }
    return breakdown


def is_assessment_category(category):
    c = (category or "").lower()
    return any(k in c for k in ("assess", "test", "quiz", "exam", "unit"))


def compute_score_trajectory(assignments):
    """Compare avg of last 3 graded scores vs prior 3."""
    graded = [
        a for a in assignments
        if a.get("points_earned") is not None and a.get("percentage") is not None
    ]
    graded.sort(key=lambda a: parse_due_date(a.get("due_date")) or datetime.min)

    if len(graded) < 4:
        return "insufficient_data"

    recent = [a["percentage"] for a in graded[-3:]]
    prior = [a["percentage"] for a in graded[-6:-3]] if len(graded) >= 6 else [a["percentage"] for a in graded[:-3]]

    if not prior:
        return "insufficient_data"

    recent_avg = sum(recent) / len(recent)
    prior_avg = sum(prior) / len(prior)
    diff = recent_avg - prior_avg

    if diff >= 5:
        return "improving"
    if diff <= -5:
        return "slipping"
    return "flat"


def detect_performance_pattern(grade_pct, missing_count, category_breakdown, score_trajectory):
    """Rule-based tag for Grok to interpret."""
    has_missing = missing_count > 0
    low_grade = grade_pct is not None and grade_pct < 80
    strong_grade = grade_pct is not None and grade_pct >= 90 and not has_missing

    assessment_avgs = []
    other_avgs = []
    for cat, info in category_breakdown.items():
        if is_assessment_category(cat):
            assessment_avgs.append(info["avg_pct"])
        else:
            other_avgs.append(info["avg_pct"])

    test_weakness = False
    if assessment_avgs and other_avgs:
        assess_avg = sum(assessment_avgs) / len(assessment_avgs)
        other_avg = sum(other_avgs) / len(other_avgs)
        if assess_avg < 75 and other_avg >= 80:
            test_weakness = True
        elif other_avg - assess_avg >= 12:
            test_weakness = True

    if strong_grade and score_trajectory != "slipping":
        return "on_track"
    if not low_grade and has_missing:
        return "completion_gap"
    if low_grade and has_missing:
        return "both"
    if test_weakness:
        return "test_weakness"
    if low_grade:
        return "low_performance"
    if score_trajectory == "slipping":
        return "slipping_trend"
    return "mixed"


def infer_urgency_from_analytics(grade_pct, missing_count, trend, performance_pattern):
    if grade_pct is None:
        return "ok", "on_track"

    if grade_pct >= 90 and missing_count == 0 and performance_pattern == "on_track":
        return "strong", "on_track"
    if grade_pct >= 80 and missing_count == 0 and performance_pattern not in ("slipping_trend", "low_performance"):
        return "ok", "on_track"
    if grade_pct >= 80 and missing_count > 0:
        return "watch", "missing_backlog"
    if grade_pct < 80 and missing_count > 0:
        return "critical", "both"
    if grade_pct < 70:
        return "critical", "low_grade"
    if grade_pct < 80:
        return "critical", "low_grade"
    if trend and trend.get("direction") == "down":
        return "watch", "slipping_trend"
    return "ok", "on_track"


def format_assignment_entry(assignment, today, kind):
    due = parse_due_date(assignment.get("due_date"))
    entry = {
        "name": assignment.get("description", ""),
        "category": assignment.get("category", ""),
        "due_date": assignment.get("due_date", ""),
        "points_possible": assignment.get("points_possible"),
    }
    if due:
        entry["due_weekday"] = due.strftime("%a")
        if kind == "missing":
            entry["days_overdue"] = (today - due).days
        elif kind == "upcoming":
            entry["days_until_due"] = (due - today).days
    if kind == "recent":
        entry["points_earned"] = assignment.get("points_earned")
        entry["percentage"] = assignment.get("percentage")
    return entry


def analyze_class(class_meta, assignments, today):
    grade_pct = safe_float(class_meta.get("percent"))
    mark = class_meta.get("mark") or ""

    missing = [
        a for a in assignments
        if is_missing_assignment(a, today)
    ]
    missing.sort(key=lambda a: parse_due_date(a.get("due_date")) or datetime.min)

    recoverable_points = sum(
        a.get("points_possible") or 0 for a in missing
    )

    recent = []
    upcoming = []
    for a in assignments:
        due = parse_due_date(a.get("due_date"))
        if not due:
            continue
        days_diff = (due - today).days
        if a.get("points_earned") is not None and -14 <= days_diff < 0:
            recent.append(a)
        elif 0 <= days_diff <= 14 and not a.get("grading_complete"):
            upcoming.append(a)

    recent.sort(key=lambda a: parse_due_date(a.get("due_date")) or datetime.min, reverse=True)
    upcoming.sort(key=lambda a: parse_due_date(a.get("due_date")) or datetime.min)

    category_breakdown = build_category_breakdown(assignments)
    score_trajectory = compute_score_trajectory(assignments)
    trend = parse_trend_html(class_meta.get("trend", ""))
    performance_pattern = detect_performance_pattern(
        grade_pct, len(missing), category_breakdown, score_trajectory
    )
    urgency, issue_type = infer_urgency_from_analytics(
        grade_pct, len(missing), trend, performance_pattern
    )

    return {
        "period": class_meta.get("period"),
        "course_name": class_meta.get("course_name", ""),
        "teacher": class_meta.get("teacher", ""),
        "current_grade_pct": grade_pct,
        "current_grade_mark": mark,
        "trend": trend,
        "missing_assignments": [
            format_assignment_entry(a, today, "missing") for a in missing
        ],
        "recoverable_points": round(recoverable_points, 1),
        "category_breakdown": category_breakdown,
        "recent_scores": [
            format_assignment_entry(a, today, "recent") for a in recent[:8]
        ],
        "upcoming": [
            format_assignment_entry(a, today, "upcoming") for a in upcoming[:8]
        ],
        "performance_pattern": performance_pattern,
        "score_trajectory": score_trajectory,
        "suggested_urgency": urgency,
        "issue_type": issue_type,
    }


def build_class_analytics(student_data):
    """Pre-compute per-class facts for Grok interpretation."""
    today = datetime.now()
    student_name = student_data.get("name", "Student")
    class_analytics = []

    for class_meta in student_data.get("classes", []):
        if not class_meta.get("percent"):
            continue
        period = class_meta.get("period")
        assignments = assignments_for_period(student_data, period)
        class_analytics.append(analyze_class(class_meta, assignments, today))

    total_recoverable = sum(c.get("recoverable_points", 0) for c in class_analytics)
    missing_class_count = sum(1 for c in class_analytics if c.get("missing_assignments"))
    low_grade_count = sum(
        1 for c in class_analytics
        if c.get("current_grade_pct") is not None and c["current_grade_pct"] < 80
    )

    if missing_class_count >= 2:
        dominant_theme = (
            f"missing_work_backlog in {missing_class_count} of "
            f"{len(class_analytics)} graded classes"
        )
    elif low_grade_count >= 2:
        dominant_theme = f"low_grades in {low_grade_count} classes"
    elif any(c.get("performance_pattern") == "test_weakness" for c in class_analytics):
        dominant_theme = "assessment_scores lagging behind other work"
    else:
        dominant_theme = "mixed or stable performance"

    wins = []
    for c in class_analytics:
        if (
            c.get("current_grade_pct") is not None
            and c["current_grade_pct"] >= 85
            and not c.get("missing_assignments")
            and c.get("score_trajectory") != "slipping"
        ):
            wins.append({
                "class_name": c["course_name"],
                "grade": f"{c.get('current_grade_mark') or ''} {c['current_grade_pct']}%".strip(),
                "note": "no missing work, stable or improving",
            })

    urgency_rank = {"critical": 0, "watch": 1, "ok": 2, "strong": 3}
    classes_needing_focus = sorted(
        [
            {
                "class_name": c["course_name"],
                "urgency": c["suggested_urgency"],
                "issue_type": c["issue_type"],
                "recoverable_points": c.get("recoverable_points", 0),
            }
            for c in class_analytics
            if c["suggested_urgency"] in ("critical", "watch")
        ],
        key=lambda x: (
            urgency_rank.get(x["urgency"], 2),
            -x.get("recoverable_points", 0),
        ),
    )

    return {
        "student_name": student_name,
        "date": today.strftime("%A, %B %d, %Y"),
        "dominant_theme": dominant_theme,
        "total_recoverable_pts": round(total_recoverable, 1),
        "classes_needing_focus": classes_needing_focus,
        "wins_hints": wins[:3],
        "classes": class_analytics,
    }


def build_student_context(student_data):
    """Build Grok user message from precomputed analytics."""
    analytics = build_class_analytics(student_data)
    return (
        "PRECOMPUTED_ANALYTICS (source of truth — do not redo arithmetic):\n"
        f"{json.dumps(analytics, indent=2)}\n\n"
        "Interpret these facts into a family briefing. Use assignment names, "
        "scores, and trends from this JSON. Do not invent assignments or scores."
    )  # kept for standalone testing


def generate_ai_summary(student_data):
    """Call Grok API to generate a family-facing daily briefing."""
    if not GROK_API_KEY:
        return None

    student_name = student_data.get("name", "the student")
    analytics = build_class_analytics(student_data)
    context = (
        "PRECOMPUTED_ANALYTICS (source of truth — do not redo arithmetic):\n"
        f"{json.dumps(analytics, indent=2)}\n\n"
        "Write a tight family briefing from these facts only. "
        "Name real assignments and scores. Do not invent data. "
        "Do not restate assignment lists the dashboard already shows — interpret them."
    )

    today = datetime.now()
    today_dow = today.strftime("%A")
    system_prompt = f"""You are a tight, practical academic coach writing a daily family briefing for {student_name}. Parent and student read this on a phone in under a minute.

Today is {today_dow}, {today.strftime('%B %d, %Y')}.

You receive PRECOMPUTED_ANALYTICS (grades, missing work, categories, trends, recoverable points). Your job is INTERPRETATION only — no math, no full assignment dumps.

VOICE
- Third person with "{student_name}" (never "you")
- Plain family English. No jargon: never say completion_gap, performance_pattern, recoverable points, dominant_theme, issue_type, or "Pattern suggests"
- Short sentences. Specific. Encouraging, not accusatory
- Prefer naming the 1–2 highest-leverage missing items over listing everything

Respond ONLY with valid JSON matching this exact schema:
{{
  "headline": "ONE sentence the family can skim — the cross-class story, not every grade",
  "focus_tonight": "Single highest-leverage action for tonight: named assignment(s) + class. Empty string if nothing urgent",
  "wins": ["0–2 short genuine positives with evidence — not category % recitation"],
  "classes": [
    {{
      "class_name": "must match course_name from analytics",
      "urgency": "critical | watch | ok | strong",
      "snap": "≤12 words for the closed card line — why this class matters right now",
      "story": "1–2 plain sentences: what's going on. Habit vs skill only when clear from data. Do NOT dump the full missing list",
      "do_tonight": "What {student_name} should do tonight — named assignment(s). Empty string if nothing needed",
      "parent_tip": "One low-pressure parent move. Empty string if not useful",
      "ask": "Optional natural conversation opener. Empty string unless it truly helps"
    }}
  ]
}}

Urgency (match suggested_urgency unless you have a strong reason not to — drives dashboard colors):
- critical: grade below 70%, OR below 80% WITH missing work, OR failing trend with multiple high-point missing items
- watch: B+ (80%+) but missing work or a clear slipping trend — backlog, not panic
- ok: solid, nothing urgent
- strong: A / 90%+, no missing, clearly on track

Rules:
- Include EVERY class from analytics.classes
- Order classes: critical → watch → ok → strong
- For ok/strong classes: short snap + brief story is enough; leave do_tonight, parent_tip, and ask empty
- For critical/watch: fill do_tonight when there is a concrete action; name specific assignments from missing_assignments (not "the missing work")
- Do NOT invent scores, assignments, or causes
- Do NOT list every missing assignment in story — pick what matters most
- Do NOT write forecast math lectures; at most one plain phrase if trend is clearly slipping or improving
- wins must be real and specific (a strong class, a good recent score, an improving trend) — skip empty praise
- Prefer empty strings over filler"""

    user_prompt = context

    try:
        resp = requests.post(
            GROK_API_URL,
            headers={
                "Authorization": f"Bearer {GROK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROK_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.4,
                "max_tokens": 5000,
            },
            timeout=120,
        )

        if resp.status_code != 200:
            print(f"  WARNING: Grok API returned {resp.status_code}")
            return None

        result = resp.json()
        content = result["choices"][0]["message"]["content"]
        summary = json.loads(content)
        summary["generated_at"] = datetime.now(timezone.utc).isoformat()
        summary["analytics_snapshot"] = {
            "dominant_theme": analytics.get("dominant_theme"),
            "total_recoverable_pts": analytics.get("total_recoverable_pts"),
        }
        return summary

    except requests.exceptions.Timeout:
        print("  WARNING: Grok API timed out")
        return None
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"  WARNING: Failed to parse Grok response: {e}")
        return None


def regenerate_grok_summaries():
    """Re-run Grok briefings against existing grades_data.json (no Aeries scrape)."""
    if not GROK_API_KEY:
        print("ERROR: GROK_API_KEY must be set")
        sys.exit(1)

    if not OUTPUT_FILE.exists():
        print(f"ERROR: {OUTPUT_FILE} not found — run a full scrape first")
        sys.exit(1)

    try:
        data = json.loads(OUTPUT_FILE.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: Could not parse {OUTPUT_FILE}: {e}")
        sys.exit(1)

    if is_summer_break(data):
        print("Summer break mode is active (SUMMER_BREAK) — skipping Grok API calls.")
        data["summer_break"] = True
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        OUTPUT_FILE.write_text(json.dumps(data, indent=2))
        return

    students = data.get("students", [])
    if not students:
        print("ERROR: No students found in grades_data.json")
        sys.exit(1)

    print(f"Regenerating AI briefings for {len(students)} student(s) from {OUTPUT_FILE.name}...")

    for student in students:
        name = student.get("name", "Student")
        print(f"  {name}...")
        ai_summary = generate_ai_summary(student)
        if ai_summary:
            student["ai_summary"] = ai_summary
            print(f"    AI summary generated ({len(ai_summary.get('classes', []))} classes)")
        else:
            print("    WARNING: AI summary failed — keeping previous briefing if any")

    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    data["summer_break"] = False
    OUTPUT_FILE.write_text(json.dumps(data, indent=2))
    print(f"\nBriefings written to {OUTPUT_FILE}")
    print(f"Last updated: {data['last_updated']}")


def scrape_all():
    require_scrape_config()

    previous_data = {}
    if OUTPUT_FILE.exists():
        try:
            previous_data = json.loads(OUTPUT_FILE.read_text())
        except Exception:
            previous_data = {}

    summer_break = is_summer_break(previous_data)

    if summer_break:
        print("Summer break mode is active (SUMMER_BREAK env or grades_data.json).")
        print("Skipping Aeries scraping and Grok API calls for today.")
        data = previous_data.copy() if previous_data else {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "district": "Tustin USD",
            "portal_url": BASE_URL,
            "students": [],
            "summer_break": True,
        }
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        data["summer_break"] = True
        OUTPUT_FILE.write_text(json.dumps(data, indent=2))
        print(f"Data preserved (no new scrape). Written to {OUTPUT_FILE}")
        return

    session = login()
    data = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "district": "Tustin USD",
        "portal_url": BASE_URL,
        "students": [],
        "summer_break": False,
    }

    for student in STUDENTS:
        print(f"Fetching data for {student['name']} (SN: {student['sn']})...")

        switch_student(session, student["school_code"], student["sn"])

        # Class summary (grades overview)
        raw_summary = fetch_class_summary(session)
        classes = parse_class_summary(raw_summary)
        print(f"  {len(classes)} classes")

        # Assignments per class
        print("  Fetching assignments...")
        class_assignments = fetch_all_assignments(session)
        total_assignments = sum(len(ca["assignments"]) for ca in class_assignments)
        print(f"  {total_assignments} assignments across {len(class_assignments)} classes")

        student_data = {
            "sn": student["sn"],
            "name": student["name"],
            "school_code": student["school_code"],
            "classes": classes,
            "assignments_by_class": class_assignments,
        }

        if GROK_API_KEY:
            print("  Generating AI summary...")
            ai_summary = generate_ai_summary(student_data)
            if ai_summary:
                student_data["ai_summary"] = ai_summary
                print("  AI summary generated")
            else:
                print("  AI summary skipped")

        data["students"].append(student_data)

    data["summer_break"] = False

    OUTPUT_FILE.write_text(json.dumps(data, indent=2))
    print(f"\nData written to {OUTPUT_FILE}")
    print(f"Last updated: {data['last_updated']}")
    print(f"Summer break: {data['summer_break']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aeries grade scraper + Grok briefings")
    parser.add_argument(
        "--grok-only",
        action="store_true",
        help="Regenerate AI briefings from existing grades_data.json (skip Aeries scrape)",
    )
    args = parser.parse_args()

    if args.grok_only:
        regenerate_grok_summaries()
    else:
        scrape_all()
