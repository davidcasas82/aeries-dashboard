import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
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

TUSD_CALENDAR_URL = "https://www.tustin.k12.ca.us/about-us/calendars-school-year"
CALENDAR_FILE = Path(__file__).parent / "school_calendar.json"

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
HISTORY_FILE = Path(__file__).parent / "grade_history.json"
HISTORY_MAX_DAYS = 120
HISTORY_MISSING_NAMES_CAP = 8
PCT_HISTORY_UI_POINTS = 30
TREND_DELTA_THRESHOLD = 2.0  # percentage points over 7d for improve/slip labels

# Fallback if school_calendar.json missing / unreadable
_DEFAULT_SCHOOL_YEARS = [
    {"id": "2025-26", "first_day": "2025-08-13", "last_day": "2026-05-29"},
    {"id": "2026-27", "first_day": "2026-08-13", "last_day": "2027-05-28"},
    {"id": "2027-28", "first_day": "2027-08-12", "last_day": "2028-05-26"},
]


def downsample_pct_history(points, max_points=PCT_HISTORY_UI_POINTS):
    """Keep first/last and evenly sample middle so sparklines span the full term."""
    if not points or len(points) <= max_points:
        return list(points or [])
    if max_points < 2:
        return points[-1:]
    n = len(points)
    indices = {0, n - 1}
    for i in range(1, max_points - 1):
        idx = round(i * (n - 1) / (max_points - 1))
        indices.add(idx)
    return [points[i] for i in sorted(indices)]


def env_truthy(name):
    """Return True/False if env var is set to a boolean-ish value, else None."""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return None
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _parse_iso_date_str(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _month_day_to_date(token, year_hint):
    """Parse 'Aug 13' / 'Aug 13, Wednesday' / 'May 29, 2026' with a year hint."""
    token = re.sub(r"\s+", " ", (token or "").strip())
    token = re.sub(r",?\s*(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", "", token, flags=re.I)
    token = token.strip(" ,")
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(token, fmt).date()
        except ValueError:
            pass
    for fmt in ("%b %d", "%B %d"):
        try:
            d = datetime.strptime(token, fmt)
            return date(year_hint, d.month, d.day)
        except ValueError:
            pass
    return None


def parse_tusd_school_years_html(html):
    """Extract first/last instructional days from TUSD school-year calendar page."""
    if not html:
        return []
    text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    years = []
    # Split on "YYYY-YY School Year Calendar"
    chunks = re.split(r"(\d{4}-\d{2})\s*School Year Calendar", text)
    # chunks: [pre, id1, body1, id2, body2, ...]
    for i in range(1, len(chunks) - 1, 2):
        year_id = chunks[i].strip()
        body = chunks[i + 1]
        # Year span e.g. 2025-26 → start year 2025, end year 2026
        try:
            start_y = int(year_id.split("-")[0])
            end_y = start_y + 1
        except ValueError:
            continue

        first = None
        last = None
        # Line before FIRST DAY OF SCHOOL
        m_first = re.search(
            r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?)"
            r"[^\n]{0,30}\n\s*FIRST DAY OF SCHOOL",
            body,
            re.I,
        )
        if m_first:
            first = _month_day_to_date(m_first.group(1), start_y)

        m_last = re.search(
            r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?)"
            r"[^\n]{0,30}\n\s*LAST DAY OF SCHOOL",
            body,
            re.I,
        )
        if m_last:
            last = _month_day_to_date(m_last.group(1), end_y)

        if first and last:
            years.append({
                "id": year_id,
                "first_day": first.isoformat(),
                "last_day": last.isoformat(),
            })
    return years


def fetch_tusd_school_years(timeout=20):
    """Download TUSD public calendar; return year list or []."""
    try:
        resp = requests.get(
            TUSD_CALENDAR_URL,
            timeout=timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; aeries-dashboard/1.0; "
                    "+https://github.com/davidcasas82/aeries-dashboard)"
                )
            },
        )
        if resp.status_code != 200:
            return []
        return parse_tusd_school_years_html(resp.text)
    except requests.RequestException:
        return []


def load_school_calendar(refresh=True):
    """Load school years from school_calendar.json, optionally refresh from TUSD."""
    data = {
        "district": "Tustin Unified School District",
        "source_url": TUSD_CALENDAR_URL,
        "years": list(_DEFAULT_SCHOOL_YEARS),
    }
    if CALENDAR_FILE.exists():
        try:
            file_data = json.loads(CALENDAR_FILE.read_text())
            if file_data.get("years"):
                data.update(file_data)
        except (json.JSONDecodeError, OSError):
            pass

    if refresh and env_truthy("SKIP_CALENDAR_REFRESH") is not True:
        remote_years = fetch_tusd_school_years()
        if remote_years:
            # Merge by id (remote wins)
            by_id = {y["id"]: y for y in data.get("years") or []}
            for y in remote_years:
                by_id[y["id"]] = y
            data["years"] = sorted(by_id.values(), key=lambda y: y.get("first_day") or "")
            data["updated_at"] = datetime.now(timezone.utc).date().isoformat()
            data["source_url"] = TUSD_CALENDAR_URL
            try:
                CALENDAR_FILE.write_text(json.dumps(data, indent=2) + "\n")
            except OSError:
                pass
    return data


def school_session_window(today=None, calendar=None):
    """Return the school-year dict containing today, or None if outside all windows (summer)."""
    today = today or datetime.now().date()
    if isinstance(today, datetime):
        today = today.date()
    calendar = calendar or load_school_calendar(refresh=False)
    for y in calendar.get("years") or []:
        first = _parse_iso_date_str(y.get("first_day"))
        last = _parse_iso_date_str(y.get("last_day"))
        if first and last and first <= today <= last:
            return y
    return None


def next_first_day(today=None, calendar=None):
    """Next first day of school on or after today (for UI messaging)."""
    today = today or datetime.now().date()
    if isinstance(today, datetime):
        today = today.date()
    calendar = calendar or load_school_calendar(refresh=False)
    candidates = []
    for y in calendar.get("years") or []:
        first = _parse_iso_date_str(y.get("first_day"))
        if first and first >= today:
            candidates.append(first)
    return min(candidates) if candidates else None


def target_attendance_year_label(today=None, calendar=None):
    """Aeries-style year label (e.g. 2025-2026) for the school year we should display.

    - During a session: the active school year
    - During summer: the year that most recently started (just ended), not an older one
    """
    today = today or datetime.now().date()
    if isinstance(today, datetime):
        today = today.date()
    calendar = calendar or load_school_calendar(refresh=False)

    active = school_session_window(today=today, calendar=calendar)
    if active:
        first = _parse_iso_date_str(active.get("first_day"))
        last = _parse_iso_date_str(active.get("last_day"))
        if first:
            end_y = last.year if last else first.year + 1
            return f"{first.year}-{end_y}"

    started = []
    for y in calendar.get("years") or []:
        first = _parse_iso_date_str(y.get("first_day"))
        last = _parse_iso_date_str(y.get("last_day"))
        if first and first <= today:
            end_y = last.year if last else first.year + 1
            started.append((first, f"{first.year}-{end_y}"))
    if not started:
        return None
    started.sort(key=lambda t: t[0], reverse=True)
    return started[0][1]


def is_calendar_summer_break(today=None, calendar=None):
    """True when today is outside every instructional school-year window."""
    return school_session_window(today=today, calendar=calendar) is None


def is_summer_break(previous_data=None):
    """
    Pause Aeries scraping and Grok API calls during summer (and forced break).

    Priority:
      1. SUMMER_BREAK env (GitHub variable / local) — explicit override
      2. TUSD school calendar (school_calendar.json ± live refresh)
      3. summer_break field on grades_data.json
    """
    env = env_truthy("SUMMER_BREAK")
    if env is not None:
        return env

    try:
        calendar = load_school_calendar(refresh=True)
        # Calendar is authoritative when available
        if calendar.get("years"):
            paused = is_calendar_summer_break(calendar=calendar)
            return paused
    except Exception as e:
        print(f"  WARNING: school calendar check failed ({e}); falling back")

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


def _parse_att_summary_area(html):
    """Parse #AttendanceSummaryArea on Attendance.aspx (current-year totals)."""
    soup = BeautifulSoup(html, "html.parser")
    area = soup.find(id="AttendanceSummaryArea") or soup.find(
        class_=re.compile(r"AttendanceSummary", re.I)
    )
    if not area:
        return None
    text = re.sub(r"\s+", " ", area.get_text(" ", strip=True))
    # Year Days Enrolled: 0 Days Present: 0 Days Excused: 0 Days Unexcused: 0 Periods Tardy: 0 ...
    def grab(label):
        m = re.search(rf"{label}\s*:\s*(\d+)", text, re.I)
        return int(m.group(1)) if m else None

    enrolled = grab(r"Days\s+Enrolled")
    present = grab(r"Days\s+Present")
    excused = grab(r"Days\s+Excused")
    unexcused = grab(r"Days\s+Unexcused")
    tardies = grab(r"Periods\s+Tardy")
    if all(v is None for v in (enrolled, present, excused, unexcused, tardies)):
        return None
    absences = None
    if excused is not None or unexcused is not None:
        absences = (excused or 0) + (unexcused or 0)
    return {
        "absences": absences,
        "tardies": tardies,
        "excused": excused,
        "unexcused": unexcused,
        "days_enrolled": enrolled,
        "days_present": present,
        "year": None,
        "source": "Attendance.aspx#AttendanceSummaryArea",
        "raw": text[:200],
    }


def _parse_history_year_cards(html, prefer_year=None):
    """Parse Attendance History Summary cards.

    Cards look like:
      2025-2026 Legacy Magnet Academy (...) Program: Home School: Grade: 9
      Enrolled: 180 Present: 178 Absent: 2 ...

    prefer_year: Aeries label like '2025-2026' from TUSD calendar alignment.
    """
    soup = BeautifulSoup(html, "html.parser")
    page_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    pattern = re.compile(
        r"(20\d{2}\s*[-–]\s*20\d{2})\s+"
        r"(.+?)\s+Program:\s*(\w*)\s*"
        r".{0,40}?"
        r"Enrolled:\s*(\d+)\s*Present:\s*(\d+)\s*Absen\w*:\s*(\d+)",
        re.I,
    )
    cards = []
    for m in pattern.finditer(page_text):
        year = re.sub(r"\s+", "", m.group(1).replace("–", "-"))
        school = m.group(2).strip()[:80]
        program = (m.group(3) or "").strip().upper()
        enrolled = int(m.group(4))
        present = int(m.group(5))
        absent = int(m.group(6))
        is_transfer = program in ("I", "H") or enrolled < 20
        cards.append({
            "year": year,
            "days_enrolled": enrolled,
            "days_present": present,
            "absences": absent,
            "school": school,
            "program": program,
            "is_transfer": is_transfer,
        })

    if not cards:
        for m in re.finditer(
            r"(20\d{2}\s*[-–]\s*20\d{2}).{0,160}?"
            r"Enrolled:\s*(\d+)\s*Present:\s*(\d+)\s*Absen\w*:\s*(\d+)",
            page_text,
            re.I,
        ):
            year = re.sub(r"\s+", "", m.group(1).replace("–", "-"))
            enrolled = int(m.group(2))
            cards.append({
                "year": year,
                "days_enrolled": enrolled,
                "days_present": int(m.group(3)),
                "absences": int(m.group(4)),
                "school": "",
                "program": "",
                "is_transfer": enrolled < 20,
            })

    if not cards:
        return None

    seen = set()
    unique = []
    for c in cards:
        key = (c["year"], c["days_enrolled"], c["absences"], c["days_present"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)

    full = [c for c in unique if not c["is_transfer"] and c["days_enrolled"] >= 100]
    pool = full or [c for c in unique if not c["is_transfer"]] or unique

    prefer_year = (prefer_year or "").replace("–", "-").replace(" ", "")
    best = None
    if prefer_year:
        for c in pool:
            if c["year"] == prefer_year:
                best = c
                break
    if best is None:
        pool.sort(key=lambda c: c["year"], reverse=True)
        best = pool[0]

    year_compact = best["year"]
    # Period tardy totals for that year only (T = tardy, V = excused tardy in TUSD)
    tardies = 0
    for m in re.finditer(
        rf"\b[TV]\s+{re.escape(year_compact)}\s+(?:EXTARDY|TARDY)\b.{{0,60}}?Total:\s*(\d+)",
        page_text,
        re.I,
    ):
        tardies += int(m.group(1))

    return {
        "absences": best["absences"],
        "tardies": tardies if tardies else 0,
        "excused": None,
        "unexcused": None,
        "days_enrolled": best["days_enrolled"],
        "days_present": best["days_present"],
        "year": best["year"],
        "school": best.get("school"),
        "source": "AttendanceHistory.aspx",
        "year_source": "calendar" if prefer_year and best["year"] == prefer_year else "latest_card",
    }


def parse_attendance_html(html, source_hint="", prefer_year=None):
    """Extract absence/tardy counts from Attendance or AttendanceHistory HTML."""
    if not html:
        return None

    prefer_year = prefer_year or target_attendance_year_label()

    # Mid-year live strip only when it matches the calendar year we want
    # (summer often zeros this out for the *new* year — ignore zeros)
    summary = _parse_att_summary_area(html)
    if summary and (summary.get("days_enrolled") or 0) > 0:
        summary["year"] = summary.get("year") or prefer_year
        return summary

    history = _parse_history_year_cards(html, prefer_year=prefer_year)
    if history and history.get("days_enrolled") is not None:
        return history

    return None


def fetch_attendance(session, prefer_year=None):
    """Return attendance for the calendar-aligned school year."""
    prefer_year = prefer_year or target_attendance_year_label()
    if prefer_year:
        print(f"  Attendance target year: {prefer_year}")

    hist = session.get(
        f"{BASE_URL}/student/AttendanceHistory.aspx",
        allow_redirects=True,
        timeout=90,
    )
    if hist.status_code == 200 and "LoginParent" not in hist.url and "NotFound" not in hist.url:
        parsed = parse_attendance_html(hist.text, "history", prefer_year=prefer_year)
        if parsed and parsed.get("absences") is not None:
            return parsed

    att = session.get(
        f"{BASE_URL}/student/Attendance.aspx",
        allow_redirects=True,
        timeout=60,
    )
    if att.status_code != 200 or "LoginParent" in att.url:
        print("  Attendance pages unavailable")
        return None
    parsed = parse_attendance_html(att.text, "attendance", prefer_year=prefer_year)
    if not parsed:
        print("  Attendance page loaded but no counts parsed")
        return {
            "absences": None,
            "tardies": None,
            "parse_failed": True,
            "year": prefer_year,
            "source": "Attendance.aspx",
        }
    return parsed


def refresh_attendance_only():
    """Login and update attendance on existing grades_data (works in summer)."""
    require_scrape_config()
    if not OUTPUT_FILE.exists():
        print(f"ERROR: {OUTPUT_FILE} not found — run a full scrape first")
        sys.exit(1)
    try:
        data = json.loads(OUTPUT_FILE.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: Could not parse {OUTPUT_FILE}: {e}")
        sys.exit(1)

    cal = load_school_calendar(refresh=True)
    prefer_year = target_attendance_year_label(calendar=cal)
    print(f"Refreshing attendance only (target year {prefer_year})...")

    session = login()
    students = data.get("students") or []
    by_sn = {str(s.get("sn")): s for s in students}

    for student in STUDENTS:
        sn = str(student["sn"])
        name = student.get("name", sn)
        print(f"  {name}...")
        switch_student(session, student["school_code"], student["sn"])
        att = fetch_attendance(session, prefer_year=prefer_year)
        if att:
            print(
                f"    year={att.get('year')} absences={att.get('absences')} "
                f"tardies={att.get('tardies')} present={att.get('days_present')}/"
                f"{att.get('days_enrolled')}"
            )
            if sn in by_sn:
                by_sn[sn]["attendance"] = att
            else:
                students.append({
                    "sn": student["sn"],
                    "name": name,
                    "school_code": student["school_code"],
                    "classes": [],
                    "assignments_by_class": [],
                    "attendance": att,
                })
                by_sn[sn] = students[-1]
        else:
            print("    WARNING: no attendance parsed")

    data["students"] = students
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    nxt = next_first_day(calendar=cal)
    paused = is_calendar_summer_break(calendar=cal)
    data["summer_break"] = paused
    data["school_session"] = {
        "active": not paused,
        "reason": "TUSD school calendar" if paused else "in session",
        "next_first_day": nxt.isoformat() if nxt else None,
        "attendance_year": prefer_year,
        "calendar_source": cal.get("source_url"),
    }
    OUTPUT_FILE.write_text(json.dumps(data, indent=2))
    print(f"\nAttendance written to {OUTPUT_FILE}")


def probe_attendance():
    """Login and dump Attendance-related page structure for each student (debug)."""
    require_scrape_config()
    session = login()
    paths = [
        "/student/Attendance.aspx",
        "/student/AttendanceHistory.aspx",
        "/student/StudentAttendanceHistory.aspx",
        "/student/AttendanceSummary.aspx",
    ]
    for student in STUDENTS:
        print(f"\n=== PROBE attendance: {student['name']} (SN {student['sn']}) ===")
        switch_student(session, student["school_code"], student["sn"])
        for path in paths:
            resp = session.get(f"{BASE_URL}{path}", allow_redirects=True, timeout=60)
            print(f"\n-- {path} status={resp.status_code} final={resp.url} len={len(resp.text)}")
            if "LoginParent" in resp.url or "NotFound" in resp.url or "Error" in resp.url:
                print("  skip (login/error)")
                continue
            html = resp.text
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text("\n", strip=True)
            # Prefer content after main "Attendance" heading if present
            idx = text.lower().rfind("would you like to take a tour")
            body = text[idx + 40 :] if idx >= 0 else text
            # Also try to find summary blocks
            print("--- body sample ---")
            print(body[:2500])
            # Links containing attendance
            for a in soup.find_all("a", href=True):
                label = a.get_text(" ", strip=True)
                href = a["href"]
                if re.search(r"attend", href + " " + label, re.I) and label:
                    print(f"  link: {label[:60]!r} -> {href[:90]}")
            # Title attributes / legends often hold totals
            for el in soup.find_all(attrs={"title": True})[:30]:
                t = el.get("title") or ""
                if re.search(r"absent|tardy|excused|total", t, re.I):
                    print(f"  title: {t[:120]!r}")
            # Any element with id/class containing attend
            for el in soup.find_all(True):
                cid = " ".join(filter(None, [el.get("id"), " ".join(el.get("class") or [])]))
                if re.search(r"attend|absent|tardy", cid, re.I):
                    snippet = el.get_text(" ", strip=True)[:100]
                    if snippet:
                        print(f"  node {cid[:60]!r}: {snippet!r}")
            parsed = parse_attendance_html(html)
            print("--- parsed ---")
            print(json.dumps(parsed, indent=2))
            # Save a redacted length marker for calendar codes
            codes = re.findall(r'\bclass="[^"]*absent[^"]*"', html, re.I)
            print(f"  class*=absent attrs: {len(codes)}")
            codes2 = re.findall(r"title=\"[^\"]{0,80}\"", html)
            interesting = [c for c in codes2 if re.search(r"absent|tardy|code", c, re.I)]
            print(f"  interesting titles: {interesting[:15]}")



def fetch_gradebook_summary(session):
    """Fetch GradebookSummary page; return list of (label_hint, series) pairs.

    Soft-fails to [] if the page structure changes — daily snapshots still work.
    """
    resp = session.get(f"{BASE_URL}/student/GradebookSummary.aspx")
    if resp.status_code != 200:
        return []

    # Course names often appear near chart setup: createScatterChart('ID', 'Name', 'S')
    chart_names = re.findall(
        r"createScatterChart\(\s*'[^']+'\s*,\s*'([^']+)'\s*,",
        resp.text,
    )

    series_list = []
    for match in re.finditer(
        r'var\s+\w+\s*=\s*(\[\s*\{\s*"overallDate"[\s\S]*?\]);', resp.text
    ):
        try:
            data = json.loads(match.group(1))
            if isinstance(data, list) and data:
                series_list.append(data)
        except json.JSONDecodeError:
            pass

    labeled = []
    for i, series in enumerate(series_list):
        hint = chart_names[i] if i < len(chart_names) else ""
        labeled.append((hint, series))
    return labeled


def normalize_aeries_series(raw_series):
    """Turn Aeries overallDate points into [{date, pct}, ...]."""
    points = []
    for pt in raw_series or []:
        if not isinstance(pt, dict):
            continue
        date_raw = pt.get("overallDate") or pt.get("date") or pt.get("Date")
        pct_raw = (
            pt.get("overallScore")
            or pt.get("overallPercent")
            or pt.get("score")
            or pt.get("percent")
            or pt.get("Percent")
            or pt.get("y")
        )
        if date_raw is None or pct_raw is None:
            continue
        pct = safe_float(pct_raw)
        if pct is None:
            continue
        # Normalize date to YYYY-MM-DD when possible
        date_str = str(date_raw).strip()
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
            try:
                date_str = datetime.strptime(date_str[:10], fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue
        points.append({"date": date_str, "pct": round(pct, 1)})
    points.sort(key=lambda p: p["date"])
    return points


def match_aeries_series_to_classes(classes, labeled_series):
    """Map course_name -> slim pct series from GradebookSummary when possible."""
    by_class = {}
    if not labeled_series:
        return by_class

    graded = [c for c in classes if c.get("percent")]
    used = set()

    for course in graded:
        name = course.get("course_name", "")
        name_norm = re.sub(r"[^a-z0-9]", "", name.lower())
        best_i = None
        best_len = 0
        for i, (hint, series) in enumerate(labeled_series):
            if i in used:
                continue
            hint_norm = re.sub(r"[^a-z0-9]", "", (hint or "").lower())
            if not hint_norm or not name_norm:
                continue
            if name_norm in hint_norm or hint_norm in name_norm:
                if len(hint_norm) > best_len:
                    best_i = i
                    best_len = len(hint_norm)
        if best_i is not None:
            used.add(best_i)
            points = normalize_aeries_series(labeled_series[best_i][1])
            if points:
                by_class[name] = points

    # Fallback: assign remaining series in order to unmatched graded classes
    remaining = [i for i in range(len(labeled_series)) if i not in used]
    unmatched = [c for c in graded if c.get("course_name") not in by_class]
    for course, i in zip(unmatched, remaining):
        points = normalize_aeries_series(labeled_series[i][1])
        if points:
            by_class[course.get("course_name", "")] = points

    return by_class


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


# ── Grade history (multi-day memory across scrapes) ──────────────────────────


def load_grade_history():
    if not HISTORY_FILE.exists():
        return {"students": {}}
    try:
        data = json.loads(HISTORY_FILE.read_text())
        if not isinstance(data, dict):
            return {"students": {}}
        data.setdefault("students", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"students": {}}


def save_grade_history(history):
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def _parse_iso_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def snapshot_missing_names(class_analytics_entry):
    """Top missing assignment names for history (by points, then name)."""
    missing = list(class_analytics_entry.get("missing_assignments") or [])
    missing.sort(
        key=lambda m: (
            -(m.get("points_possible") or 0),
            m.get("name") or "",
        )
    )
    names = []
    for m in missing[:HISTORY_MISSING_NAMES_CAP]:
        name = (m.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def build_student_snapshot(student_data, class_analytics_list, captured_at=None):
    """Compact daily snapshot for one student."""
    now = captured_at or datetime.now(timezone.utc)
    date_str = now.astimezone().strftime("%Y-%m-%d") if now.tzinfo else now.strftime("%Y-%m-%d")
    classes = {}
    for c in class_analytics_list:
        name = c.get("course_name") or ""
        if not name:
            continue
        trend = c.get("trend") or {}
        classes[name] = {
            "period": c.get("period"),
            "pct": c.get("current_grade_pct"),
            "mark": c.get("current_grade_mark") or "",
            "missing_count": len(c.get("missing_assignments") or []),
            "missing_names": snapshot_missing_names(c),
            "aeries_trend": trend.get("direction") if isinstance(trend, dict) else None,
        }
    prev_brief = None
    ai = student_data.get("ai_summary") or {}
    if ai.get("headline") or ai.get("focus_tonight"):
        prev_brief = {
            "headline": (ai.get("headline") or "").strip(),
            "focus_tonight": (ai.get("focus_tonight") or "").strip(),
        }
    snap = {
        "date": date_str,
        "captured_at": now.isoformat() if hasattr(now, "isoformat") else str(now),
        "classes": classes,
    }
    if prev_brief:
        snap["previous_briefing"] = prev_brief
    return snap


def upsert_student_snapshot(history, student_sn, student_name, snapshot):
    """One snapshot per student per calendar day; trim old days."""
    students = history.setdefault("students", {})
    entry = students.setdefault(str(student_sn), {"name": student_name, "snapshots": []})
    entry["name"] = student_name or entry.get("name", "")
    snaps = entry.get("snapshots") or []
    date_str = snapshot.get("date")
    snaps = [s for s in snaps if s.get("date") != date_str]
    snaps.append(snapshot)
    snaps.sort(key=lambda s: s.get("date") or "")
    if len(snaps) > HISTORY_MAX_DAYS:
        snaps = snaps[-HISTORY_MAX_DAYS:]
    entry["snapshots"] = snaps
    return history


def _class_point_on_or_before(snapshots, course_name, target_date, min_age_days=0, as_of=None):
    """Most recent snapshot on or before target_date for a class.

    If min_age_days > 0, the snapshot must be at least that many days before as_of
    (so a 1-day-old point is not treated as a 7-day comparison).
    Returns (snapshot_date, class_dict) or (None, None).
    """
    as_of = as_of or datetime.now().date()
    best = None
    best_date = None
    for snap in snapshots:
        d = _parse_iso_date(snap.get("date"))
        if not d or d > target_date:
            continue
        if min_age_days and (as_of - d).days < min_age_days:
            continue
        cls = (snap.get("classes") or {}).get(course_name)
        if cls is None:
            continue
        best = cls
        best_date = d
    return best_date, best


def _pct_delta(current_pct, past_cls):
    if current_pct is None or not past_cls:
        return None
    past_pct = past_cls.get("pct")
    if past_pct is None:
        return None
    return round(current_pct - past_pct, 1)


def trend_label_from_delta(delta):
    if delta is None:
        return "insufficient_history"
    if delta >= TREND_DELTA_THRESHOLD:
        return "improving"
    if delta <= -TREND_DELTA_THRESHOLD:
        return "slipping"
    return "stable"


def build_history_context(student_sn, student_data, class_analytics_list, history):
    """Multi-day deltas and chronic missing for Grok + UI."""
    entry = (history.get("students") or {}).get(str(student_sn), {})
    snapshots = list(entry.get("snapshots") or [])
    today = datetime.now().date()

    # Include today's in-memory analytics as the "current" point even before write
    current_by_class = {
        c["course_name"]: c for c in class_analytics_list if c.get("course_name")
    }

    if not snapshots and not current_by_class:
        return {
            "history_span_days": 0,
            "snapshot_count": 0,
            "classes": {},
            "movers": [],
            "previous_briefing": None,
        }

    first_date = _parse_iso_date(snapshots[0]["date"]) if snapshots else today
    span = (today - first_date).days if first_date else 0

    previous_briefing = None
    for snap in reversed(snapshots):
        if snap.get("previous_briefing"):
            previous_briefing = snap["previous_briefing"]
            break
    # Prefer last stored ai_summary if regenerating same day without snapshot field
    if not previous_briefing:
        ai = student_data.get("ai_summary") or {}
        if ai.get("headline") or ai.get("focus_tonight"):
            previous_briefing = {
                "headline": (ai.get("headline") or "").strip(),
                "focus_tonight": (ai.get("focus_tonight") or "").strip(),
            }

    target_7 = today.fromordinal(today.toordinal() - 7)
    target_14 = today.fromordinal(today.toordinal() - 14)

    classes_ctx = {}
    movers = []

    for course_name, c in current_by_class.items():
        pct = c.get("current_grade_pct")
        missing_names = [
            (m.get("name") or "").strip()
            for m in (c.get("missing_assignments") or [])
            if (m.get("name") or "").strip()
        ]
        # Require ~5+ / ~10+ day-old points so short history isn't labeled as 7d/14d
        _, past_7 = _class_point_on_or_before(
            snapshots, course_name, target_7, min_age_days=5, as_of=today
        )
        _, past_14 = _class_point_on_or_before(
            snapshots, course_name, target_14, min_age_days=10, as_of=today
        )
        # Nearest older snapshot for chronic missing (any prior day)
        _, past_any = _class_point_on_or_before(
            snapshots, course_name, today.fromordinal(today.toordinal() - 1), as_of=today
        )

        delta_7 = _pct_delta(pct, past_7)
        delta_14 = _pct_delta(pct, past_14)

        ref_missing = past_7 or past_any or {}
        past_missing = set(ref_missing.get("missing_names") or [])
        now_missing = set(missing_names)
        chronic = sorted(past_missing & now_missing)
        resolved = sorted(past_missing - now_missing)

        # Full series for span delta; downsampled series for UI sparklines
        full_hist = []
        for snap in snapshots:
            cls = (snap.get("classes") or {}).get(course_name)
            if cls is None or cls.get("pct") is None:
                continue
            full_hist.append({"date": snap.get("date"), "pct": cls.get("pct")})
        if pct is not None:
            today_str = today.strftime("%Y-%m-%d")
            if not full_hist:
                full_hist.append({"date": today_str, "pct": pct})
            elif full_hist[-1].get("date") == today_str:
                full_hist[-1] = {"date": today_str, "pct": pct}
            elif full_hist[-1].get("pct") != pct:
                full_hist.append({"date": today_str, "pct": pct})

        pct_history = downsample_pct_history(full_hist, PCT_HISTORY_UI_POINTS)

        delta_span = None
        span_window = None
        if len(full_hist) >= 2 and pct is not None:
            oldest_pct = full_hist[0].get("pct")
            oldest_date = _parse_iso_date(full_hist[0].get("date"))
            if oldest_pct is not None and oldest_date:
                age = (today - oldest_date).days
                if age >= 14:
                    delta_span = round(pct - oldest_pct, 1)
                    span_window = f"{age}d"

        label = trend_label_from_delta(
            delta_7 if delta_7 is not None else (
                delta_14 if delta_14 is not None else delta_span
            )
        )

        classes_ctx[course_name] = {
            "pct_now": pct,
            "pct_7d_ago": (past_7 or {}).get("pct"),
            "pct_14d_ago": (past_14 or {}).get("pct"),
            "delta_7d": delta_7,
            "delta_14d": delta_14,
            "delta_span": delta_span,
            "span_window": span_window,
            "missing_count_now": len(missing_names),
            "missing_count_7d_ago": (past_7 or {}).get("missing_count"),
            "chronic_missing": chronic[:HISTORY_MISSING_NAMES_CAP],
            "resolved_missing": resolved[:HISTORY_MISSING_NAMES_CAP],
            "trend_label": label,
            "pct_history": pct_history,
        }

        delta_for_mover = None
        mover_window = None
        for candidate, window in (
            (delta_7, "7d"),
            (delta_14, "14d"),
            (delta_span, span_window or "term"),
        ):
            if candidate is not None and abs(candidate) >= TREND_DELTA_THRESHOLD:
                delta_for_mover = candidate
                mover_window = window
                break
        if delta_for_mover is not None:
            movers.append({
                "class_name": course_name,
                "delta": delta_for_mover,
                "window": mover_window,
                "pct_now": pct,
            })

    movers.sort(key=lambda m: abs(m["delta"]), reverse=True)

    return {
        "history_span_days": span,
        "snapshot_count": len(snapshots),
        "classes": classes_ctx,
        "movers": movers[:6],
        "previous_briefing": previous_briefing,
    }


def attach_ui_trend_fields(student_data, history_context, aeries_series_by_class=None):
    """Write slim trend fields onto student payload for the static dashboard."""
    aeries_series_by_class = aeries_series_by_class or {}
    classes_ctx = history_context.get("classes") or {}

    trend_summary = []
    for m in history_context.get("movers") or []:
        trend_summary.append({
            "class_name": m["class_name"],
            "delta": m["delta"],
            "window": m.get("window", "7d"),
            "pct_now": m.get("pct_now"),
            "direction": "up" if m["delta"] > 0 else "down",
        })
    # Fill remaining strip slots with next-largest measurable deltas
    if len(trend_summary) < 4:
        already = {t["class_name"] for t in trend_summary}
        scored = []
        for name, ctx in classes_ctx.items():
            if name in already:
                continue
            d = ctx.get("delta_7d")
            window = "7d"
            if d is None or d == 0:
                d14 = ctx.get("delta_14d")
                if d14 is not None and d14 != 0:
                    d, window = d14, "14d"
                elif ctx.get("delta_span") is not None:
                    d = ctx.get("delta_span")
                    window = ctx.get("span_window") or "term"
            if d is None or d == 0:
                continue
            scored.append((abs(d), name, d, window, ctx.get("pct_now")))
        scored.sort(reverse=True)
        for _, name, d, window, pct_now in scored:
            if len(trend_summary) >= 4:
                break
            trend_summary.append({
                "class_name": name,
                "delta": d,
                "window": window,
                "pct_now": pct_now,
                "direction": "up" if d > 0 else ("down" if d < 0 else "same"),
            })

    student_data["trend_summary"] = trend_summary[:4]
    student_data["history_span_days"] = history_context.get("history_span_days", 0)

    # Attach per-class pct_history onto matching class meta for UI
    class_history = {}
    for name, ctx in classes_ctx.items():
        hist = list(ctx.get("pct_history") or [])
        aeries = aeries_series_by_class.get(name) or []
        # Prefer multi-day scrape history; fall back to denser Aeries series for sparklines
        if len(hist) < 2 and len(aeries) >= 2:
            hist = aeries[-PCT_HISTORY_UI_POINTS:]
        class_history[name] = {
            "pct_history": hist,
            "delta_7d": ctx.get("delta_7d"),
            "delta_14d": ctx.get("delta_14d"),
            "delta_span": ctx.get("delta_span"),
            "span_window": ctx.get("span_window"),
            "trend_label": ctx.get("trend_label"),
        }
    # Classes only present in Aeries series
    for name, series in aeries_series_by_class.items():
        if name not in class_history and series:
            class_history[name] = {
                "pct_history": series[-PCT_HISTORY_UI_POINTS:],
                "delta_7d": None,
                "delta_14d": None,
                "trend_label": "insufficient_history",
            }

    student_data["class_trends"] = class_history
    return student_data


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

    overdue_days = []
    for a in missing:
        due = parse_due_date(a.get("due_date"))
        if due:
            overdue_days.append((today - due).days)
    oldest_missing_days = max(overdue_days) if overdue_days else None
    median_missing_days = None
    if overdue_days:
        ordered = sorted(overdue_days)
        mid = len(ordered) // 2
        median_missing_days = (
            ordered[mid]
            if len(ordered) % 2
            else round((ordered[mid - 1] + ordered[mid]) / 2)
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
        "oldest_missing_days": oldest_missing_days,
        "median_missing_days": median_missing_days,
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


def build_class_analytics(student_data, history_context=None):
    """Pre-compute per-class facts for Grok interpretation."""
    today = datetime.now()
    student_name = student_data.get("name", "Student")
    class_analytics = []
    history_context = history_context or {}
    hist_classes = history_context.get("classes") or {}

    for class_meta in student_data.get("classes", []):
        if not class_meta.get("percent"):
            continue
        period = class_meta.get("period")
        assignments = assignments_for_period(student_data, period)
        analyzed = analyze_class(class_meta, assignments, today)
        # Fold multi-day history onto each class for Grok
        h = hist_classes.get(analyzed["course_name"]) or {}
        if h:
            analyzed["history"] = {
                "delta_7d": h.get("delta_7d"),
                "delta_14d": h.get("delta_14d"),
                "pct_7d_ago": h.get("pct_7d_ago"),
                "pct_14d_ago": h.get("pct_14d_ago"),
                "trend_label": h.get("trend_label"),
                "chronic_missing": h.get("chronic_missing") or [],
                "resolved_missing": h.get("resolved_missing") or [],
                "missing_count_7d_ago": h.get("missing_count_7d_ago"),
            }
        class_analytics.append(analyzed)

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
        h = c.get("history") or {}
        if h.get("trend_label") == "improving" and h.get("delta_7d") is not None:
            wins.append({
                "class_name": c["course_name"],
                "grade": f"{c.get('current_grade_mark') or ''} {c['current_grade_pct']}%".strip(),
                "note": f"up {h['delta_7d']} pts vs ~1 week ago",
            })
        elif (
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

    result = {
        "student_name": student_name,
        "date": today.strftime("%A, %B %d, %Y"),
        "dominant_theme": dominant_theme,
        "total_recoverable_pts": round(total_recoverable, 1),
        "classes_needing_focus": classes_needing_focus,
        "wins_hints": wins[:3],
        "classes": class_analytics,
        "data_scope": (
            "Aeries parent portal only — current grades, posted missing work, "
            "scored assignments, and short portal forecasts. Not a full picture of "
            "work done offline or not yet graded."
        ),
    }
    if history_context:
        result["history_meta"] = {
            "history_span_days": history_context.get("history_span_days", 0),
            "snapshot_count": history_context.get("snapshot_count", 0),
            "movers": history_context.get("movers") or [],
            "previous_briefing": history_context.get("previous_briefing"),
        }
    return result


def count_graded_classes(student_data):
    return sum(
        1
        for c in student_data.get("classes") or []
        if c.get("percent") not in (None, "")
    )


def empty_term_summary(student_data):
    """Static briefing when the portal has no graded classes (e.g. summer)."""
    student_name = student_data.get("name", "the student")
    return {
        "headline": (
            f"School year is out — the portal has no current classes or missing work "
            f"for {student_name}."
        ),
        "focus_tonight": "",
        "wins": [],
        "classes": [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "analytics_snapshot": {
            "dominant_theme": "empty_term",
            "total_recoverable_pts": 0,
            "empty_term": True,
        },
    }


def generate_ai_summary(student_data, history_context=None):
    """Call Grok API to generate a unified family daily briefing."""
    # No graded classes (common in summer) — never invent "mixed/stable performance"
    if count_graded_classes(student_data) == 0:
        return empty_term_summary(student_data)

    if not GROK_API_KEY:
        return None

    student_name = student_data.get("name", "the student")
    history_context = history_context or {}
    analytics = build_class_analytics(student_data, history_context=history_context)

    has_history = (history_context.get("snapshot_count") or 0) >= 1 or bool(
        history_context.get("movers")
    )

    context = (
        "PRECOMPUTED_ANALYTICS (source of truth — do not redo arithmetic):\n"
        f"{json.dumps(analytics, indent=2)}\n\n"
        "Write one tight briefing from these facts only. "
        "Name real assignments and scores. Do not invent data. "
        "Do not restate full assignment lists the dashboard already shows — interpret them."
    )
    if has_history:
        context += (
            "\n\nHISTORY_CONTEXT is embedded under each class as \"history\" and "
            "history_meta (multi-day scrape memory). Use it for week-scale movement, "
            "chronic vs new missing items, and genuine improving/slipping wins. "
            "If history is thin, do not invent multi-week stories."
        )

    today = datetime.now()
    today_dow = today.strftime("%A")
    system_prompt = f"""You are writing a daily grade briefing for {student_name}'s family. One output only — parent and kids may both read it. Direct facts at a glance. Not a coaching script, not separate parent vs student advice.

Today is {today_dow}, {today.strftime('%B %d, %Y')}.

DATA SCOPE (hard limits)
- Facts come only from the Aeries parent portal: posted grades, missing work, scored assignments, portal forecast, and multi-day scrape history when present.
- The portal can lag reality (turned-in but not graded, offline work). Prefer "portal still shows … missing" over "never did …".
- Do NOT invent causes (motivation, home, teacher fairness, effort).
- Do NOT assume prior advice was followed unless history shows a grade or missing-work change.
- Without multi-day history, only use within-term assignment trajectory / Aeries forecast — never invent "over the past month" narratives.

You receive PRECOMPUTED_ANALYTICS. Your job is INTERPRETATION only — no math, no full assignment dumps.

VOICE
- Third person with "{student_name}" (never "you", never "as a parent…")
- One shared tone: plain English, specific, calm, not accusatory
- No jargon: never say completion_gap, performance_pattern, recoverable points, dominant_theme, issue_type, or "Pattern suggests"
- Short sentences. Prefer the 1–2 highest-leverage missing items over listing everything
- No parent tips, conversation openers, or separate student pep talks

Respond ONLY with valid JSON matching this exact schema:
{{
  "headline": "ONE sentence anyone can skim — the cross-class story, not every grade",
  "focus_tonight": "Single highest-leverage next action: named assignment(s) + class. Empty string if nothing urgent",
  "wins": ["0–2 short genuine positives with evidence — grade up vs last week, strong class, good recent score"],
  "classes": [
    {{
      "class_name": "must match course_name from analytics",
      "urgency": "critical | watch | ok | strong",
      "snap": "≤12 words for the closed card line — why this class matters right now",
      "story": "1–2 plain sentences: what's going on from portal data. Do NOT dump the full missing list",
      "do_tonight": "Concrete next step — named assignment(s). Empty string if nothing needed"
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
- If analytics.classes is empty: headline must say the portal has no current classes / school is out — do NOT say "mixed or stable performance" or invent status
- Order classes: critical → watch → ok → strong
- For ok/strong: short snap + brief story; leave do_tonight empty
- For critical/watch: fill do_tonight when there is a concrete portal action; name specific assignments from missing_assignments
- Phrase actions as what the portal still needs (e.g. submit X), not moral judgments
- If history.chronic_missing is set, you may note those items are still open in the portal
- If history.delta_7d / trend_label show clear movement, one plain phrase is enough
- Do NOT invent scores, assignments, or causes
- Do NOT list every missing assignment in story
- Do NOT write forecast math lectures
- wins must be real and specific — skip empty praise
- Prefer empty strings over filler
- Never output parent_tip, ask, parent_support, or similar fields"""

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
                    {"role": "user", "content": context},
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
        # Strip any legacy parent/student coaching fields if the model still emits them
        for cls in summary.get("classes") or []:
            if isinstance(cls, dict):
                cls.pop("parent_tip", None)
                cls.pop("ask", None)
                cls.pop("parent_support", None)
                cls.pop("student_action", None)
        summary["generated_at"] = datetime.now(timezone.utc).isoformat()
        summary["analytics_snapshot"] = {
            "dominant_theme": analytics.get("dominant_theme"),
            "total_recoverable_pts": analytics.get("total_recoverable_pts"),
            "history_span_days": (history_context or {}).get("history_span_days"),
        }
        return summary

    except requests.exceptions.Timeout:
        print("  WARNING: Grok API timed out")
        return None
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"  WARNING: Failed to parse Grok response: {e}")
        return None


def _prepare_student_for_briefing(student_data, history, aeries_series_by_class=None):
    """Build analytics, history context, UI trend fields; return history_context."""
    # First pass: class analytics without history (for snapshot + context base)
    base_analytics = build_class_analytics(student_data, history_context=None)
    class_list = base_analytics.get("classes") or []

    history_context = build_history_context(
        student_data.get("sn"),
        student_data,
        class_list,
        history,
    )
    attach_ui_trend_fields(
        student_data,
        history_context,
        aeries_series_by_class=aeries_series_by_class or {},
    )
    return history_context, class_list


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

    history = load_grade_history()
    print(f"Regenerating AI briefings for {len(students)} student(s) from {OUTPUT_FILE.name}...")

    for student in students:
        name = student.get("name", "Student")
        print(f"  {name}...")
        # Keep prior briefing for previous_briefing context before overwrite
        history_context, _ = _prepare_student_for_briefing(student, history)
        ai_summary = generate_ai_summary(student, history_context=history_context)
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

    # Preserve prior AI briefings for history "previous_briefing" notes
    previous_by_sn = {
        str(s.get("sn")): s
        for s in (previous_data.get("students") or [])
        if s.get("sn") is not None
    }

    summer_break = is_summer_break(previous_data)

    if summer_break:
        cal = load_school_calendar(refresh=True)
        nxt = next_first_day(calendar=cal)
        prefer_year = target_attendance_year_label(calendar=cal)
        reason = "SUMMER_BREAK env override" if env_truthy("SUMMER_BREAK") is True else "TUSD school calendar"
        print(f"School session paused ({reason}).")
        if nxt:
            print(f"Next first day of school: {nxt.isoformat()}")
        print(f"Skipping grades scrape and Grok; refreshing attendance for {prefer_year} only.")
        # Preserve classes/AI; only update attendance from portal
        if OUTPUT_FILE.exists() and previous_data.get("students"):
            try:
                refresh_attendance_only()
                return
            except SystemExit:
                raise
            except Exception as e:
                print(f"  WARNING: attendance refresh failed ({e}); preserving prior data")
        data = previous_data.copy() if previous_data else {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "district": "Tustin USD",
            "portal_url": BASE_URL,
            "students": [],
            "summer_break": True,
        }
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        data["summer_break"] = True
        data["school_session"] = {
            "active": False,
            "reason": reason,
            "next_first_day": nxt.isoformat() if nxt else None,
            "attendance_year": prefer_year,
            "calendar_source": cal.get("source_url"),
        }
        OUTPUT_FILE.write_text(json.dumps(data, indent=2))
        print(f"Data preserved (no new grade scrape). Written to {OUTPUT_FILE}")
        return

    session = login()
    history = load_grade_history()
    cal = load_school_calendar(refresh=False)
    session_year = school_session_window(calendar=cal)
    data = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "district": "Tustin USD",
        "portal_url": BASE_URL,
        "students": [],
        "summer_break": False,
        "school_session": {
            "active": True,
            "year_id": (session_year or {}).get("id"),
            "first_day": (session_year or {}).get("first_day"),
            "last_day": (session_year or {}).get("last_day"),
            "calendar_source": cal.get("source_url"),
        },
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

        # Optional within-term grade curves from GradebookSummary
        print("  Fetching gradebook trend series...")
        labeled_series = fetch_gradebook_summary(session)
        aeries_series = match_aeries_series_to_classes(classes, labeled_series)
        if aeries_series:
            print(f"  Aeries series for {len(aeries_series)} class(es)")
        else:
            print("  Aeries series unavailable (using daily snapshots only)")

        print("  Fetching attendance...")
        attendance = fetch_attendance(session)
        if attendance and not attendance.get("parse_failed"):
            print(
                f"  Attendance: absences={attendance.get('absences')} "
                f"tardies={attendance.get('tardies')}"
            )
        elif attendance and attendance.get("parse_failed"):
            print("  Attendance: page OK, counts not parsed yet")
        else:
            print("  Attendance: skipped")

        student_data = {
            "sn": student["sn"],
            "name": student["name"],
            "school_code": student["school_code"],
            "classes": classes,
            "assignments_by_class": class_assignments,
            "attendance": attendance,
        }

        # Carry prior briefing so history_context can reference last focus
        prior = previous_by_sn.get(str(student["sn"])) or {}
        if prior.get("ai_summary"):
            student_data["ai_summary"] = prior["ai_summary"]

        history_context, class_list = _prepare_student_for_briefing(
            student_data, history, aeries_series_by_class=aeries_series
        )

        if GROK_API_KEY:
            print("  Generating AI summary...")
            ai_summary = generate_ai_summary(
                student_data, history_context=history_context
            )
            if ai_summary:
                student_data["ai_summary"] = ai_summary
                print("  AI summary generated")
            else:
                print("  AI summary skipped")
                # Drop stale prior briefing if regenerate failed and we only carried it for context
                if prior.get("ai_summary") and student_data.get("ai_summary") is prior.get("ai_summary"):
                    pass  # keep previous briefing on failure

        # Append today's grade snapshot after briefing (stores previous_briefing from prior AI)
        # Prefer storing the briefing we just replaced as previous — rebuild snapshot with prior AI
        snap_source = dict(student_data)
        if prior.get("ai_summary"):
            snap_source["ai_summary"] = prior["ai_summary"]
        snapshot = build_student_snapshot(snap_source, class_list)
        upsert_student_snapshot(
            history, student["sn"], student["name"], snapshot
        )
        print(f"  History snapshots: {len((history.get('students') or {}).get(str(student['sn']), {}).get('snapshots') or [])}")

        data["students"].append(student_data)

    data["summer_break"] = False

    save_grade_history(history)
    OUTPUT_FILE.write_text(json.dumps(data, indent=2))
    print(f"\nData written to {OUTPUT_FILE}")
    print(f"History written to {HISTORY_FILE}")
    print(f"Last updated: {data['last_updated']}")
    print(f"Summer break: {data['summer_break']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aeries grade scraper + Grok briefings")
    parser.add_argument(
        "--grok-only",
        action="store_true",
        help="Regenerate AI briefings from existing grades_data.json (skip Aeries scrape)",
    )
    parser.add_argument(
        "--probe-attendance",
        action="store_true",
        help="Login and dump Attendance page structure (no grades_data write)",
    )
    args = parser.parse_args()

    if args.probe_attendance:
        probe_attendance()
    elif args.grok_only:
        regenerate_grok_summaries()
    else:
        scrape_all()