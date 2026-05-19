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

if not EMAIL or not PASSWORD:
    print("ERROR: AERIES_EMAIL and AERIES_PASSWORD must be set in .env")
    sys.exit(1)

if not STUDENTS:
    print("ERROR: No students configured in .env (need STUDENT_1_SN at minimum)")
    sys.exit(1)

OUTPUT_FILE = Path(__file__).parent / "grades_data.json"


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


def build_student_context(student_data):
    """Build a compact text summary of a student's data for the AI prompt."""
    today = datetime.now()
    lines = []

    lines.append(f"Student: {student_data['name']}")
    lines.append(f"Date: {today.strftime('%B %d, %Y')}")
    lines.append("")

    # Grades overview with real missing counts
    lines.append("CURRENT GRADES:")
    for c in student_data["classes"]:
        if not c["percent"]:
            continue
        missing = 0
        missing_html = c.get("missing_assignments", "")
        if "MissingAssignment" in str(missing_html):
            m = re.search(r">(\d+)<", missing_html)
            if m:
                missing = int(m.group(1))
        missing_str = f" [{missing} MISSING]" if missing > 0 else ""
        lines.append(
            f"  P{c['period']} {c['course_name']}: {c['percent']}% "
            f"({c['mark'] or 'no letter grade'}){missing_str} — {c['teacher']}"
        )

    # Assignments by class — recent, upcoming, and missing
    lines.append("")
    lines.append("ASSIGNMENTS BY CLASS:")
    for ca in student_data.get("assignments_by_class", []):
        recent = []
        upcoming = []
        missing = []

        for a in ca["assignments"]:
            if not a["due_date"]:
                continue
            try:
                due = datetime.strptime(a["due_date"], "%m/%d/%Y")
            except ValueError:
                continue

            days_diff = (due - today).days

            if a["points_earned"] is not None and -14 <= days_diff < 0:
                recent.append(a)
            elif days_diff >= 0 and days_diff <= 14 and not a["grading_complete"]:
                upcoming.append(a)
            elif days_diff < 0 and a["points_earned"] is None and not a["grading_complete"]:
                missing.append(a)

        if not recent and not upcoming and not missing:
            continue

        lines.append(f"\n  {ca['class_name']}:")

        if recent:
            lines.append("    Recent scores:")
            for a in recent[-5:]:
                pct = f"{a['percentage']:.0f}%" if a["percentage"] else "?"
                lines.append(
                    f"      {a['description'][:50]} — "
                    f"{a['points_earned']}/{a['points_possible']} ({pct}) "
                    f"due {a['due_date']}"
                )

        if upcoming:
            lines.append("    Upcoming:")
            for a in upcoming:
                pts = f" (worth {a['points_possible']} pts)" if a["points_possible"] else ""
                lines.append(
                    f"      {a['description'][:50]} — due {a['due_date']}{pts}"
                )

        if missing:
            lines.append("    MISSING/UNSCORED (past due):")
            for a in missing[-5:]:
                pts = f" (worth {a['points_possible']} pts)" if a["points_possible"] else ""
                lines.append(
                    f"      {a['description'][:50]} — was due {a['due_date']}{pts}"
                )

    return "\n".join(lines)


def generate_ai_summary(student_data):
    """Call Grok API to generate a parent-facing weekly summary."""
    if not GROK_API_KEY:
        return None

    context = build_student_context(student_data)

    today = datetime.now()
    today_dow = today.strftime("%A")
    system_prompt = f"""You are a direct, no-nonsense academic advisor helping a parent monitor their child's school performance. Your job is to give a clear weekly status briefing organized by class — what's happening, what needs attention, and where to focus effort for maximum grade impact.

Today is {today_dow}, {today.strftime('%B %d, %Y')}.

Respond ONLY with valid JSON matching this exact schema:
{{
  "status": "2-3 sentence executive summary of where this student stands right now — highlight the 1-2 classes that need the most attention",
  "classes": [
    {{
      "class_name": "short name (e.g. Engineering Geo, Eng 9, WrldHis)",
      "current_grade": "letter and % (e.g. B- 74%)",
      "status": "one sentence — how this class is going right now",
      "recent": [
        {{"date": "Day, Mon DD", "event": "score posted, assignment turned in, etc."}}
      ],
      "upcoming": [
        {{"day": "Day, Mon DD", "assignment": "name", "weight_context": "points / importance"}}
      ],
      "action_items": [
        {{"priority": "high or medium", "issue": "what's wrong", "parent_action": "specific thing to do"}}
      ]
    }}
  ]
}}

Rules:
- ALL dates must include day of week: "Wed, May 21" not just "05/21/2026".
- Only include classes that have graded assignments or notable activity. Skip electives/PE with no grade data.
- Order classes by priority: classes with missing work or grades below B come first.
- "recent" = assignments graded in the last 7-10 days in that class (scores posted). Most recent first. Include the score.
- "upcoming" = assignments due this week and next week in that class. Include day of week.
- "action_items" = specific things the parent should follow up on for this class. Only include if there IS an action needed.
- Be specific and actionable. "Talk to your kid about missing work" is useless. "Ask Daniel if he turned in the 29.1 and 29.2 assignments — they show as unscored and are worth 20pts total" is useful.
- For classes with action items, do rough math: if missing assignments are worth X pts, estimate what turning them in would do to the grade.
- Classes where everything is fine (A, no missing work, nothing upcoming): include with a brief positive status but empty action_items array. Keep the entry short.
- Max 2-3 action items per class. Focus on highest-impact actions."""

    user_prompt = f"Here is the current academic data:\n\n{context}"

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
                "temperature": 0.7,
                "max_tokens": 3000,
            },
            timeout=90,
        )

        if resp.status_code != 200:
            print(f"  WARNING: Grok API returned {resp.status_code}")
            return None

        result = resp.json()
        content = result["choices"][0]["message"]["content"]
        summary = json.loads(content)
        summary["generated_at"] = datetime.now(timezone.utc).isoformat()
        return summary

    except requests.exceptions.Timeout:
        print("  WARNING: Grok API timed out")
        return None
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"  WARNING: Failed to parse Grok response: {e}")
        return None


def scrape_all():
    session = login()
    data = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "district": "Tustin USD",
        "portal_url": BASE_URL,
        "students": [],
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

    OUTPUT_FILE.write_text(json.dumps(data, indent=2))
    print(f"\nData written to {OUTPUT_FILE}")
    print(f"Last updated: {data['last_updated']}")


if __name__ == "__main__":
    scrape_all()
