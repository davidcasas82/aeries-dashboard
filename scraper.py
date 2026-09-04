import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://tustinusd.aeries.net"
EMAIL = os.getenv("AERIES_EMAIL", "")
PASSWORD = os.getenv("AERIES_PASSWORD", "")
GROK_API_KEY = os.getenv("GROK_API_KEY", "")
GROK_API_URL = "https://api.x.ai/v1/chat/completions"
GROK_MODEL = "grok-4"
PACIFIC = ZoneInfo("America/Los_Angeles")
DEFAULT_HTTP_TIMEOUT = 60
LOGIN_TIMEOUT_ATTEMPTS = 3
LOGIN_RETRY_DELAY_SEC = 2
_LOGIN_TIMEOUTS = (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout)

# Month-day cutovers for TUSD 6-12 gradebook term selection (overridable in school_calendar.json)
_DEFAULT_TERM_CUTOVERS = {
    "q1_last_md": "10-12",
    "fall_last_md": "01-04",
    "q3_last_md": "03-15",
}


class ScrapeError(Exception):
    """Fatal config/login error; CLI exits 1."""


class TimeoutSession(requests.Session):
    """requests.Session that always applies a timeout unless the caller set one."""

    def __init__(self, timeout=DEFAULT_HTTP_TIMEOUT):
        super().__init__()
        self._default_timeout = timeout

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", self._default_timeout)
        return super().request(method, url, **kwargs)


def pacific_now():
    return datetime.now(PACIFIC)


def pacific_today():
    """School-day calendar date in Pacific time (not UTC)."""
    return pacific_now().date()


def pacific_today_dt():
    """Naive midnight Pacific today, for due-date math against naive Aeries dates."""
    return datetime.combine(pacific_today(), datetime.min.time())

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
            data["source_url"] = TUSD_CALENDAR_URL
            # Only rewrite the committed file when year dates actually change.
            # CI should not churn updated_at on every scrape.
            prev_years = []
            if CALENDAR_FILE.exists():
                try:
                    prev_years = (json.loads(CALENDAR_FILE.read_text()) or {}).get("years") or []
                except (json.JSONDecodeError, OSError):
                    prev_years = []
            years_changed = [
                (y.get("id"), y.get("first_day"), y.get("last_day")) for y in data.get("years") or []
            ] != [
                (y.get("id"), y.get("first_day"), y.get("last_day")) for y in prev_years
            ]
            if years_changed and os.getenv("GITHUB_ACTIONS") != "true":
                data["updated_at"] = datetime.now(timezone.utc).date().isoformat()
                try:
                    CALENDAR_FILE.write_text(json.dumps(data, indent=2) + "\n")
                except OSError:
                    pass
    return data


def school_session_window(today=None, calendar=None):
    """Return the school-year dict containing today, or None if outside all windows (summer)."""
    today = today or pacific_today()
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
    today = today or pacific_today()
    if isinstance(today, datetime):
        today = today.date()
    calendar = calendar or load_school_calendar(refresh=False)
    candidates = []
    for y in calendar.get("years") or []:
        first = _parse_iso_date_str(y.get("first_day"))
        if first and first >= today:
            candidates.append(first)
    return min(candidates) if candidates else None


def _school_year_aeries_label(year_dict):
    """Turn calendar year dict into Aeries label like 2026-2027."""
    if not year_dict:
        return None
    first = _parse_iso_date_str(year_dict.get("first_day"))
    last = _parse_iso_date_str(year_dict.get("last_day"))
    if not first:
        return None
    end_y = last.year if last else first.year + 1
    return f"{first.year}-{end_y}"


# Days before first instructional day when we start showing *new* year attendance
# (schedules post early; last year's totals would otherwise stick around).
ATTENDANCE_UPCOMING_LEAD_DAYS = 30


def target_attendance_year_label(today=None, calendar=None):
    """Aeries-style year label (e.g. 2025-2026) for the school year we should display.

    - During a session: the active school year
    - Near / after the next first day (within ATTENDANCE_UPCOMING_LEAD_DAYS): upcoming year
      so early schedules don't still show last year's absences/tardies
    - Mid-summer otherwise: the year that most recently completed
    """
    today = today or pacific_today()
    if isinstance(today, datetime):
        today = today.date()
    calendar = calendar or load_school_calendar(refresh=False)

    active = school_session_window(today=today, calendar=calendar)
    if active:
        return _school_year_aeries_label(active)

    upcoming = None
    completed = []
    for y in calendar.get("years") or []:
        first = _parse_iso_date_str(y.get("first_day"))
        last = _parse_iso_date_str(y.get("last_day"))
        if not first:
            continue
        label = _school_year_aeries_label(y)
        if first > today:
            if upcoming is None or first < _parse_iso_date_str(upcoming.get("first_day")):
                upcoming = y
        elif last and last < today:
            completed.append((last, label))
        elif first <= today:
            # first day passed but outside last_day window shouldn't happen often
            completed.append((first, label))

    if upcoming:
        first = _parse_iso_date_str(upcoming.get("first_day"))
        if first and 0 <= (first - today).days <= ATTENDANCE_UPCOMING_LEAD_DAYS:
            return _school_year_aeries_label(upcoming)

    if completed:
        completed.sort(key=lambda t: t[0], reverse=True)
        return completed[0][1]

    if upcoming:
        return _school_year_aeries_label(upcoming)
    return None


def empty_attendance_for_year(year_label, reason="not_started"):
    """Zeros for a school year that has no Attendance History card yet."""
    return {
        "absences": 0,
        "tardies": 0,
        "excused": 0,
        "unexcused": 0,
        "days_enrolled": 0,
        "days_present": 0,
        "year": year_label,
        "school": None,
        "source": "default_empty",
        "year_source": reason,
        "not_started": True,
    }


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


def student_log_label(index, student=None):
    """CI-safe label — never print names or student numbers."""
    return f"student {index}"


def assignment_count(student_data):
    return sum(
        len(group.get("assignments") or [])
        for group in (student_data or {}).get("assignments_by_class") or []
    )


def portal_looks_incomplete(student_data, prior, in_session=True):
    """Return a reason string when a scrape looks like portal HTML drift, else None."""
    if not in_session or not prior:
        return None
    classes = [
        c for c in (student_data or {}).get("classes") or []
        if (c.get("course_name") or "").strip()
    ]
    prior_classes = [
        c for c in (prior.get("classes") or [])
        if (c.get("course_name") or "").strip()
    ]
    if prior_classes and not classes:
        return "class list empty after a prior scrape with classes"
    prev_n = assignment_count(prior)
    now_n = assignment_count(student_data)
    if prev_n >= 10 and now_n < prev_n * 0.5:
        return f"assignment count dropped {prev_n} → {now_n}"
    return None


def redact_probe_text(text, student):
    """Strip configured identifiers from probe dumps (public Actions logs)."""
    out = text or ""
    for val in (student.get("name"), student.get("sn"), EMAIL):
        if val:
            out = out.replace(str(val), "[redacted]")
    return out


def require_scrape_config():
    if not EMAIL or not PASSWORD:
        raise ScrapeError("AERIES_EMAIL and AERIES_PASSWORD must be set in .env")
    if not STUDENTS:
        raise ScrapeError("No students configured in .env (need STUDENT_1_SN at minimum)")


def _login_once():
    session = TimeoutSession()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    })

    login_url = f"{BASE_URL}/student/LoginParent.aspx"
    resp = session.get(login_url)
    if resp.status_code != 200:
        raise ScrapeError(f"Could not load login page (status {resp.status_code})")

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
        detail = error_match.group(1).strip() if error_match else "still on login page after submit"
        raise ScrapeError(f"Login failed — {detail}")

    print("Logged in successfully")
    return session


def login():
    """Log into Aeries. Retry ReadTimeout/ConnectTimeout a few times, then fail."""
    for attempt in range(1, LOGIN_TIMEOUT_ATTEMPTS + 1):
        try:
            return _login_once()
        except _LOGIN_TIMEOUTS:
            if attempt >= LOGIN_TIMEOUT_ATTEMPTS:
                raise
            print(
                f"Login timed out (attempt {attempt}/{LOGIN_TIMEOUT_ATTEMPTS}); retrying..."
            )
            time.sleep(LOGIN_RETRY_DELAY_SEC)


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
        # Do not fall back to another year when a specific year was requested —
        # that is what kept last year's absences/tardies after schedules posted.
        if best is None:
            return None
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
    prefer_norm = (prefer_year or "").replace("–", "-").replace(" ", "")

    # Live summary strip — only trust when it matches the year we want (or no prefer)
    summary = _parse_att_summary_area(html)
    if summary and (summary.get("days_enrolled") or 0) > 0:
        sy = (summary.get("year") or "").replace("–", "-").replace(" ", "")
        if sy and prefer_norm and sy != prefer_norm:
            pass  # wrong year (common mid-summer); fall through to history
        elif sy:
            summary["year"] = sy
            return summary
        elif prefer_norm and school_session_window() is not None:
            # Unlabelled strip during active session only
            summary["year"] = prefer_year
            return summary
        elif not prefer_norm:
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
        if prefer_year:
            print(f"  No attendance data for {prefer_year} — using empty baseline")
            return empty_attendance_for_year(prefer_year, reason="portal_unavailable")
        return None
    parsed = parse_attendance_html(att.text, "attendance", prefer_year=prefer_year)
    if not parsed:
        if prefer_year:
            print(f"  No attendance card for {prefer_year} yet — using empty baseline")
            return empty_attendance_for_year(prefer_year, reason="year_not_in_history")
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
        raise ScrapeError(f"{OUTPUT_FILE} not found — run a full scrape first")
    try:
        data = json.loads(OUTPUT_FILE.read_text())
    except json.JSONDecodeError as e:
        raise ScrapeError(f"Could not parse {OUTPUT_FILE}: {e}")

    cal = load_school_calendar(refresh=True)
    prefer_year = target_attendance_year_label(calendar=cal)
    print(f"Refreshing attendance only (target year {prefer_year})...")

    session = login()
    students = data.get("students") or []
    by_sn = {str(s.get("sn")): s for s in students}

    for i, student in enumerate(STUDENTS, start=1):
        sn = str(student["sn"])
        label = student_log_label(i)
        print(f"  {label}...")
        try:
            switch_student(session, student["school_code"], student["sn"])
            att = fetch_attendance(session, prefer_year=prefer_year)
        except Exception as e:
            print(f"    WARNING: attendance failed for {label} ({e})")
            continue
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
                    "name": student.get("name", f"Student {i}"),
                    "school_code": student["school_code"],
                    "classes": [],
                    "assignments_by_class": [],
                    "attendance": att,
                })
                by_sn[sn] = students[-1]
        else:
            print("    WARNING: no attendance parsed")

    for row in students:
        try:
            attach_student_view(row)
        except Exception as e:
            print(f"    WARNING: view rebuild skipped ({e})")

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
    for i, student in enumerate(STUDENTS, start=1):
        print(f"\n=== PROBE attendance: {student_log_label(i)} ===")
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
            print(redact_probe_text(body[:400], student))
            # Links containing attendance
            for a in soup.find_all("a", href=True):
                label = a.get_text(" ", strip=True)
                href = a["href"]
                if re.search(r"attend", href + " " + label, re.I) and label:
                    print(f"  link: {redact_probe_text(label[:60], student)!r} -> {href[:90]}")
            # Title attributes / legends often hold totals
            for el in soup.find_all(attrs={"title": True})[:30]:
                t = el.get("title") or ""
                if re.search(r"absent|tardy|excused|total", t, re.I):
                    print(f"  title: {redact_probe_text(t[:120], student)!r}")
            # Any element with id/class containing attend
            for el in soup.find_all(True):
                cid = " ".join(filter(None, [el.get("id"), " ".join(el.get("class") or [])]))
                if re.search(r"attend|absent|tardy", cid, re.I):
                    snippet = el.get_text(" ", strip=True)[:100]
                    if snippet:
                        print(f"  node {cid[:60]!r}: {redact_probe_text(snippet, student)!r}")
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


_GRADEBOOK_TERM_PATTERN = re.compile(
    r"(1st\s+Semester|2nd\s+Semester|First\s+Semester|Second\s+Semester|"
    r"Semester\s+[12]|Trimester\s+[123]|"
    r"1st\s+Quarter|2nd\s+Quarter|3rd\s+Quarter|4th\s+Quarter|"
    r"First\s+Quarter|Second\s+Quarter|Third\s+Quarter|Fourth\s+Quarter|"
    r"Fall|Spring|Summer|Winter|Year|Q[1-4]|S[12])\b",
    re.I,
)

_GRADEBOOK_TERM_ALIASES = {
    "first semester": "1st semester",
    "second semester": "2nd semester",
    "semester 1": "1st semester",
    "semester 2": "2nd semester",
    "s1": "1st semester",
    "s2": "2nd semester",
    "first quarter": "q1",
    "1st quarter": "q1",
    "second quarter": "q2",
    "2nd quarter": "q2",
    "third quarter": "q3",
    "3rd quarter": "q3",
    "fourth quarter": "q4",
    "4th quarter": "q4",
    "trimester 1": "t1",
    "trimester 2": "t2",
    "trimester 3": "t3",
}


def normalize_gradebook_term(term):
    t = re.sub(r"\s+", " ", (term or "").strip().lower())
    return _GRADEBOOK_TERM_ALIASES.get(t, t)


def _md_tuple(token, default):
    raw = token or default
    try:
        month, day = str(raw).split("-")
        return int(month), int(day)
    except (ValueError, AttributeError):
        month, day = default.split("-")
        return int(month), int(day)


def preferred_gradebook_terms(today=None, calendar=None):
    """Ordered term keys for the current TUSD 6-12 season (Fall first half, Spring second)."""
    today = today or pacific_today()
    if isinstance(today, datetime):
        today = today.date()
    if calendar is None:
        try:
            calendar = load_school_calendar(refresh=False)
        except Exception:
            calendar = {}
    cut = {**_DEFAULT_TERM_CUTOVERS, **((calendar or {}).get("term_cutovers") or {})}
    q1_m, q1_d = _md_tuple(cut.get("q1_last_md"), "10-12")
    fall_m, fall_d = _md_tuple(cut.get("fall_last_md"), "01-04")
    q3_m, q3_d = _md_tuple(cut.get("q3_last_md"), "03-15")
    month, day = today.month, today.day
    if month >= 7:
        terms = ["fall", "1st semester", "year"]
        if month < q1_m or (month == q1_m and day <= q1_d):
            terms.extend(["q1", "t1"])
        else:
            terms.append("q2")
        return terms
    if month < fall_m or (month == fall_m and day <= fall_d):
        return ["fall", "1st semester", "q2", "year"]
    terms = ["spring", "2nd semester", "year"]
    if month < q3_m or (month == q3_m and day <= q3_d):
        terms.extend(["q3", "t2"])
    else:
        terms.extend(["q4", "t3"])
    return terms


def parse_gradebook_option_label(label):
    """Parse '2- PE 8- Spring  1/6/2026...' / '5- Alg- Fall' dropdown labels."""
    raw = (label or "").strip()
    text = re.sub(r"[<>]+", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    period = extract_period_from_label(text) or extract_period_from_label(raw)

    dates = []
    for m in re.finditer(r"(\d{1,2}/\d{1,2}/\d{2,4})", text):
        token = m.group(1)
        parsed_date = None
        for fmt in ("%m/%d/%Y", "%m/%d/%y"):
            try:
                parsed_date = datetime.strptime(token, fmt).date()
                break
            except ValueError:
                continue
        if parsed_date:
            dates.append(parsed_date)

    term_m = _GRADEBOOK_TERM_PATTERN.search(text)
    term = term_m.group(1) if term_m else None
    name = text
    if term:
        name_m = re.match(
            rf"\d+\s*-\s*(.+?)\s*-\s*{re.escape(term)}\b",
            text,
            re.I,
        )
        if name_m:
            name = name_m.group(1).strip()
        else:
            name = _GRADEBOOK_TERM_PATTERN.split(text, maxsplit=1)[0]
            name = re.sub(r"^\d+\s*-\s*", "", name).strip(" -")

    return {
        "label": raw,
        "name": name or raw,
        "period": period,
        "term": term,
        "term_key": normalize_gradebook_term(term) if term else None,
        "start_date": dates[0] if dates else None,
        "end_date": dates[1] if len(dates) > 1 else None,
    }


def extract_class_name(label):
    """Extract clean class name from a GradebookDetails dropdown label."""
    parsed = parse_gradebook_option_label(label)
    return parsed.get("name") or label


def _assignment_group_entry(class_label, assignments, totals=None):
    parsed = parse_gradebook_option_label(class_label)
    entry = {
        "class_name": parsed.get("name") or extract_class_name(class_label),
        "period": parsed.get("period"),
        "term": parsed.get("term_key"),
        "assignments": assignments,
    }
    if totals:
        entry["totals"] = totals
    return entry


def option_in_school_session(parsed, session_year):
    """True/False if the option's start date is in session; None if unknown."""
    start = parsed.get("start_date") if parsed else None
    if not start or not session_year:
        return None
    first = _parse_iso_date_str(session_year.get("first_day"))
    last = _parse_iso_date_str(session_year.get("last_day"))
    if not first or not last:
        return None
    return first <= start <= last


def select_current_gradebook_options(option_tags, today=None, calendar=None):
    """Pick current-term GradebookDetails options (Fall in Aug–Dec, Spring in Jan–Jun).

    Last year's Spring entries stay in the Aeries dropdown after year start; do not
    scrape them into the new term. Year-long courses are included alongside the
    current season term.
    """
    today = today or pacific_today()
    calendar = calendar or load_school_calendar(refresh=False)
    session = school_session_window(today=today, calendar=calendar)
    preferred = preferred_gradebook_terms(today=today, calendar=calendar)

    parsed_opts = []
    selected_value = None
    for opt in option_tags:
        value = opt.get("value")
        label = opt.get_text(" ", strip=True)
        if value is None or not str(label).strip():
            continue
        parsed = parse_gradebook_option_label(label)
        parsed["value"] = value
        parsed["selected"] = opt.has_attr("selected")
        if parsed["selected"]:
            selected_value = value
        parsed_opts.append(parsed)

    empty_meta = {
        "reason": "no_options",
        "terms": [],
        "selected_value": selected_value,
        "preload_ok": False,
        "option_count": len(parsed_opts),
        "chosen_count": 0,
    }
    if not parsed_opts:
        return [], empty_meta

    in_session = []
    undated = []
    for parsed in parsed_opts:
        flag = option_in_school_session(parsed, session)
        if flag is False:
            continue
        if flag is True:
            in_session.append(parsed)
        else:
            undated.append(parsed)

    pool = in_session or undated
    if not pool:
        terms = []
        seen = set()
        for parsed in parsed_opts:
            key = parsed.get("term_key")
            if key and key not in seen:
                seen.add(key)
                terms.append(key)
        empty_meta.update({"reason": "no_current_term", "terms": terms})
        return [], empty_meta

    terms_found = []
    seen_terms = set()
    for parsed in pool:
        key = parsed.get("term_key")
        if key and key not in seen_terms:
            seen_terms.add(key)
            terms_found.append(key)

    chosen_term = next((term for term in preferred if term in seen_terms), None)
    if chosen_term:
        keep = {chosen_term, "year"} if chosen_term != "year" else {"year"}
        ordered = [parsed for parsed in pool if parsed.get("term_key") in keep]
        reason = f"term:{chosen_term}+year" if "year" in seen_terms and chosen_term != "year" else f"term:{chosen_term}"
    else:
        ordered = [parsed for parsed in pool if parsed.get("selected")] or list(pool)
        reason = "selected" if len(ordered) == 1 and ordered[0].get("selected") else "all_current"

    pairs = [(parsed["value"], parsed["label"]) for parsed in ordered]
    preload_ok = bool(
        selected_value and pairs and pairs[0][0] == selected_value
    )
    return pairs, {
        "reason": reason,
        "terms": terms_found,
        "selected_value": selected_value,
        "preload_ok": preload_ok,
        "option_count": len(parsed_opts),
        "chosen_count": len(pairs),
    }


def fetch_all_assignments(session):
    """Fetch assignments for current-term classes via GradebookDetails postback."""
    resp = session.get(f"{BASE_URL}/student/GradebookDetails.aspx")
    if resp.status_code != 200:
        print(f"  GradebookDetails status {resp.status_code}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # Get form state
    all_inputs = {}
    for inp in soup.find_all("input", {"type": "hidden"}):
        name = inp.get("name", "")
        if name:
            all_inputs[name] = inp.get("value", "")

    class_select = soup.find("select", {"id": re.compile("dlGN")})
    if not class_select:
        print("  GradebookDetails: no class dropdown (dlGN)")
        return []

    chosen, meta = select_current_gradebook_options(class_select.find_all("option"))
    print(
        f"  Gradebook dropdown: {meta.get('option_count', 0)} option(s), "
        f"terms={meta.get('terms')}, using {meta.get('reason')} "
        f"({meta.get('chosen_count', 0)} class(es))"
    )
    if not chosen:
        return []

    all_class_assignments = []
    first_assignments = parse_assignment_rows(soup)
    first_totals = parse_gradebook_totals(soup)
    start_index = 0
    if meta.get("preload_ok"):
        all_class_assignments.append(
            _assignment_group_entry(chosen[0][1], first_assignments, first_totals)
        )
        start_index = 1

    for class_value, class_label in chosen[start_index:]:

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
            totals = parse_gradebook_totals(frag_soup)
            all_class_assignments.append(
                _assignment_group_entry(class_label, assignments, totals)
            )

            vs_match = re.search(r"__VIEWSTATE\|([^|]+)\|", resp2.text)
            if vs_match:
                all_inputs["__VIEWSTATE"] = vs_match.group(1)
            vsg_match = re.search(r"__VIEWSTATEGENERATOR\|([^|]+)\|", resp2.text)
            if vsg_match:
                all_inputs["__VIEWSTATEGENERATOR"] = vsg_match.group(1)

    for entry in all_class_assignments:
        finalize_gradebook_group(entry)
    return all_class_assignments


def parse_assignment_rows(soup):
    """Parse assignment-info rows from a BeautifulSoup object.

    Column layout (17 cells per row):
    [0]  # (with "Date Assigned: MM/DD/YYYY" embedded)
    [1]  Description
    [2]  Category
    [3]  Score fraction display (e.g. "5 / 5") or status code (NA / TX)
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

    Keep the raw Score cell text. Never coerce NA/TX to blank via safe_float.
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

        # Score — raw text first so NA/TX stay visible
        score_display = cells[3].get_text(separator=" ", strip=True) if len(cells) > 3 else ""
        earned_raw = cells[4].get_text(strip=True) if len(cells) > 4 else ""
        possible_raw = cells[6].get_text(strip=True) if len(cells) > 6 else ""
        # Score cell only. Do not fall back to Max alone (pending rows would look scored).
        if score_display:
            score_raw = score_display
        elif earned_raw:
            score_raw = f"{earned_raw} / {possible_raw}".strip(" /") if possible_raw else earned_raw
        else:
            score_raw = ""
        points_earned = safe_float(earned_raw)
        points_possible = safe_float(possible_raw)

        # # Correct
        correct_display = cells[7].get_text(separator=" ", strip=True) if len(cells) > 7 else ""
        correct_earned_raw = cells[8].get_text(strip=True) if len(cells) > 8 else ""
        correct_possible_raw = cells[10].get_text(strip=True) if len(cells) > 10 else ""
        correct_raw = correct_display or ""
        correct_earned = safe_float(correct_earned_raw)
        correct_possible = safe_float(correct_possible_raw)

        # Percentage
        pct_text = cells[11].get_text(strip=True) if len(cells) > 11 else ""
        pct_match = re.search(r"([\d.]+)%", pct_text)
        percentage = float(pct_match.group(1)) if pct_match else None

        comment = cells[12].get_text(separator=" ", strip=True) if len(cells) > 12 else ""
        date_completed = cells[13].get_text(strip=True) if len(cells) > 13 else ""
        due_date = cells[14].get_text(strip=True) if len(cells) > 14 else ""
        grading_complete = (
            cells[15].get_text(strip=True) == "Yes" if len(cells) > 15 else False
        )
        documents = cells[16].get_text(separator=" ", strip=True) if len(cells) > 16 else ""

        extra_credit = (
            points_possible == 0
            and points_earned is not None
        )
        row_classes = " ".join(row.get("class") or [])
        aeries_missing = bool(
            _MISSING_MARK_RE.search(score_display or "")
            or re.search(r"MissingAssignment", row_classes)
        )

        assignment = {
            "number": assign_num,
            "description": cells[1].get_text(strip=True),
            "category": cells[2].get_text(strip=True),
            "score_raw": score_raw,
            "points_earned": points_earned,
            "points_possible": points_possible,
            "percentage": percentage,
            "correct_raw": correct_raw,
            "correct_earned": correct_earned,
            "correct_possible": correct_possible,
            "comment": comment,
            "documents": documents,
            "date_assigned": date_assigned,
            "date_completed": date_completed,
            "due_date": due_date,
            "grading_complete": grading_complete,
            "extra_credit": extra_credit,
            "aeries_missing": aeries_missing,
        }
        assignments.append(assignment)

    return assignments


SCORE_STATUS_CODES = ("NA", "TX", "EX", "NT")
_SCORE_STATUS_RE = re.compile(
    r"\b(" + "|".join(SCORE_STATUS_CODES) + r")\b",
    re.IGNORECASE,
)
# Aeries may render a per-assignment missing marker in the Score cell ("MI" / "Missing").
_MISSING_MARK_RE = re.compile(r"\b(MI|Missing)\b", re.IGNORECASE)
# Past-due, unscored, not flagged missing for this long is worth a question to the teacher.
STALE_AWAITING_DAYS = 10
_TOTAL_ROW_RE = re.compile(r"^(total|overall|grand total)\b", re.IGNORECASE)
_MIN_MAX_NOTE_RE = re.compile(
    r"min(?:imum)?(?:\s+assignment)?(?:\s+(?:value|score|pct|percent(?:age)?))?"
    r"(?:\s+(?:of|is|:))?\s*(\d+(?:\.\d+)?)\s*%"
    r".{0,120}"
    r"max(?:imum)?(?:\s+assignment)?(?:\s+(?:value|score|pct|percent(?:age)?))?"
    r"(?:\s+(?:of|is|:))?\s*(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE | re.DOTALL,
)
_MAX_MIN_NOTE_RE = re.compile(
    r"max(?:imum)?(?:\s+assignment)?(?:\s+(?:value|score|pct|percent(?:age)?))?"
    r"(?:\s+(?:of|is|:))?\s*(\d+(?:\.\d+)?)\s*%"
    r".{0,120}"
    r"min(?:imum)?(?:\s+assignment)?(?:\s+(?:value|score|pct|percent(?:age)?))?"
    r"(?:\s+(?:of|is|:))?\s*(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE | re.DOTALL,
)


def safe_float(val):
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def score_status_code(*texts):
    """Return NA/TX/EX/NT if present in any Score-cell text; do not invent blanks."""
    for text in texts:
        if not text:
            continue
        match = _SCORE_STATUS_RE.search(str(text))
        if match:
            return match.group(1).upper()
    return None


def assignment_has_score_mark(assignment):
    """True when Aeries recorded a numeric score or a visible status code (NA/TX)."""
    if not assignment:
        return False
    if assignment.get("points_earned") is not None:
        return True
    return bool(score_status_code(assignment.get("score_raw")))


def _cell_text(cell):
    return cell.get_text(separator=" ", strip=True) if cell is not None else ""


def _norm_header(text):
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _parse_pct_value(text):
    if text is None:
        return None
    match = re.search(r"(-?[\d.]+)\s*%", str(text))
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return safe_float(str(text).strip().rstrip("%"))


def _weight_in_header(text):
    match = re.search(r"\((\s*-?[\d.]+)\s*%\s*\)", text or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _header_cells(table):
    if table is None:
        return []
    thead = table.find("thead")
    if thead:
        row = thead.find("tr")
        if row:
            cells = row.find_all(["th", "td"])
            if cells:
                return [_cell_text(c) for c in cells]
    first = table.find("tr")
    if not first:
        return []
    cells = first.find_all("th")
    if not cells:
        # Some Aeries footers use a header row of td cells
        maybe = first.find_all("td")
        if maybe and any(
            re.search(r"category|perc of grade|summative|formative", _norm_header(_cell_text(c)))
            for c in maybe
        ):
            cells = maybe
    return [_cell_text(c) for c in cells]


def _classify_totals_headers(headers):
    """Return layout name and column index map, or (None, {})."""
    norms = [_norm_header(h) for h in headers]
    joined = " | ".join(norms)
    if re.search(r"description|due date|grading complete|date assigned", joined):
        return None, {}
    if not any("category" in h for h in norms):
        return None, {}

    idx = {}
    for i, h in enumerate(norms):
        if "category" in h and "idx_category" not in idx:
            idx["category"] = i
        elif "perc of grade" in h or "percent of grade" in h or h in ("weight", "weight %"):
            idx["weight"] = i
        elif "summative" in h and "pts" in h:
            idx["summative_pts"] = i
        elif "summative" in h and "max" in h:
            idx["summative_max"] = i
        elif "summative" in h and ("perc" in h or "percent" in h or "%" in h):
            idx["summative_perc"] = i
        elif "formative" in h and "pts" in h:
            idx["formative_pts"] = i
        elif "formative" in h and "max" in h:
            idx["formative_max"] = i
        elif "formative" in h and ("perc" in h or "percent" in h or "%" in h):
            idx["formative_perc"] = i
        elif "overall" in h and ("perc" in h or "percent" in h or "%" in h):
            idx["overall_perc"] = i
        elif h in ("points", "pts") or (h.endswith("pts") and "summative" not in h and "formative" not in h):
            idx["points"] = i
        elif h == "max" or h == "max pts" or h == "points possible":
            idx["max"] = i
        elif h in ("perc", "percent", "%", "percentage") or (
            ("perc" in h or "percent" in h) and "grade" not in h and "overall" not in h
            and "summative" not in h and "formative" not in h
        ):
            idx["perc"] = i
        elif h == "mark" or h == "letter":
            idx["mark"] = i

    if "summative_perc" in idx and "formative_perc" in idx:
        return "summative_formative", idx
    if "weight" in idx:
        return "perc_of_grade", idx
    return None, {}


def parse_min_max_assignment_scale(soup):
    """Store min/max assignment scale only when a footer note says it is in effect."""
    if soup is None:
        return None, None
    text = soup.get_text(" ", strip=True) if hasattr(soup, "get_text") else str(soup)
    if not re.search(r"in effect|assignment (?:value|score|scale)|scale min", text, re.I):
        # Require wording that the scale is actually on, not a random 50%/100% elsewhere
        if not re.search(r"min(?:imum)?(?:\s+assignment).{0,40}\d+\s*%", text, re.I):
            return None, None
    match = _MIN_MAX_NOTE_RE.search(text)
    if match:
        return safe_float(match.group(1)), safe_float(match.group(2))
    match = _MAX_MIN_NOTE_RE.search(text)
    if match:
        return safe_float(match.group(2)), safe_float(match.group(1))
    return None, None


def parse_gradebook_totals(soup):
    """Parse the GradebookDetails Totals footer. Soft-fail (None) if absent."""
    if soup is None:
        return None
    try:
        tables = list(soup.find_all("table"))
        # Prefer a table whose id/class mentions totals, but accept header match anywhere
        ranked = []
        for table in tables:
            ident = " ".join(
                filter(
                    None,
                    [
                        table.get("id") or "",
                        " ".join(table.get("class") or []),
                    ],
                )
            )
            headers = _header_cells(table)
            layout, idx = _classify_totals_headers(headers)
            if not layout:
                continue
            score = 2 if re.search(r"total", ident, re.I) else 1
            ranked.append((score, table, headers, layout, idx))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0], reverse=True)
        _, table, headers, layout, idx = ranked[0]
        min_pct, max_pct = parse_min_max_assignment_scale(soup)

        body_rows = []
        thead = table.find("thead")
        for row in table.find_all("tr"):
            if thead and row.parent is thead:
                continue
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            texts = [_cell_text(c) for c in cells]
            # Skip a repeated header row
            if texts and _norm_header(texts[0]) == "category" and any(
                "perc" in _norm_header(t) or "pts" in _norm_header(t) for t in texts[1:]
            ):
                continue
            body_rows.append(texts)

        def col(texts, key):
            i = idx.get(key)
            if i is None or i >= len(texts):
                return ""
            return texts[i]

        categories = []
        overall_perc = None
        overall_mark = ""
        summative_weight = _weight_in_header(headers[idx["summative_perc"]]) if "summative_perc" in idx else None
        formative_weight = _weight_in_header(headers[idx["formative_perc"]]) if "formative_perc" in idx else None

        for texts in body_rows:
            name = col(texts, "category") or (texts[0] if texts else "")
            if not name:
                continue
            is_total = bool(_TOTAL_ROW_RE.match(name))

            if layout == "perc_of_grade":
                weight = _parse_pct_value(col(texts, "weight"))
                points = safe_float(col(texts, "points"))
                maximum = safe_float(col(texts, "max"))
                perc = _parse_pct_value(col(texts, "perc"))
                mark = col(texts, "mark")
                empty = (points in (0, None) and maximum in (0, None))
                if is_total:
                    overall_perc = perc if perc is not None else overall_perc
                    overall_mark = mark or overall_mark
                    continue
                categories.append({
                    "name": name,
                    "weight_pct": weight,
                    "points": points,
                    "max": maximum,
                    "perc": perc,
                    "mark": mark,
                    "empty": empty,
                })
            else:
                s_pts = safe_float(col(texts, "summative_pts"))
                s_max = safe_float(col(texts, "summative_max"))
                s_perc = _parse_pct_value(col(texts, "summative_perc"))
                f_pts = safe_float(col(texts, "formative_pts"))
                f_max = safe_float(col(texts, "formative_max"))
                f_perc = _parse_pct_value(col(texts, "formative_perc"))
                o_perc = _parse_pct_value(col(texts, "overall_perc"))
                mark = col(texts, "mark")
                if is_total:
                    overall_perc = o_perc if o_perc is not None else overall_perc
                    overall_mark = mark or overall_mark
                    if s_perc is not None:
                        # Total row type perc is the bucket score, not the weight
                        pass
                    # Represent the two weighted buckets from the Total row when present
                    continue
                # A category row lives on one side or both
                s_empty = s_pts in (0, None) and s_max in (0, None)
                f_empty = f_pts in (0, None) and f_max in (0, None)
                if not s_empty or s_perc is not None:
                    categories.append({
                        "name": name,
                        "kind": "summative",
                        "weight_pct": summative_weight,
                        "points": s_pts,
                        "max": s_max,
                        "perc": s_perc,
                        "mark": mark,
                        "empty": s_empty,
                    })
                if not f_empty or f_perc is not None:
                    categories.append({
                        "name": name,
                        "kind": "formative",
                        "weight_pct": formative_weight,
                        "points": f_pts,
                        "max": f_max,
                        "perc": f_perc,
                        "mark": mark,
                        "empty": f_empty,
                    })
                if s_empty and f_empty and s_perc is None and f_perc is None:
                    # Still record the named category so weights can attach later
                    kind = None
                    lname = name.lower()
                    if "summative" in lname:
                        kind = "summative"
                    elif "formative" in lname:
                        kind = "formative"
                    categories.append({
                        "name": name,
                        "kind": kind,
                        "weight_pct": (
                            summative_weight if kind == "summative"
                            else formative_weight if kind == "formative"
                            else None
                        ),
                        "points": 0.0,
                        "max": 0.0,
                        "perc": None,
                        "mark": mark,
                        "empty": True,
                    })

        if layout == "summative_formative":
            # If no per-category rows landed, synthesize buckets from weights alone
            if not categories:
                if summative_weight is not None:
                    categories.append({
                        "name": "Summative",
                        "kind": "summative",
                        "weight_pct": summative_weight,
                        "points": None,
                        "max": None,
                        "perc": None,
                        "mark": "",
                        "empty": True,
                    })
                if formative_weight is not None:
                    categories.append({
                        "name": "Formative",
                        "kind": "formative",
                        "weight_pct": formative_weight,
                        "points": None,
                        "max": None,
                        "perc": None,
                        "mark": "",
                        "empty": True,
                    })

        totals = {
            "layout": layout,
            "categories": categories,
            "overall_perc": overall_perc,
            "overall_mark": overall_mark,
            "min_assignment_pct": min_pct,
            "max_assignment_pct": max_pct,
            "min_max_in_effect": min_pct is not None or max_pct is not None,
            "parse_ok": True,
        }
        if layout == "summative_formative":
            totals["summative_weight_pct"] = summative_weight
            totals["formative_weight_pct"] = formative_weight
        return totals
    except Exception:
        return None


def category_weight_map(totals):
    """Map normalized category name → weight_pct from the Totals footer."""
    weights = {}
    if not totals:
        return weights
    for cat in totals.get("categories") or []:
        name = (cat.get("name") or "").strip()
        if not name:
            continue
        weight = cat.get("weight_pct")
        if weight is None:
            continue
        weights[_norm_course_name(name)] = weight
        weights[name.lower()] = weight
    # Layout A: also map the type words
    if totals.get("layout") == "summative_formative":
        if totals.get("summative_weight_pct") is not None:
            weights["summative"] = totals["summative_weight_pct"]
            weights["summatives"] = totals["summative_weight_pct"]
        if totals.get("formative_weight_pct") is not None:
            weights["formative"] = totals["formative_weight_pct"]
            weights["formatives"] = totals["formative_weight_pct"]
    return weights


def lookup_category_weight(category, weights):
    if not category or not weights:
        return None
    key = _norm_course_name(category)
    if key in weights:
        return weights[key]
    low = category.strip().lower()
    if low in weights:
        return weights[low]
    # Prefix / contains match for "Summatives" vs "Summative"
    for stored, weight in weights.items():
        if not stored:
            continue
        if stored in key or key in stored:
            return weight
    return None


def annotate_assignment_status(assignment, weights=None):
    """Set status / status_label from Score raw text + footer weights."""
    weights = weights or {}
    raw = (assignment.get("score_raw") or "").strip()
    code = score_status_code(raw)
    cat = (assignment.get("category") or "").strip()
    weight = lookup_category_weight(cat, weights)
    extra = bool(
        assignment.get("extra_credit")
        or (
            assignment.get("points_possible") == 0
            and assignment.get("points_earned") is not None
        )
    )
    assignment["extra_credit"] = extra

    if code:
        assignment["status"] = code.lower()
        assignment["status_label"] = code
        assignment["counts_toward_grade"] = False
        return assignment

    if assignment.get("points_earned") is None:
        if assignment.get("aeries_missing"):
            assignment["status"] = "missing"
            assignment["status_label"] = "marked missing in Aeries"
            assignment["counts_toward_grade"] = False
            return assignment
        # "Grading Completed" is a teacher bookkeeping flag; a blank is still unscored.
        if assignment_turned_in(assignment):
            assignment["status"] = "turned_in"
            assignment["status_label"] = "turned in · awaiting score"
        else:
            assignment["status"] = "pending"
            assignment["status_label"] = "pending"
        assignment["counts_toward_grade"] = False
        return assignment

    if weight == 0:
        assignment["status"] = "zero_weight"
        assignment["status_label"] = "0% category (doesn't count)"
        assignment["counts_toward_grade"] = False
        return assignment

    if extra:
        assignment["status"] = "extra_credit"
        assignment["status_label"] = f"extra credit in {cat}" if cat else "extra credit"
        assignment["counts_toward_grade"] = True
        return assignment

    if cat:
        assignment["status"] = "counts"
        assignment["status_label"] = f"counts in {cat}"
    else:
        assignment["status"] = "counts"
        assignment["status_label"] = "counts"
    assignment["counts_toward_grade"] = True
    return assignment


def _clamp_pct(pct, min_pct, max_pct):
    if pct is None:
        return None
    if min_pct is not None:
        pct = max(pct, min_pct)
    if max_pct is not None:
        pct = min(pct, max_pct)
    return pct


def _category_perc_from_assignments(assignments, category_name, min_pct, max_pct):
    """Points-weighted category % from assignments; extra credit (max=0) adds to numerator."""
    earned = 0.0
    possible = 0.0
    extra = 0.0
    have = False
    want = _norm_course_name(category_name)
    for a in assignments or []:
        name = _norm_course_name(a.get("category"))
        if want and name and not (want in name or name in want):
            continue
        if score_status_code(a.get("score_raw")):
            continue
        pts = a.get("points_earned")
        mx = a.get("points_possible")
        if pts is None:
            continue
        have = True
        if mx == 0:
            extra += float(pts)
            continue
        if not mx:
            continue
        pct = a.get("percentage")
        if pct is None:
            pct = (float(pts) / float(mx)) * 100.0
        pct = _clamp_pct(pct, min_pct, max_pct)
        if pct is None:
            continue
        earned += (pct / 100.0) * float(mx)
        possible += float(mx)
    if not have or possible <= 0:
        return None
    return ((earned + extra) / possible) * 100.0


def rebuild_counted_percent(totals, assignments=None):
    """Rebuild class % from counted footer categories only. Empty 0/0 buckets drop out.

    Example: 70% Assessments 0/0 + 20% of 100 + 10% of 85 → 95%.
    Does not overwrite the official Aeries posted percent.
    """
    if not totals or not totals.get("categories"):
        return None
    min_pct = totals.get("min_assignment_pct")
    max_pct = totals.get("max_assignment_pct")
    layout = totals.get("layout")

    if layout == "summative_formative":
        buckets = {"summative": None, "formative": None}
        weights = {
            "summative": totals.get("summative_weight_pct"),
            "formative": totals.get("formative_weight_pct"),
        }
        for cat in totals["categories"]:
            kind = cat.get("kind")
            if kind not in buckets:
                continue
            if weights.get(kind) in (None, 0):
                continue
            if cat.get("empty") or (cat.get("points") in (0, None) and cat.get("max") in (0, None)):
                continue
            perc = cat.get("perc")
            if perc is None:
                perc = _category_perc_from_assignments(
                    assignments, cat.get("name"), min_pct, max_pct
                )
            if perc is None:
                continue
            # If several categories share a kind, points-weight them
            prev = buckets[kind]
            if prev is None:
                buckets[kind] = {
                    "perc": perc,
                    "points": cat.get("points") or 0,
                    "max": cat.get("max") or 0,
                }
            else:
                mx = (prev["max"] or 0) + (cat.get("max") or 0)
                pts = (prev["points"] or 0) + (cat.get("points") or 0)
                if mx > 0:
                    buckets[kind] = {"perc": (pts / mx) * 100.0, "points": pts, "max": mx}
                else:
                    buckets[kind] = {"perc": perc, "points": pts, "max": mx}
        counted = []
        for kind, data in buckets.items():
            weight = weights.get(kind)
            if weight in (None, 0) or not data:
                continue
            counted.append((weight, data["perc"]))
    else:
        counted = []
        for cat in totals["categories"]:
            name = cat.get("name") or ""
            if _TOTAL_ROW_RE.match(name):
                continue
            weight = cat.get("weight_pct")
            if weight is None or weight <= 0:
                continue
            pts = cat.get("points")
            mx = cat.get("max")
            perc = cat.get("perc")
            extra = mx == 0 and pts is not None and pts > 0
            if extra:
                # Extra-credit category (max 0) is a bonus, not a weighted slot
                continue
            if cat.get("empty") or (pts in (0, None) and mx in (0, None)):
                continue
            if perc is None:
                perc = _category_perc_from_assignments(assignments, name, min_pct, max_pct)
            if perc is None and pts is not None and mx:
                perc = (pts / mx) * 100.0
            if perc is None:
                continue
            counted.append((weight, perc))

    if not counted:
        return None
    total_w = sum(w for w, _ in counted)
    if total_w <= 0:
        return None
    return round(sum(w * p for w, p in counted) / total_w, 2)


def build_counted_insight(totals, assignments=None, posted_pct=None, posted_mark=None):
    """Family-facing counted-only mix from the footer. Never replaces posted %."""
    if not totals or not totals.get("parse_ok"):
        return None
    rebuild = rebuild_counted_percent(totals, assignments)
    cats = []
    extra_credit = False
    for cat in totals.get("categories") or []:
        name = cat.get("name") or ""
        if _TOTAL_ROW_RE.match(name):
            continue
        weight = cat.get("weight_pct")
        pts = cat.get("points")
        mx = cat.get("max")
        empty = bool(cat.get("empty") or (pts in (0, None) and mx in (0, None)))
        extra = mx == 0 and pts is not None and pts > 0
        if extra:
            extra_credit = True
        counts = bool(
            weight not in (None, 0)
            and not empty
            and not extra
        )
        reason = None
        if extra:
            reason = "extra_credit"
        elif weight == 0:
            reason = "zero_weight"
        elif empty:
            reason = "empty_0_0"
        cats.append({
            "name": name,
            "kind": cat.get("kind"),
            "weight_pct": weight,
            "perc": cat.get("perc"),
            "points": pts,
            "max": mx,
            "empty": empty,
            "counts": counts,
            "reason": reason,
        })
        if any(a.get("extra_credit") for a in (assignments or [])):
            extra_credit = True

    matches = None
    if posted_pct is not None and rebuild is not None:
        try:
            matches = abs(float(posted_pct) - float(rebuild)) < 0.51
        except (TypeError, ValueError):
            matches = None

    return {
        "layout": totals.get("layout"),
        "categories": cats,
        "rebuild_pct": rebuild,
        "posted_pct": posted_pct,
        "posted_mark": posted_mark or "",
        "matches_posted": matches,
        "min_assignment_pct": totals.get("min_assignment_pct"),
        "max_assignment_pct": totals.get("max_assignment_pct"),
        "extra_credit": extra_credit,
    }


def finalize_gradebook_group(entry):
    """Annotate assignment statuses and counted insight on one GradebookDetails group."""
    if not entry:
        return entry
    totals = entry.get("totals")
    weights = category_weight_map(totals)
    for assignment in entry.get("assignments") or []:
        annotate_assignment_status(assignment, weights)
    insight = build_counted_insight(totals, entry.get("assignments"))
    if insight:
        entry["counted_insight"] = insight
    return entry


def assignment_group_for_class(class_assignments, class_meta):
    """Return the GradebookDetails group matching a ClassSummary row."""
    if not class_assignments or not class_meta:
        return None
    period = class_meta.get("period")
    course_n = _norm_course_name(class_meta.get("course_name"))
    fallback = None
    for group in class_assignments:
        label = group.get("class_name") or ""
        group_period = group.get("period")
        if group_period is None:
            group_period = extract_period_from_label(label)
        name_n = _norm_course_name(label)
        if period is not None and group_period is not None and int(group_period) == int(period):
            return group
        if course_n and name_n and (course_n in name_n or name_n in course_n):
            fallback = group
    return fallback


def attach_gradebook_insights(classes, class_assignments):
    """Copy footer + counted insight onto class rows. Never overwrite percent/mark."""
    for group in class_assignments or []:
        finalize_gradebook_group(group)
    for class_meta in classes or []:
        group = assignment_group_for_class(class_assignments, class_meta)
        if not group:
            continue
        if group.get("totals"):
            class_meta["gradebook_totals"] = group["totals"]
        # posted_pct is always the ClassSummary number — never a homemade rebuild
        insight = build_counted_insight(
            group.get("totals"),
            group.get("assignments"),
            posted_pct=safe_float(class_meta.get("percent")),
            posted_mark=class_meta.get("mark") or "",
        )
        if insight:
            group["counted_insight"] = insight
            class_meta["counted_insight"] = insight
        # Official class number is ClassSummary Percent / CurrentMark only
        class_meta.pop("rebuild_pct", None)
    return classes


def _class_row_richness(cls):
    """Score how much grade signal a class row carries (for Aeries duplicate merge)."""
    score = 0
    pct = str(cls.get("percent") or "").strip()
    mark = str(cls.get("mark") or "").strip()
    if pct:
        score += 2
        try:
            if float(pct) > 0:
                score += 2
        except (TypeError, ValueError):
            pass
    if mark:
        score += 3
    if cls.get("missing_count"):
        score += 1
    if str(cls.get("missing_assignments") or "").strip():
        score += 1
    if str(cls.get("trend") or "").strip():
        score += 1
    return score


def dedupe_class_summary_rows(classes):
    """Merge duplicate ClassSummary rows (same period + course), keep richest grade data.

    Aeries sometimes returns two widgets for one section at year start — one empty
    schedule row and one with a phantom 0% gradebook.
    """
    if not classes:
        return []
    best = {}
    order = []
    for cls in classes:
        key = (
            cls.get("period"),
            re.sub(r"\s+", " ", (cls.get("course_name") or "").strip().lower()),
        )
        if key not in best:
            best[key] = cls
            order.append(key)
            continue
        prev = best[key]
        if _class_row_richness(cls) > _class_row_richness(prev):
            best[key] = cls
    return [best[k] for k in order]


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
    return dedupe_class_summary_rows(classes)


def class_has_real_grade(class_meta, assignments=None):
    """True when the portal has a meaningful posted grade (not schedule-only / phantom 0%).

    Aeries often posts 0% with an empty CurrentMark before a class grade exists.
    Scored assignments do not make that placeholder a real class grade. A letter
    mark, a percent above 0, or a 0% with Aeries-flagged missing work does.
    `assignments` is accepted so call sites can pass the same evidence the UI has.
    """
    mark = str(class_meta.get("mark") or "").strip()
    if mark:
        return True
    pct_raw = class_meta.get("percent")
    if pct_raw in (None, ""):
        return False
    try:
        pct = float(pct_raw)
    except (TypeError, ValueError):
        return False
    if pct > 0:
        return True
    # 0% + empty mark is a placeholder unless Aeries also flags missing work
    if class_meta.get("missing_count"):
        return True
    return False


def is_phantom_zero_grade(class_meta, assignments=None):
    """Posted 0% + empty mark that the tile hides as no grade yet."""
    mark = str(class_meta.get("mark") or "").strip()
    if mark:
        return False
    pct_raw = class_meta.get("percent")
    if pct_raw in (None, ""):
        return False
    try:
        pct = float(pct_raw)
    except (TypeError, ValueError):
        return False
    if pct != 0:
        return False
    return not class_has_real_grade(class_meta, assignments)


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
    date_str = pacific_today().isoformat()
    classes = {}
    # Include schedule-only rows so new-term days still have a baseline before grades post
    for class_meta in student_data.get("classes") or []:
        name = (class_meta.get("course_name") or "").strip()
        if not name:
            continue
        assignments = assignments_for_class(student_data, class_meta)
        real = class_has_real_grade(class_meta, assignments)
        classes[name] = {
            "period": class_meta.get("period"),
            "pct": safe_float(class_meta.get("percent")) if real else None,
            "mark": (class_meta.get("mark") or "").strip() if real else "",
            "missing_count": class_meta.get("missing_count") or 0,
            "missing_names": [],
            "aeries_trend": None,
        }
    for c in class_analytics_list or []:
        name = (c.get("course_name") or "").strip()
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
    as_of = as_of or pacific_today()
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
    if _is_phantom_history_point(past_cls):
        return None
    return round(current_pct - past_pct, 1)


def _is_phantom_history_point(cls):
    """History row is a no-grade / phantom 0 placeholder, not a first real score."""
    if not cls or cls.get("pct") is None:
        return True
    try:
        pct = float(cls.get("pct"))
    except (TypeError, ValueError):
        return True
    if pct != 0:
        return False
    return not str(cls.get("mark") or "").strip()


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
    today = pacific_today()

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
        # Newly flagged since the most recent prior snapshot (no prior snapshot -> nothing is "new")
        last_seen = set((past_any or {}).get("missing_names") or []) if past_any else None
        newly_missing = sorted(now_missing - last_seen) if last_seen is not None else []

        # Full series for span delta; downsampled series for UI sparklines.
        # Skip phantom 0% placeholders so first posted score is not a +95 jump.
        full_hist = []
        for snap in snapshots:
            cls = (snap.get("classes") or {}).get(course_name)
            if cls is None or cls.get("pct") is None:
                continue
            if _is_phantom_history_point(cls):
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
            "newly_missing": newly_missing[:HISTORY_MISSING_NAMES_CAP],
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


def aeries_missing_count(class_meta):
    """Portal missing badge — not the same as 'ungraded and past due'."""
    try:
        n = int((class_meta or {}).get("missing_count") or 0)
    except (TypeError, ValueError):
        n = 0
    html = str((class_meta or {}).get("missing_assignments") or "")
    if re.search(r'class="MissingAssignment"', html):
        m = re.search(r">(\d+)<", html)
        if m:
            n = max(n, int(m.group(1)))
    return n


def is_past_due_ungraded(assignment, today):
    due = parse_due_date(assignment.get("due_date"))
    if not due or due >= today:
        return False
    if assignment_has_score_mark(assignment):
        return False
    return assignment.get("points_earned") is None


def assignment_turned_in(assignment):
    """Aeries stamps Date Completed when work was handed in (often in class)."""
    return bool(str((assignment or {}).get("date_completed") or "").strip())


def select_missing_assignments(assignments, class_meta, today):
    """Past-due ungraded work is missing only if the Aeries portal flags it.

    Much of the kids' work is done in class and scored days later. A blank score —
    even with the teacher's "Grading Completed" box ticked — is pending_grade until
    the portal's own missing badge says otherwise. Rows Aeries marks missing in the
    gradebook itself (aeries_missing) always count. For the rest of the portal's N,
    pick the most likely rows: not turned in first, then grading-complete, oldest.
    """
    flagged = [a for a in assignments if a.get("aeries_missing") and a.get("points_earned") is None]
    aeries_n = max(aeries_missing_count(class_meta), len(flagged))
    if aeries_n <= 0:
        return []
    flagged_ids = {id(a) for a in flagged}
    past_due = [
        a for a in assignments
        if is_past_due_ungraded(a, today) and id(a) not in flagged_ids
    ]
    past_due.sort(key=lambda a: (
        assignment_turned_in(a),
        not a.get("grading_complete"),
        parse_due_date(a.get("due_date")) or datetime.min,
    ))
    picked = flagged + past_due[:max(0, aeries_n - len(flagged))]
    picked.sort(key=lambda a: parse_due_date(a.get("due_date")) or datetime.min)
    return picked


def is_missing_assignment(assignment, today, class_meta=None):
    """Compatibility wrapper; prefer select_missing_assignments for class context."""
    if class_meta is None:
        return False
    return assignment in select_missing_assignments([assignment], class_meta, today)


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


def assignments_for_period(student_data, period):
    return assignments_for_class(student_data, {"period": period, "course_name": ""})


def build_category_breakdown(assignments, totals=None):
    """Per-category assignment facts for Grok. NOT a class grade.

    Equal-average of assignment % is stored as assignment_avg_pct only.
    Footer weights and the counted rebuild live on counted_insight.
    """
    buckets = {}
    for a in assignments or []:
        if a.get("points_earned") is None or a.get("percentage") is None:
            continue
        if score_status_code(a.get("score_raw")):
            continue
        cat = (a.get("category") or "Other").strip() or "Other"
        buckets.setdefault(cat, []).append(a["percentage"])

    weights = category_weight_map(totals)
    footer_by_name = {}
    for cat in (totals or {}).get("categories") or []:
        footer_by_name[_norm_course_name(cat.get("name"))] = cat

    breakdown = {}
    for cat, pcts in buckets.items():
        footer = footer_by_name.get(_norm_course_name(cat)) or {}
        weight = lookup_category_weight(cat, weights)
        breakdown[cat] = {
            "assignment_avg_pct": round(sum(pcts) / len(pcts), 1),
            "count": len(pcts),
            "weight_pct": weight,
            "footer_perc": footer.get("perc"),
            "counts": weight not in (0,) and not footer.get("empty"),
            "not_a_class_grade": True,
        }
    # Include footer-only categories (empty 0/0, 0% weight) so Grok can see the mix
    for cat in (totals or {}).get("categories") or []:
        name = (cat.get("name") or "").strip()
        if not name or name in breakdown or _TOTAL_ROW_RE.match(name):
            continue
        breakdown[name] = {
            "assignment_avg_pct": None,
            "count": 0,
            "weight_pct": cat.get("weight_pct"),
            "footer_perc": cat.get("perc"),
            "counts": bool(cat.get("weight_pct") not in (None, 0) and not cat.get("empty")),
            "empty": bool(cat.get("empty")),
            "not_a_class_grade": True,
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
    for cat, info in (category_breakdown or {}).items():
        avg = info.get("assignment_avg_pct")
        if avg is None:
            avg = info.get("avg_pct")
        if avg is None:
            continue
        if is_assessment_category(cat):
            assessment_avgs.append(avg)
        else:
            other_avgs.append(avg)

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


def extract_grade_mix_flags(counted_insight, min_high_weight=20):
    """Empty high-weight buckets and 0% categories for briefing WHY facts."""
    empty_high = []
    zero_weight = []
    for cat in (counted_insight or {}).get("categories") or []:
        try:
            weight = float(cat["weight_pct"]) if cat.get("weight_pct") is not None else None
        except (TypeError, ValueError):
            weight = None
        if cat.get("empty") and weight not in (None, 0) and weight >= min_high_weight:
            empty_high.append({
                "name": cat.get("name"),
                "weight_pct": weight,
                "reason": cat.get("reason") or "empty_0_0",
            })
        if cat.get("reason") == "zero_weight" or weight == 0:
            zero_weight.append({
                "name": cat.get("name"),
                "weight_pct": 0 if weight is None else weight,
            })
    return empty_high, zero_weight


def infer_urgency_from_analytics(grade_pct, missing_count, trend, performance_pattern):
    if grade_pct is None:
        if missing_count > 0:
            return "watch", "missing_backlog"
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


def _as_calendar_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _short_month_day(due):
    due_d = _as_calendar_date(due) or due
    return f"{due_d.strftime('%b')} {due_d.day}"


def _weekday_and_date(due):
    due_d = _as_calendar_date(due) or due
    return f"{due_d.strftime('%a')} {_short_month_day(due_d)}"


def _format_score_pair(earned, possible):
    def _fmt(n):
        if isinstance(n, float) and n.is_integer():
            return str(int(n))
        return f"{n:g}"

    return f"{_fmt(earned)}/{_fmt(possible)}"


def assignment_due_state(assignment, today, kind=None, is_missing=False):
    """today / upcoming / overdue / completed / pending — never weekday-only."""
    due = parse_due_date(assignment.get("due_date"))
    if not due:
        return None
    today_d = _as_calendar_date(today)
    due_d = _as_calendar_date(due)
    if today_d is None or due_d is None:
        return None
    days = (due_d - today_d).days
    scored = assignment_has_score_mark(assignment)
    missing = bool(is_missing or kind == "missing")
    if scored and days < 0:
        return "completed"
    if days == 0 and not scored:
        return "today"
    if days > 0:
        return "upcoming"
    if missing:
        return "overdue"
    if days == 0 and scored:
        return "completed"
    return "pending"


def assignment_due_label(assignment, today, kind=None, is_missing=False):
    """Parent-facing due copy. Never weekday-only ('due Tue')."""
    due = parse_due_date(assignment.get("due_date"))
    if not due:
        return None
    state = assignment_due_state(assignment, today, kind=kind, is_missing=is_missing)
    date_part = _short_month_day(due)
    weekday_date = _weekday_and_date(due)
    if state == "today":
        return "due today"
    if state == "upcoming":
        return f"due {weekday_date}"
    if state == "completed":
        earned = assignment.get("points_earned")
        possible = assignment.get("points_possible")
        if earned is not None and possible is not None:
            return _format_score_pair(earned, possible)
        pct = assignment.get("percentage")
        if pct is not None:
            return f"{pct:g}%"
        raw = str(assignment.get("score_raw") or "").strip()
        if raw:
            return raw
        return f"completed {date_part}"
    if state == "overdue":
        return f"overdue {date_part}"
    if state == "pending":
        return f"awaiting score · was due {weekday_date}"
    return None


def format_assignment_entry(assignment, today, kind):
    due = parse_due_date(assignment.get("due_date"))
    is_missing = kind == "missing"
    entry = {
        "name": assignment.get("description", ""),
        "category": assignment.get("category", ""),
        "due_date": assignment.get("due_date", ""),
        "points_possible": assignment.get("points_possible"),
    }
    state = assignment_due_state(assignment, today, kind=kind, is_missing=is_missing)
    label = assignment_due_label(assignment, today, kind=kind, is_missing=is_missing)
    if state:
        entry["due_state"] = state
    if label:
        entry["due_label"] = label
    if due:
        entry["due_weekday"] = due.strftime("%a")
        if kind == "missing":
            entry["days_overdue"] = (today - due).days
        elif kind == "upcoming":
            entry["days_until_due"] = (due - today).days
        elif kind == "pending":
            days_past = (today - due).days
            entry["days_past_due"] = days_past
            if days_past >= STALE_AWAITING_DAYS:
                entry["stale"] = True
    if assignment_turned_in(assignment):
        entry["turned_in"] = True
    if assignment.get("aeries_missing"):
        entry["aeries_missing"] = True
    if kind == "recent":
        entry["points_earned"] = assignment.get("points_earned")
        entry["percentage"] = assignment.get("percentage")
        if assignment.get("score_raw"):
            entry["score_raw"] = assignment.get("score_raw")
    if assignment.get("status"):
        entry["status"] = assignment.get("status")
        entry["status_label"] = assignment.get("status_label")
    return entry


def analyze_class(class_meta, assignments, today):
    mark = class_meta.get("mark") or ""
    if class_has_real_grade(class_meta, assignments):
        grade_pct = safe_float(class_meta.get("percent"))
    else:
        grade_pct = None
        mark = ""

    missing = select_missing_assignments(assignments, class_meta, today)
    missing_ids = {id(a) for a in missing}
    pending_grade = [
        a for a in assignments
        if is_past_due_ungraded(a, today) and id(a) not in missing_ids
    ]
    pending_grade.sort(key=lambda a: parse_due_date(a.get("due_date")) or datetime.min)

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
        elif 0 <= days_diff <= 14 and a.get("points_earned") is None:
            upcoming.append(a)

    recent.sort(key=lambda a: parse_due_date(a.get("due_date")) or datetime.min, reverse=True)
    upcoming.sort(key=lambda a: parse_due_date(a.get("due_date")) or datetime.min)

    totals = class_meta.get("gradebook_totals")
    counted_insight = class_meta.get("counted_insight")
    category_breakdown = build_category_breakdown(assignments, totals)
    score_trajectory = compute_score_trajectory(assignments)
    trend = parse_trend_html(class_meta.get("trend", ""))
    performance_pattern = detect_performance_pattern(
        grade_pct, len(missing), category_breakdown, score_trajectory
    )
    urgency, issue_type = infer_urgency_from_analytics(
        grade_pct, len(missing), trend, performance_pattern
    )
    due_today = [
        a for a in upcoming
        if parse_due_date(a.get("due_date"))
        and (parse_due_date(a.get("due_date")).date() - today.date()).days == 0
        and not assignment_turned_in(a)
    ]
    if due_today and urgency in ("ok", "strong"):
        urgency, issue_type = "watch", "due_today"

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

    phantom_zero = is_phantom_zero_grade(class_meta, assignments)
    empty_high, zero_weight = extract_grade_mix_flags(counted_insight)
    posted_vs_counted = {
        "posted_pct": grade_pct,
        "posted_mark": mark,
        "rebuild_pct": (counted_insight or {}).get("rebuild_pct"),
        "matches_posted": False if phantom_zero else (counted_insight or {}).get("matches_posted"),
        "official_is_posted": (not phantom_zero) and grade_pct is not None,
        "phantom_zero": phantom_zero,
    }

    return {
        "period": class_meta.get("period"),
        "course_name": class_meta.get("course_name", ""),
        "teacher": class_meta.get("teacher", ""),
        "current_grade_pct": grade_pct,
        "current_grade_mark": mark,
        "current_grade_source": "aeries_class_summary",
        "phantom_zero": phantom_zero,
        "posted_vs_counted": posted_vs_counted,
        "empty_high_weight_categories": empty_high,
        "zero_weight_categories": zero_weight,
        "trend": trend,
        "missing_assignments": [
            format_assignment_entry(a, today, "missing") for a in missing
        ],
        "pending_grade": [
            format_assignment_entry(a, today, "pending") for a in pending_grade[:8]
        ],
        "awaiting_count": len(pending_grade),
        "stale_awaiting_count": sum(
            1 for a in pending_grade
            if (parse_due_date(a.get("due_date")) is not None
                and (today - parse_due_date(a.get("due_date"))).days >= STALE_AWAITING_DAYS)
        ),
        "recoverable_points": round(recoverable_points, 1),
        "oldest_missing_days": oldest_missing_days,
        "median_missing_days": median_missing_days,
        "category_breakdown": category_breakdown,
        "counted_insight": counted_insight,
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
    today = pacific_today_dt()
    student_name = student_data.get("name", "Student")
    class_analytics = []
    history_context = history_context or {}
    hist_classes = history_context.get("classes") or {}

    for class_meta in student_data.get("classes", []):
        period = class_meta.get("period")
        assignments = assignments_for_class(student_data, class_meta)
        # Skip empty schedule rows; keep ungraded classes that already have posted work
        if not class_has_real_grade(class_meta, assignments) and not assignments:
            continue
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

    tonight_plan = build_tonight_plan(class_analytics)

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

    scheduled = sum(
        1
        for c in student_data.get("classes") or []
        if (c.get("course_name") or "").strip()
    )
    result = {
        "student_name": student_name,
        "date": today.strftime("%A, %B %d, %Y"),
        "timezone": "America/Los_Angeles",
        "as_of": pacific_today().isoformat(),
        "coverage": {
            "scheduled_classes": scheduled,
            "classes_with_portal_work": len(class_analytics),
        },
        "dominant_theme": dominant_theme,
        "total_recoverable_pts": round(total_recoverable, 1),
        "tonight_plan": tonight_plan,
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


def build_tonight_plan(class_analytics, limit=3):
    """Heads-up list: due today, then Aeries-confirmed missing, then due tomorrow.

    Never pending_grade, and never anything Aeries shows as turned in — most of the
    kids' work happens in class, so a blank score is not a to-do.
    """
    items = []
    seen = set()

    def add(course, assignment, reason):
        name = (assignment.get("name") or "").strip()
        if not name or not course or assignment.get("turned_in"):
            return
        key = (_norm_course_name(course), name.lower())
        if key in seen:
            return
        seen.add(key)
        items.append({
            "name": name,
            "class_name": course,
            "reason": reason,
            "due_date": assignment.get("due_date") or "",
        })

    def add_upcoming(days, reason):
        for c in class_analytics or []:
            course = (c.get("course_name") or "").strip()
            for a in c.get("upcoming") or []:
                if a.get("days_until_due") == days:
                    add(course, a, reason)
                    if len(items) >= limit:
                        return

    add_upcoming(0, "due_today")

    if len(items) < limit:
        for c in class_analytics or []:
            course = (c.get("course_name") or "").strip()
            for a in c.get("missing_assignments") or []:
                add(course, a, "missing")
                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break

    if len(items) < limit:
        add_upcoming(1, "due_tomorrow")

    items = items[:limit]
    label = "; ".join(f"{i['name']} ({i['class_name']})" for i in items)
    return {"items": items, "label": label}


def apply_tonight_plan(summary, analytics):
    """Tonight is computed, not left to the model — yesterday's ungraded work cannot win."""
    if not isinstance(summary, dict):
        return summary
    plan = (analytics or {}).get("tonight_plan") or {}
    items = plan.get("items") or []
    summary["focus_tonight"] = (plan.get("label") or "").strip()
    by_class = {}
    for item in items:
        by_class.setdefault(item.get("class_name") or "", []).append(item.get("name") or "")
    for cls in summary.get("classes") or []:
        if not isinstance(cls, dict):
            continue
        key = (cls.get("class_name") or "").strip()
        names = by_class.get(key)
        if not names:
            kn = _norm_course_name(key)
            for ck, cn in by_class.items():
                cn_key = _norm_course_name(ck)
                if kn and cn_key and (kn in cn_key or cn_key in kn):
                    names = cn
                    break
        cls["do_tonight"] = (names[0] if names else "") or ""
    return summary


def _view_assignment(assignment, today, kind):
    """Assignment dict the dashboard can render without re-deriving rules."""
    entry = dict(assignment or {})
    formatted = format_assignment_entry(assignment, today, kind)
    entry.update(formatted)
    entry["description"] = (
        assignment.get("description")
        or formatted.get("name")
        or entry.get("name")
        or ""
    )
    return entry


def _urgency_reason(analyzed):
    missing_n = len(analyzed.get("missing_assignments") or [])
    mark = analyzed.get("current_grade_mark") or ""
    pct = analyzed.get("current_grade_pct")
    urgency = analyzed.get("suggested_urgency") or "ok"
    if pct is None and not mark:
        return f"{missing_n} missing" if missing_n else "No grade yet"
    pct_s = f"{round(pct)}%" if pct is not None else ""
    if urgency == "watch":
        return f"{mark} {pct_s} · {missing_n} missing".strip()
    if urgency == "critical":
        return f"At risk · {missing_n} missing" if missing_n else "Needs improvement"
    if urgency == "strong":
        return "On track"
    return ""


def build_student_view(student_data, history_context=None):
    """Ready-to-render payload so the dashboard does not re-implement scrape rules."""
    today = pacific_today_dt()
    history_context = history_context or {}
    analytics = build_class_analytics(student_data, history_context=history_context)
    tonight = analytics.get("tonight_plan") or {}
    ai = student_data.get("ai_summary") or {}
    trends = student_data.get("class_trends") or {}
    analyzed_by = {
        (c.get("period"), _norm_course_name(c.get("course_name"))): c
        for c in analytics.get("classes") or []
    }

    classes_out = []
    for class_meta in student_data.get("classes") or []:
        name = (class_meta.get("course_name") or "").strip()
        if not name:
            continue
        assignments = assignments_for_class(student_data, class_meta)
        analyzed = analyzed_by.get(
            (class_meta.get("period"), _norm_course_name(name))
        )
        if analyzed is None:
            analyzed = analyze_class(class_meta, assignments, today)

        missing = select_missing_assignments(assignments, class_meta, today)
        missing_ids = {id(a) for a in missing}
        awaiting = [
            a for a in assignments
            if is_past_due_ungraded(a, today) and id(a) not in missing_ids
        ]
        awaiting.sort(key=lambda a: parse_due_date(a.get("due_date")) or datetime.min)
        hist_cls = (history_context.get("classes") or {}).get(name) or {}
        upcoming = []
        recent = []
        for assignment in assignments:
            due = parse_due_date(assignment.get("due_date"))
            if not due:
                continue
            days_diff = (due - today).days
            if assignment.get("points_earned") is not None and -14 <= days_diff < 0:
                recent.append(assignment)
            elif 0 <= days_diff <= 14 and assignment.get("points_earned") is None:
                upcoming.append(assignment)
        recent.sort(key=lambda a: parse_due_date(a.get("due_date")) or datetime.min, reverse=True)
        upcoming.sort(key=lambda a: parse_due_date(a.get("due_date")) or datetime.min)

        ai_cls = None
        for row in ai.get("classes") or []:
            if _norm_course_name(row.get("class_name")) == _norm_course_name(name):
                ai_cls = row
                break
            cn = _norm_course_name(name)
            an = _norm_course_name(row.get("class_name"))
            if cn and an and (cn in an or an in cn):
                ai_cls = row

        tonight_name = ""
        for item in tonight.get("items") or []:
            if _norm_course_name(item.get("class_name")) == _norm_course_name(name):
                tonight_name = item.get("name") or ""
                break
        if not tonight_name and ai_cls:
            tonight_name = (ai_cls.get("do_tonight") or "").strip()
        if not tonight_name:
            due_today = [
                a for a in upcoming
                if parse_due_date(a.get("due_date"))
                and (parse_due_date(a.get("due_date")).date() - today.date()).days == 0
                and not assignment_turned_in(a)
            ]
            if due_today:
                tonight_name = due_today[0].get("description") or ""
            elif missing:
                tonight_name = missing[0].get("description") or ""

        trend = trends.get(name) or {}
        classes_out.append({
            "period": class_meta.get("period"),
            "course_name": name,
            "teacher": class_meta.get("teacher") or "",
            "room": class_meta.get("room") or "",
            "percent": class_meta.get("percent"),
            "mark": analyzed.get("current_grade_mark") or class_meta.get("mark") or "",
            "urgency": analyzed.get("suggested_urgency") or "ok",
            "snap": ((ai_cls or {}).get("snap") or _urgency_reason(analyzed) or "").strip(),
            "story": ((ai_cls or {}).get("story") or "").strip(),
            "do_tonight": tonight_name,
            "missing_count": len(missing),
            "missing": [_view_assignment(a, today, "missing") for a in missing],
            "awaiting_count": len(awaiting),
            "awaiting": [_view_assignment(a, today, "pending") for a in awaiting],
            "stale_awaiting_count": analyzed.get("stale_awaiting_count") or 0,
            "newly_missing": list(hist_cls.get("newly_missing") or []),
            "upcoming": [_view_assignment(a, today, "upcoming") for a in upcoming],
            "recent": [_view_assignment(a, today, "recent") for a in recent],
            "assignments": [_view_assignment(a, today, "recent") for a in assignments],
            "pct_history": trend.get("pct_history") or [],
            "delta_7d": trend.get("delta_7d"),
            "delta_14d": trend.get("delta_14d"),
            "delta_span": trend.get("delta_span"),
            "span_window": trend.get("span_window"),
            "phantom_zero": analyzed.get("phantom_zero"),
            "counted_insight": class_meta.get("counted_insight"),
        })

    return {
        "tonight": tonight.get("items") or [],
        "tonight_label": (tonight.get("label") or "").strip(),
        "headline": (ai.get("headline") or "").strip(),
        "wins": list(ai.get("wins") or [])[:2],
        "classes": classes_out,
        "generated_at": ai.get("generated_at"),
    }


def attach_student_view(student_data, history_context=None):
    student_data["view"] = build_student_view(student_data, history_context=history_context)
    return student_data


def briefing_generated_on_pacific_date(ai, day):
    raw = (ai or {}).get("generated_at") or ""
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(PACIFIC).date() == day


def count_graded_classes(student_data):
    return sum(
        1
        for c in student_data.get("classes") or []
        if class_has_real_grade(c, assignments_for_class(student_data, c))
    )


def count_scheduled_classes(student_data):
    return sum(
        1
        for c in student_data.get("classes") or []
        if (c.get("course_name") or "").strip()
    )


def empty_term_summary(student_data):
    """Static briefing when the portal has no meaningful grades (summer or pre-grade week)."""
    student_name = student_data.get("name", "the student")
    scheduled = count_scheduled_classes(student_data)
    if scheduled > 0:
        headline = (
            f"Schedules are up for {student_name} ({scheduled} classes) — "
            f"no grades or missing work posted yet."
        )
        theme = "schedule_only"
    else:
        headline = (
            f"School year is out — the portal has no current classes or missing work "
            f"for {student_name}."
        )
        theme = "empty_term"
    return {
        "headline": headline,
        "focus_tonight": "",
        "wins": [],
        "classes": [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "analytics_snapshot": {
            "dominant_theme": theme,
            "total_recoverable_pts": 0,
            "empty_term": scheduled == 0,
            "schedule_only": scheduled > 0,
            "scheduled_classes": scheduled,
        },
    }


def academic_fingerprint(student_data):
    """Stable snapshot of grades + assignments (ignores attendance, briefings, timestamps)."""
    classes = sorted(
        [
            {
                "period": c.get("period"),
                "course": (c.get("course_name") or "").strip(),
                "percent": str(c.get("percent") or "").strip(),
                "mark": str(c.get("mark") or "").strip(),
                "missing_count": int(c.get("missing_count") or 0),
            }
            for c in (student_data or {}).get("classes") or []
        ],
        key=lambda row: (row["period"] is None, row["period"], row["course"]),
    )
    assignments = []
    for group in (student_data or {}).get("assignments_by_class") or []:
        for assignment in group.get("assignments") or []:
            assignments.append({
                "period": group.get("period"),
                "class": group.get("class_name") or "",
                "number": assignment.get("number") or "",
                "name": assignment.get("description") or "",
                "due": assignment.get("due_date") or "",
                "earned": assignment.get("points_earned"),
                "pct": assignment.get("percentage"),
                "score_raw": assignment.get("score_raw") or "",
                "complete": bool(assignment.get("grading_complete")),
            })
    assignments.sort(
        key=lambda row: (
            row["period"] is None,
            row["period"],
            row["class"],
            row["number"],
            row["name"],
        )
    )
    return json.dumps(
        {"classes": classes, "assignments": assignments},
        sort_keys=True,
        default=str,
    )


_DUE_WEEKDAY_RE = re.compile(
    r"\bdue\s+(Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|"
    r"Friday|Saturday|Sunday)\b(?!\s+[A-Za-z]{3}\s+\d)",
    re.IGNORECASE,
)


def _assignment_display_name(entry):
    return (entry.get("name") or entry.get("description") or "").strip()


def iter_analytics_assignments(class_analytics):
    for key in ("missing_assignments", "pending_grade", "recent_scores", "upcoming"):
        for item in (class_analytics or {}).get(key) or []:
            yield item


def rewrite_stale_due_copy(text, assignment_entries):
    """Replace weekday-only or past-date 'due …' with precomputed due_label."""
    if not text:
        return text
    out = str(text)
    entries = [
        a for a in (assignment_entries or [])
        if _assignment_display_name(a) and a.get("due_label")
    ]
    entries.sort(key=lambda a: len(_assignment_display_name(a)), reverse=True)
    weekday = (
        r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|"
        r"Thursday|Friday|Saturday|Sunday)"
    )
    for a in entries:
        name = _assignment_display_name(a)
        label = a.get("due_label")
        state = a.get("due_state")
        if name not in out:
            continue
        named_weekday = re.compile(
            re.escape(name) + rf"(\s*[·\-–—:]\s*)due\s+{weekday}\b(?!\s+[A-Za-z]{{3}}\s+\d)",
            re.IGNORECASE,
        )
        if named_weekday.search(out):
            out = named_weekday.sub(lambda m, n=name, lb=label: n + m.group(1) + lb, out)
            continue
        if state in ("completed", "overdue", "pending"):
            named_due = re.compile(
                re.escape(name) + r"(\s*[·\-–—:]\s*)due\s+[^\n·]+",
                re.IGNORECASE,
            )
            if named_due.search(out):
                out = named_due.sub(lambda m, n=name, lb=label: n + m.group(1) + lb, out)
    if _DUE_WEEKDAY_RE.search(out):
        fallback = next(
            (
                a for a in entries
                if a.get("due_state") in ("completed", "overdue", "pending")
            ),
            None,
        )
        if fallback and fallback.get("due_label"):
            out = _DUE_WEEKDAY_RE.sub(fallback["due_label"], out)
        else:
            upcoming = next(
                (
                    a for a in entries
                    if a.get("due_state") in ("upcoming", "today") and a.get("due_label")
                ),
                None,
            )
            if upcoming:
                out = _DUE_WEEKDAY_RE.sub(upcoming["due_label"], out)
    return out


def rewrite_phantom_zero_copy(text):
    """Parent copy must not call a hidden 0% tile 'at 0%'."""
    if not text:
        return text
    out = str(text)
    out = re.sub(r"\bGrade at 0%", "No grade yet", out, flags=re.IGNORECASE)
    out = re.sub(r"\bat 0%", "with no grade yet", out, flags=re.IGNORECASE)
    out = re.sub(r"\b0%\s+needs attention", "no grade yet needs attention", out, flags=re.IGNORECASE)
    out = re.sub(r"\bposted grade remains 0%", "no grade posted yet", out, flags=re.IGNORECASE)
    out = re.sub(r"\bremains 0%", "is not posted yet", out, flags=re.IGNORECASE)
    out = re.sub(r"(?<![\d.])0%", "no grade yet", out)
    return out


def sanitize_briefing_copy(summary, analytics):
    """Post-process Grok JSON so leftover due-Tue / at-0% copy cannot ship."""
    if not isinstance(summary, dict):
        return summary
    by_name = {}
    for ca in (analytics or {}).get("classes") or []:
        name = (ca.get("course_name") or "").strip()
        if name:
            by_name[name] = ca

    def _match_class(class_name):
        key = (class_name or "").strip()
        if key in by_name:
            return by_name[key]
        kn = _norm_course_name(key)
        best = None
        best_len = 0
        for name, ca in by_name.items():
            cn = _norm_course_name(name)
            if kn and cn and (kn in cn or cn in kn) and len(cn) > best_len:
                best = ca
                best_len = len(cn)
        return best

    any_phantom = any(ca.get("phantom_zero") for ca in by_name.values())
    all_entries = []
    for ca in by_name.values():
        all_entries.extend(iter_analytics_assignments(ca))

    headline = summary.get("headline") or ""
    headline = rewrite_stale_due_copy(headline, all_entries)
    if any_phantom:
        headline = rewrite_phantom_zero_copy(headline)
    summary["headline"] = headline

    wins = []
    for win in summary.get("wins") or []:
        text = rewrite_stale_due_copy(win, all_entries)
        if any_phantom:
            text = rewrite_phantom_zero_copy(text)
        wins.append(text)
    if "wins" in summary:
        summary["wins"] = wins

    for cls in summary.get("classes") or []:
        if not isinstance(cls, dict):
            continue
        ca = _match_class(cls.get("class_name"))
        entries = list(iter_analytics_assignments(ca)) if ca else all_entries
        for field in ("snap", "story"):
            text = cls.get(field) or ""
            text = rewrite_stale_due_copy(text, entries)
            if ca and ca.get("phantom_zero"):
                text = rewrite_phantom_zero_copy(text)
            cls[field] = text
    return summary


def annotate_assignment_due_fields(student_data, today=None):
    """Attach due_label / due_state onto portal assignment rows the UI also reads."""
    today = today or pacific_today_dt()
    for group in student_data.get("assignments_by_class") or []:
        class_meta = {}
        g_period = group.get("period")
        g_name = _norm_course_name(group.get("class_name"))
        for c in student_data.get("classes") or []:
            same_period = g_period is not None and c.get("period") == g_period
            same_name = g_name and _norm_course_name(c.get("course_name")) == g_name
            if same_period or same_name:
                class_meta = c
                break
        assignments = group.get("assignments") or []
        missing = select_missing_assignments(assignments, class_meta, today)
        missing_ids = {id(a) for a in missing}
        for a in assignments:
            if id(a) in missing_ids:
                kind = "missing"
            elif assignment_has_score_mark(a):
                kind = "recent"
            else:
                due = parse_due_date(a.get("due_date"))
                if due and due >= today:
                    kind = "upcoming"
                else:
                    kind = "pending"
            a["due_label"] = assignment_due_label(
                a, today, kind=kind, is_missing=(kind == "missing")
            )
            a["due_state"] = assignment_due_state(
                a, today, kind=kind, is_missing=(kind == "missing")
            )
    return student_data


def annotate_class_truth_flags(student_data):
    """Mark phantom-0 classes so the dashboard and briefing share one truth."""
    for class_meta in student_data.get("classes") or []:
        assignments = assignments_for_class(student_data, class_meta)
        class_meta["phantom_zero"] = is_phantom_zero_grade(class_meta, assignments)
    return student_data


def generate_ai_summary(student_data, history_context=None):
    """Call Grok API to generate a unified family daily briefing."""
    # No real grades and no posted work yet — don't invent urgency
    has_assignments = any(
        ca.get("assignments")
        for ca in student_data.get("assignments_by_class") or []
    )
    if count_graded_classes(student_data) == 0 and not has_assignments:
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

    now_pt = pacific_now()
    today_label = now_pt.strftime("%A, %B %d, %Y")
    system_prompt = f"""You write the daily briefing a parent reads in 10 seconds on a phone or car screen. One output. {student_name}'s family may all see it. Facts at a glance — not a coaching script.

Today is {today_label} (Pacific). Due dates in PRECOMPUTED_ANALYTICS already use Pacific time. Trust due_label / due_state / days_overdue / days_until_due. Never call something overdue or "yesterday" unless days_overdue >= 1 or due_state is overdue. Due today is not missing.

DATA SCOPE
- Facts only from PRECOMPUTED_ANALYTICS (Aeries parent portal). Do not redo math.
- current_grade_pct / current_grade_mark is the official Aeries ClassSummary Percent / CurrentMark when phantom_zero is false. Never replace a real posted grade with assignment averages, category averages, or counted_insight.rebuild_pct.
- If phantom_zero is true, Aeries posted 0% with no letter mark — the family tile shows "—" / no grade yet. Never write "at 0%", "0% needs attention", or treat that placeholder as a current grade.
- You MAY explain WHY using precomputed facts only: phantom_zero, posted_vs_counted (rebuild vs posted), empty_high_weight_categories (e.g. empty 70% Assessments), zero_weight_categories (e.g. 0% Daily Assignments). Do not invent other causes.
- category_breakdown.*.assignment_avg_pct is the unweighted mean of scored assignment percentages in that bucket — not the class grade and not Aeries' weighted category %. Do not write a class as "at 72%" because seven assignments average 72. You may say scored work averages 73% while no class grade is posted when phantom_zero is true and posted_vs_counted.rebuild_pct is present.
- If counted_insight is present, you may name which categories count, are 0% weight, or are empty. If rebuild_pct differs from current_grade_pct and the posted grade is real, still use the posted grade.
- Portal can lag (turned in but not graded, Canvas/paper work). Prefer "portal still shows …" over "never did …".
- Do not invent causes, effort, psychology, or teacher fairness.
- Do not invent week-scale stories unless history.delta_7d / trend_label is present.

PARENT SKIM (this is the job)
- Headline: one sentence — what needs attention, plus one real win if there is one. Not a roster of every class. A WHY clause is welcome when a precomputed flag explains it.
- If coverage.classes_with_portal_work is much smaller than coverage.scheduled_classes, add a short clause that most classes have no work posted yet (normal early in the term). Do not treat silence as all-clear, and do not panic about empty gradebooks.
- focus_tonight: copy tonight_plan.label exactly (empty if tonight_plan.items is empty). Due today beats missing beats due tomorrow. pending_grade is submitted/awaiting a teacher score — not tonight, not missing. Anything with turned_in true was handed in (usually in class) and is never a to-do.
- Most of this work happens in class. Missing means only what the Aeries portal itself flags; a blank score is not missing and not homework.
- Awaiting score is not missing. Never count pending_grade items (awaiting_count) in any missing total or missing points; only missing_assignments are missing. If a pending_grade item is stale (10+ days), at most say it is worth asking the teacher about.
- wins: 0–2 specifics with evidence (grade + assignment). Skip empty praise.

VOICE
- Third person with "{student_name}" (never "you", never "as a parent…")
- Plain English, calm, specific. Short sentences.
- No jargon: never say completion_gap, performance_pattern, recoverable points, dominant_theme, issue_type, Summatives, or "Pattern suggests"
- Name the assignment, not the Aeries category — except when empty_high_weight_categories / zero_weight_categories is the WHY
- No pep talks, conversation openers, or parent tips

Respond ONLY with valid JSON matching this exact schema:
{{
  "headline": "ONE sentence a parent can skim",
  "focus_tonight": "Named assignment(s) + class; empty if nothing due today/tomorrow or Aeries-flagged missing",
  "wins": ["0–2 short genuine positives with evidence"],
  "classes": [
    {{
      "class_name": "must match course_name from analytics",
      "urgency": "critical | watch | ok | strong",
      "snap": "Name the assignment and copy due_label (e.g. Anatomy of a Great Game · 5/5). May be longer than 12 words if needed to tell the truth.",
      "story": "One sentence of context the closed card does not already say. Do not repeat the missing list. WHY from precomputed flags is allowed.",
      "do_tonight": "Named assignment if action is needed; else empty"
    }}
  ]
}}

Urgency (match suggested_urgency unless you have a strong reason not to — drives dashboard colors):
- critical: grade below 70%, OR below 80% WITH missing work
- watch: missing/due work, or a B with a slipping trend — backlog, not an F
- ok: solid, or no grade yet without missing work
- strong: A / 90%+, no missing

Rules:
- Include EVERY class from analytics.classes (not the full 9-period schedule)
- If analytics.classes is empty: headline says no posted work yet — do not invent status
- Order classes: critical → watch → ok → strong
- ok/strong: leave do_tonight empty
- critical/watch: do_tonight is that class's tonight_plan item; empty if the class is not in tonight_plan
- pending_grade: do not tell the family to redo it tonight; portal often lags after turn-in
- Copy each assignment's due_label (and due_state). Never write weekday-only due lines ("due Tue", "due Wednesday"). Past scored work must not say "due".
- Do not write "portal shows no missing work" as the story. If the only news is a due-today item, leave story short or empty.
- wins must be real — skip them if there are none
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
        return sanitize_briefing_copy(apply_tonight_plan(summary, analytics), analytics)

    except requests.exceptions.Timeout:
        print("  WARNING: Grok API timed out")
        return None
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"  WARNING: Failed to parse Grok response: {e}")
        return None


def _prepare_student_for_briefing(student_data, history, aeries_series_by_class=None):
    """Build analytics, history context, UI trend fields; return history_context."""
    attach_gradebook_insights(
        student_data.get("classes") or [],
        student_data.get("assignments_by_class") or [],
    )
    annotate_assignment_due_fields(student_data)
    annotate_class_truth_flags(student_data)
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
        raise ScrapeError("GROK_API_KEY must be set")

    if not OUTPUT_FILE.exists():
        raise ScrapeError(f"{OUTPUT_FILE} not found — run a full scrape first")

    try:
        data = json.loads(OUTPUT_FILE.read_text())
    except json.JSONDecodeError as e:
        raise ScrapeError(f"Could not parse {OUTPUT_FILE}: {e}")

    if is_summer_break(data):
        print("Summer break mode is active (SUMMER_BREAK) — skipping Grok API calls.")
        data["summer_break"] = True
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        OUTPUT_FILE.write_text(json.dumps(data, indent=2))
        return

    students = data.get("students", [])
    if not students:
        raise ScrapeError("No students found in grades_data.json")

    history = load_grade_history()
    print(f"Regenerating AI briefings for {len(students)} student(s) from {OUTPUT_FILE.name}...")

    for i, student in enumerate(students, start=1):
        print(f"  {student_log_label(i)}...")
        # Keep prior briefing for previous_briefing context before overwrite
        history_context, _ = _prepare_student_for_briefing(student, history)
        ai_summary = generate_ai_summary(student, history_context=history_context)
        if ai_summary:
            student["ai_summary"] = ai_summary
            print(f"    AI summary generated ({len(ai_summary.get('classes', []))} classes)")
        else:
            print("    WARNING: AI summary failed — keeping previous briefing if any")
        attach_student_view(student, history_context=history_context)

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
            except (SystemExit, ScrapeError):
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

    failed = 0
    for i, student in enumerate(STUDENTS, start=1):
        label = student_log_label(i)
        prior = previous_by_sn.get(str(student["sn"])) or {}
        print(f"Fetching data for {label}...")
        try:
            switch_student(session, student["school_code"], student["sn"])

            # Class summary (grades overview)
            raw_summary = fetch_class_summary(session)
            classes = parse_class_summary(raw_summary)
            print(f"  {len(classes)} classes")

            # Assignments per class
            print("  Fetching assignments...")
            class_assignments = fetch_all_assignments(session)
            attach_gradebook_insights(classes, class_assignments)
            total_assignments = sum(len(ca["assignments"]) for ca in class_assignments)
            totals_n = sum(1 for ca in class_assignments if ca.get("totals"))
            print(f"  {total_assignments} assignments across {len(class_assignments)} classes")
            print(f"  GradebookDetails totals parsed for {totals_n}/{len(class_assignments)} class(es)")

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

            issue = portal_looks_incomplete(student_data, prior, in_session=True)
            if issue:
                print(f"  WARNING: {issue} — keeping previous snapshot")
                failed += 1
                if prior:
                    data["students"].append(prior)
                continue

            # Carry prior briefing so history_context can reference last focus
            if prior.get("ai_summary"):
                student_data["ai_summary"] = prior["ai_summary"]

            history_context, class_list = _prepare_student_for_briefing(
                student_data, history, aeries_series_by_class=aeries_series
            )

            if GROK_API_KEY:
                portal_changed = academic_fingerprint(student_data) != academic_fingerprint(prior)
                briefing_today = briefing_generated_on_pacific_date(
                    prior.get("ai_summary"), pacific_today()
                )
                if not portal_changed and briefing_today and prior.get("ai_summary"):
                    student_data["ai_summary"] = prior["ai_summary"]
                    print("  AI summary kept (grades/missing unchanged)")
                else:
                    print(f"  Generating AI summary ({GROK_MODEL})...")
                    ai_summary = generate_ai_summary(
                        student_data, history_context=history_context
                    )
                    if ai_summary:
                        student_data["ai_summary"] = ai_summary
                        print("  AI summary generated")
                    else:
                        print("  AI summary skipped")

            if student_data.get("ai_summary"):
                analytics = build_class_analytics(
                    student_data, history_context=history_context
                )
                student_data["ai_summary"] = sanitize_briefing_copy(
                    apply_tonight_plan(student_data["ai_summary"], analytics),
                    analytics,
                )

            attach_student_view(student_data, history_context=history_context)

            # Append today's grade snapshot after briefing (stores previous_briefing from prior AI)
            # Prefer storing the briefing we just replaced as previous — rebuild snapshot with prior AI
            snap_source = dict(student_data)
            if prior.get("ai_summary"):
                snap_source["ai_summary"] = prior["ai_summary"]
            snapshot = build_student_snapshot(snap_source, class_list)
            upsert_student_snapshot(
                history, student["sn"], student["name"], snapshot
            )
            print(
                "  History snapshots: "
                f"{len((history.get('students') or {}).get(str(student['sn']), {}).get('snapshots') or [])}"
            )

            data["students"].append(student_data)
        except Exception as e:
            print(f"  WARNING: scrape failed for {label} ({e})")
            failed += 1
            if prior:
                data["students"].append(prior)

    if not data["students"] and previous_data.get("students"):
        data["students"] = previous_data["students"]

    data["summer_break"] = False

    save_grade_history(history)
    OUTPUT_FILE.write_text(json.dumps(data, indent=2))
    print(f"\nData written to {OUTPUT_FILE}")
    print(f"History written to {HISTORY_FILE}")
    print(f"Last updated: {data['last_updated']}")
    print(f"Summer break: {data['summer_break']}")
    if STUDENTS and failed == len(STUDENTS):
        raise ScrapeError("every student scrape failed or failed the portal sanity check")


def probe_gradebook():
    """Login and dump GradebookDetails dropdown terms for each student (debug)."""
    require_scrape_config()
    session = login()
    for i, student in enumerate(STUDENTS, start=1):
        print(f"\n=== PROBE gradebook: {student_log_label(i)} ===")
        switch_student(session, student["school_code"], student["sn"])
        resp = session.get(
            f"{BASE_URL}/student/GradebookDetails.aspx",
            allow_redirects=True,
            timeout=60,
        )
        print(f"status={resp.status_code} final={resp.url} len={len(resp.text)}")
        if "LoginParent" in resp.url:
            print("  skip (login)")
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        class_select = soup.find("select", {"id": re.compile("dlGN")})
        if not class_select:
            print("  no dlGN select")
            ids = [el.get("id") for el in soup.find_all(True, id=True)][:40]
            print(f"  sample ids: {ids}")
            continue
        options = class_select.find_all("option")
        print(f"  {len(options)} option(s)")
        for opt in options:
            label = opt.get_text(" ", strip=True)
            parsed = parse_gradebook_option_label(label)
            sel = " SELECTED" if opt.has_attr("selected") else ""
            print(
                f"    {opt.get('value')!r}{sel}: {label!r} "
                f"term={parsed.get('term_key')!r} start={parsed.get('start_date')} "
                f"name={parsed.get('name')!r}"
            )
        chosen, meta = select_current_gradebook_options(options)
        print("  select meta", json.dumps(meta, default=str))
        print("  chosen", [(value, extract_class_name(label)) for value, label in chosen])


def rebuild_views_only():
    """Rebuild dashboard view payloads from existing grades_data.json (no Aeries)."""
    if not OUTPUT_FILE.exists():
        raise ScrapeError(f"{OUTPUT_FILE} not found — run a full scrape first")
    try:
        data = json.loads(OUTPUT_FILE.read_text())
    except json.JSONDecodeError as e:
        raise ScrapeError(f"Could not parse {OUTPUT_FILE}: {e}")
    history = load_grade_history()
    students = data.get("students") or []
    print(f"Rebuilding dashboard views for {len(students)} student(s)...")
    for i, student in enumerate(students, start=1):
        print(f"  {student_log_label(i)}...")
        history_context, _ = _prepare_student_for_briefing(student, history)
        attach_student_view(student, history_context=history_context)
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    OUTPUT_FILE.write_text(json.dumps(data, indent=2))
    print(f"Views written to {OUTPUT_FILE}")


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
    parser.add_argument(
        "--probe-gradebook",
        action="store_true",
        help="Login and dump GradebookDetails dropdown terms (no grades_data write)",
    )
    parser.add_argument(
        "--attendance-only",
        action="store_true",
        help="Refresh absences/tardies only (works during summer; no Grok)",
    )
    parser.add_argument(
        "--rebuild-view",
        action="store_true",
        help="Rebuild dashboard view JSON from existing grades_data.json (no Aeries)",
    )
    args = parser.parse_args()

    try:
        if args.probe_attendance:
            probe_attendance()
        elif args.probe_gradebook:
            probe_gradebook()
        elif args.attendance_only:
            refresh_attendance_only()
        elif args.grok_only:
            regenerate_grok_summaries()
        elif args.rebuild_view:
            rebuild_views_only()
        else:
            scrape_all()
    except ScrapeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
