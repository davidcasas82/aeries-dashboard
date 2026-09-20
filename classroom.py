"""Google Classroom context for the dashboard.

The student's Apps Script (tools/classroom_apps_script.gs) writes
classroom_export.json into their Drive and shares it with a service account.
This module downloads those exports, maps each Classroom course onto an Aeries
class, matches Classroom items to Aeries gradebook rows, and derives the
signals the dashboard and Grok can use.

Nothing here logs student names or numbers. Exports are keyed by
``student_slot`` (1 = STUDENT_1, 2 = STUDENT_2).
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
EXPORT_FILE_NAME = "classroom_export.json"
PACIFIC = ZoneInfo("America/Los_Angeles")

TURNED_IN_STATES = {"TURNED_IN", "RETURNED"}
NOT_STARTED_STATES = {"NEW", "CREATED", "RECLAIMED_BY_STUDENT", ""}
STATE_LABELS = {
    "TURNED_IN": "Turned in on Classroom",
    "RETURNED": "Returned by teacher",
    "RECLAIMED_BY_STUDENT": "Unsubmitted on Classroom",
    "CREATED": "Not turned in on Classroom",
    "NEW": "Not started on Classroom",
    "": "",
}

INSTRUCTIONS_LIMIT = 1200
EXCERPT_LIMIT = 600
MATERIAL_TEXT_LIMIT = 1500
STAMP_TEXT_LIMIT = 400
ANNOUNCEMENT_LIMIT = 240
UPCOMING_WINDOW_DAYS = 14
RECENT_WINDOW_DAYS = 7
# Kid dump may roll 400 days; product is this school year. Syllabus often
# lands a couple of weeks before the first instructional day.
SCHOOL_YEAR_PRE_DAYS = 21
CALENDAR_FILE = Path(__file__).resolve().parent / "school_calendar.json"

_STOP_TOKENS = {"the", "of", "and", "a", "an", "to", "for", "in", "on", "with", "&", "-"}
_TITLE_NOISE = {
    "hw", "homework", "assignment", "worksheet", "ws", "pg", "pgs", "p", "pp", "due",
    "submit", "submission", "form", "practice", "packet", "page", "pages",
}
# Words that only carry a number ("LT 1.4", "Unit 2") are compared as codes, not words.
_CODE_WORDS = {"lt", "unit", "lesson", "ch", "chapter", "section", "sec", "mc", "cyu"}

COURSE_MATCH_THRESHOLD = 2.5
COURSE_RESCUE_THRESHOLD = 0.75
ITEM_MATCH_THRESHOLD = 0.6


# --------------------------------------------------------------------------- fetch


def _service_account_info():
    raw = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        print("  Classroom: GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON; skipping")
        return None
    if not info.get("client_email") or not info.get("private_key"):
        print("  Classroom: service account JSON is missing client_email/private_key; skipping")
        return None
    return info


def _drive_token(info):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
    except ImportError:
        print("  Classroom: google-auth not installed; skipping")
        return None
    creds = service_account.Credentials.from_service_account_info(info, scopes=[DRIVE_SCOPE])
    creds.refresh(Request())
    return creds.token


def fetch_classroom_exports(session=None, timeout=30):
    """Return {student_slot: export_payload} for every export shared with the service account."""
    info = _service_account_info()
    if not info:
        return {}
    try:
        token = _drive_token(info)
    except Exception as e:  # noqa: BLE001 - never let Classroom break the Aeries scrape
        print(f"  Classroom: could not get a Drive token ({type(e).__name__})")
        return {}
    if not token:
        return {}

    http = session or requests.Session()
    headers = {"Authorization": f"Bearer {token}"}
    try:
        res = http.get(
            DRIVE_FILES_URL,
            params={
                "q": f"name = '{EXPORT_FILE_NAME}' and trashed = false",
                "fields": "files(id,name,modifiedTime)",
                "pageSize": 20,
            },
            headers=headers,
            timeout=timeout,
        )
        res.raise_for_status()
        files = res.json().get("files") or []
    except Exception as e:  # noqa: BLE001
        print(f"  Classroom: Drive listing failed ({type(e).__name__})")
        return {}

    exports = {}
    for f in files:
        try:
            body = http.get(
                f"{DRIVE_FILES_URL}/{f['id']}",
                params={"alt": "media"},
                headers=headers,
                timeout=timeout,
            )
            body.raise_for_status()
            payload = body.json()
        except Exception as e:  # noqa: BLE001
            print(f"  Classroom: could not read one export ({type(e).__name__})")
            continue
        slot = payload.get("student_slot")
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            print("  Classroom: export without a student_slot; skipping")
            continue
        prev = exports.get(slot)
        if prev and (prev.get("captured_at") or "") >= (payload.get("captured_at") or ""):
            continue
        exports[slot] = payload
    for slot in sorted(exports):
        p = exports[slot]
        n_items = sum(len(c.get("items") or []) for c in p.get("courses") or [])
        print(
            f"  Classroom export for student {slot}: {len(p.get('courses') or [])} courses, "
            f"{n_items} items, captured {p.get('captured_at') or '?'}"
        )
    return exports


# --------------------------------------------------------------------------- text helpers


def _clip(text, limit):
    text = re.sub(r"[ \t]+", " ", (text or "").strip())
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _tokens(text, drop_noise=False):
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    out = []
    for t in toks:
        if t in _STOP_TOKENS:
            continue
        if drop_noise and t in _TITLE_NOISE:
            continue
        out.append(t)
    return out


def strip_student_name(title, names=()):
    """Drop the "First Last - " prefix Classroom puts on per-student copies.

    Also drops any configured first names (STUDENT_n_NAME) anywhere in the title.
    Never echoes the names back, so callers can log the result.
    """
    title = (title or "").strip()
    if not title:
        return ""
    for n in names:
        n = (n or "").strip()
        if len(n) >= 2:
            title = re.sub(rf"\b{re.escape(n)}(?:'s)?\b(?:\s+[A-Z][\w'.-]+)?\s*[-–:]\s*", "", title)
            title = re.sub(rf"\b{re.escape(n)}(?:'s)?\b", "", title)
    m = re.match(r"^([A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*){1,3})\s+[-–]\s+(.+)$", title)
    if m and not re.search(r"\d", m.group(1)):
        title = m.group(2)
    title = re.sub(r"\(\s*\)|\[\s*\]", "", title)
    return re.sub(r"\s{2,}", " ", title).strip(" -–:")


def _student_first_names():
    names = []
    for i in range(1, 10):
        n = os.getenv(f"STUDENT_{i}_NAME")
        if n:
            names.append(n.strip())
    return names


# --------------------------------------------------------------------------- dates


def due_to_pacific_date(due):
    """Classroom due is an ISO instant (UTC) or a bare YYYY-MM-DD; return a Pacific date."""
    if not due:
        return None
    s = str(due).strip()
    try:
        if len(s) == 10:
            return datetime.strptime(s, "%Y-%m-%d").date()
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(PACIFIC).date()
    except ValueError:
        return None


def _iso_to_pacific_date(stamp):
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(PACIFIC).date()


def _iso_day(stamp):
    d = _iso_to_pacific_date(stamp)
    return d.isoformat() if d else ""


def _aeries_date(mmddyyyy):
    try:
        return datetime.strptime(mmddyyyy or "", "%m/%d/%Y").date()
    except ValueError:
        return None


def _to_aeries_date(d):
    return d.strftime("%m/%d/%Y") if d else ""


def _parse_iso_date_only(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return _iso_to_pacific_date(s)


def load_school_calendar(path=None):
    path = Path(path) if path else Path(os.getenv("SCHOOL_CALENDAR_FILE") or CALENDAR_FILE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def school_year_for(today, calendar=None):
    """Active school-year dict, else upcoming, else most recently completed."""
    if hasattr(today, "date"):
        today = today.date()
    calendar = calendar if calendar is not None else load_school_calendar()
    years = calendar.get("years") or []
    active = None
    upcoming = None
    completed = []
    for y in years:
        first = _parse_iso_date_only(y.get("first_day"))
        last = _parse_iso_date_only(y.get("last_day"))
        if first and last and first <= today <= last:
            active = y
            break
        if first and first > today:
            if upcoming is None or first < _parse_iso_date_only(upcoming.get("first_day")):
                upcoming = y
        elif last and last < today:
            completed.append((last, y))
    if active:
        return active
    if upcoming:
        return upcoming
    if completed:
        completed.sort(key=lambda t: t[0], reverse=True)
        return completed[0][1]
    return None


def school_year_bounds(today, calendar=None):
    """Product window: (year_id, start, end) or None if the calendar is empty.

    ``start`` is first instructional day minus ``SCHOOL_YEAR_PRE_DAYS``.
    """
    year = school_year_for(today, calendar=calendar)
    if not year:
        return None
    first = _parse_iso_date_only(year.get("first_day"))
    last = _parse_iso_date_only(year.get("last_day"))
    if not first or not last:
        return None
    return (year.get("id") or "", first - timedelta(days=SCHOOL_YEAR_PRE_DAYS), last)


def item_in_school_year(item, start, end):
    """True when any of due / assigned / updated / scheduled falls in the window.

    Undated items stay; dropping them would hide materials teachers never dated.
    """
    dates = []
    for key in ("due", "assigned_on", "updated_on", "scheduled_on"):
        d = _parse_iso_date_only(item.get(key))
        if d:
            dates.append(d)
    if not dates:
        return True
    return any(start <= d <= end for d in dates)


def drive_folder_in_year(folder, start, end):
    """Prefer the script's ``current_year`` flag; fall back to created_at."""
    flag = folder.get("current_year")
    if flag is True:
        return True
    if flag is False:
        return False
    created = _iso_to_pacific_date(folder.get("created_at"))
    if created:
        return start <= created <= end
    return False


# --------------------------------------------------------------------------- course mapping

_PERIOD_PATTERNS = [
    re.compile(r"\bP(?:er(?:iod)?)?\.?\s*-?\s*(\d)\b", re.I),
    re.compile(r"\b(\d)(?:st|nd|rd|th)?\s*(?:per(?:iod)?)\b", re.I),
    re.compile(r"[-–]\s*(\d)\s+\1\s*$"),
    re.compile(r"[-–]\s*(\d)\s*$"),
]


def classroom_period(course):
    """Best-effort period number from a Classroom course name/section."""
    for text in (course.get("section") or "", course.get("name") or ""):
        for pat in _PERIOD_PATTERNS:
            m = pat.search(text)
            if m:
                return int(m.group(1))
    return None


def _as_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def score_course_match(course, class_meta):
    """How strongly a Classroom course looks like an Aeries class row."""
    text = f"{course.get('name') or ''} {course.get('section') or ''}"
    text_l = text.lower()
    score = 0.0

    teacher = (class_meta.get("teacher") or "").strip().rstrip(",")
    last = teacher.split(",")[0].split()[-1].lower() if teacher else ""
    if len(last) >= 3 and re.search(rf"\b{re.escape(last)}\b", text_l):
        score += 3

    # A period is unique inside one student's schedule, so a period match alone
    # ("Period 8" with nothing else in the name) is enough to map.
    period = _as_int(class_meta.get("period"))
    cp = classroom_period(course)
    if period is not None and cp is not None:
        score += COURSE_MATCH_THRESHOLD if cp == period else -COURSE_MATCH_THRESHOLD

    a_toks = [t for t in _tokens(class_meta.get("course_name")) if not t.isdigit() or len(t) > 1]
    c_toks = [t for t in _tokens(text)]
    if a_toks:
        hits = 0
        for t in a_toks:
            hits += _token_hit(t, c_toks)
        score += 2.5 * hits / len(a_toks)
    return round(score, 2)


def _is_abbreviation(short, long):
    """'mkt' → 'marketing', 'grphdes' → 'graphicdesign': same first letter, in-order subsequence."""
    if len(short) < 3 or len(short) >= len(long) or short[0] != long[0]:
        return False
    pos = 0
    for ch in short:
        pos = long.find(ch, pos)
        if pos < 0:
            return False
        pos += 1
    return True


def _token_hit(aeries_token, classroom_tokens):
    """1 for an exact token, 0.85 for an Aeries abbreviation of one or two Classroom words, 0.7 for a prefix."""
    t = aeries_token
    if t in classroom_tokens:
        return 1.0
    if len(t) < 3:
        return 0.0
    for idx, c in enumerate(classroom_tokens):
        if (c.startswith(t) or t.startswith(c)) and min(len(c), len(t)) >= 3:
            return 0.7
        if _is_abbreviation(t, c):
            return 0.85
        if idx + 1 < len(classroom_tokens) and _is_abbreviation(t, c + classroom_tokens[idx + 1]):
            return 0.85
    return 0.0


def map_courses_to_classes(courses, classes, overrides=None):
    """Return {course_id: class_meta} using overrides (course_id -> period) then scoring.

    Pass 1 takes confident matches (teacher name, period, or a clear name match).
    Pass 2 rescues a leftover course that is the only weak candidate for the only
    leftover Aeries class it resembles ("Intro to Spanish" → "Span 1 (IVC)").
    """
    overrides = overrides or {}
    by_period = {}
    real_classes = [c for c in classes or [] if (c.get("course_name") or "").strip()]
    for c in real_classes:
        p = _as_int(c.get("period"))
        if p is not None:
            by_period.setdefault(p, c)

    mapping = {}
    taken = set()
    scores = {}
    for course in courses or []:
        cid = str(course.get("id") or "")
        forced = overrides.get(cid)
        if forced is not None and _as_int(forced) in by_period:
            mapping[cid] = by_period[_as_int(forced)]
            taken.add(id(mapping[cid]))
            continue
        for cm in real_classes:
            scores[(cid, id(cm))] = (score_course_match(course, cm), cid, cm)

    confident = [v for v in scores.values() if v[0] >= COURSE_MATCH_THRESHOLD]
    for s, cid, cm in sorted(confident, key=lambda x: -x[0]):
        if cid in mapping or id(cm) in taken:
            continue
        mapping[cid] = cm
        taken.add(id(cm))

    leftovers = [
        v for v in scores.values()
        if v[1] not in mapping and id(v[2]) not in taken and v[0] >= COURSE_RESCUE_THRESHOLD
    ]
    by_course = {}
    by_class = {}
    for s, cid, cm in leftovers:
        by_course.setdefault(cid, []).append((s, cm))
        by_class.setdefault(id(cm), []).append((s, cid))
    for cid, cands in by_course.items():
        if len(cands) != 1:
            continue
        s, cm = cands[0]
        if len(by_class.get(id(cm)) or []) != 1 or id(cm) in taken:
            continue
        mapping[cid] = cm
        taken.add(id(cm))
    return mapping


def load_course_overrides(path=None):
    path = path or os.getenv("CLASSROOM_MAP_FILE") or "classroom_map.json"
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- item matching


_DATE_PREFIX_RE = re.compile(
    r"^\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*"
    r"\d{1,2}(?:\s*[-–]\s*\d{1,2})?(?:\s*[-–]\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*\d{1,2})?"
    r"\s*[:\-–]\s*",
    re.I,
)
_DECIMAL_CODE_RE = re.compile(r"(?<![\w.])(\d+\.\d+[a-z]?)(?![\w.])")
_KEYWORD_CODE_RE = re.compile(
    r"\b(" + "|".join(sorted(_CODE_WORDS)) + r")\s*[-:]?\s*#?\s*(\d+(?:\.\d+)?[a-z]?)\b", re.I
)
_HASH_CODE_RE = re.compile(r"#\s*(\d+)")


def clean_title(title):
    """Drop 'Aug. 24-28:'-style prefixes teachers put on Classroom titles."""
    return _DATE_PREFIX_RE.sub("", title or "").strip()


def title_codes(title):
    """Identifiers inside a title: '1.4', 'lt1.4', 'unit2', '#1'. These beat words."""
    text = (title or "").lower()
    codes = set(_DECIMAL_CODE_RE.findall(text))
    for word, num in _KEYWORD_CODE_RE.findall(text):
        codes.add(f"{word}{num}")
    for num in _HASH_CODE_RE.findall(text):
        codes.add(f"#{num}")
    return codes


def _title_words(title):
    words = [
        t for t in _tokens(clean_title(title), drop_noise=True)
        if not t.isdigit() and t not in _CODE_WORDS
        # "1a", "lt2", "a2": short code-like tokens, not words
        and not re.fullmatch(r"\d+[a-z]|[a-z]{1,3}\d+[a-z]?", t)
    ]
    return set(words)


def title_similarity(a, b):
    """0..1 similarity of two assignment titles.

    Words are compared as sets; codes ('LT 1.4', '#2') must agree when both
    titles have them, and agreeing codes lift a partial word match.
    """
    ca, cb = title_codes(a), title_codes(b)
    codes_agree = bool(ca & cb)
    codes_clash = bool(ca and cb and not codes_agree)

    wa, wb = _title_words(a), _title_words(b)
    if wa and wb:
        inter = wa & wb
        sim = 2 * len(inter) / (len(wa) + len(wb)) if inter else 0.0
        if inter and (wa <= wb or wb <= wa):
            sim = max(sim, 0.75)
    elif not wa and not wb:
        sim = 0.85 if codes_agree else 0.0
    else:
        # One side is only a code ("LT 1.8"); the other has words too.
        sim = 0.85 if codes_agree else 0.0

    if codes_clash:
        sim *= 0.5
    elif codes_agree and wa and wb:
        sim = min(0.95, sim + 0.15)
    return round(sim, 3)


def _allowed_gap_days(sim, codes_agree):
    """Aeries due dates are often batch-entered weeks after Classroom's, so codes buy slack."""
    if codes_agree:
        return 30
    if sim >= 0.8:
        return 7
    return 3


def match_items_to_assignments(items, assignments, today=None):
    """One-to-one match of Classroom items onto Aeries rows by title, then due date."""
    pairs = []
    for i, item in enumerate(items or []):
        if item.get("type") not in ("assignment", "question"):
            continue
        i_due = due_to_pacific_date(item.get("due"))
        for j, a in enumerate(assignments or []):
            sim = title_similarity(item.get("title"), a.get("description"))
            if sim < ITEM_MATCH_THRESHOLD:
                continue
            codes_agree = bool(title_codes(item.get("title")) & title_codes(a.get("description")))
            a_due = _aeries_date(a.get("due_date"))
            gap = abs((i_due - a_due).days) if (i_due and a_due) else None
            if gap is not None and gap > _allowed_gap_days(sim, codes_agree) and sim < 0.9:
                continue
            if gap is None and sim < 0.75:
                continue
            pairs.append((sim, gap if gap is not None else 99, i, j))
    used_i, used_j, out = set(), set(), {}
    for sim, gap, i, j in sorted(pairs, key=lambda p: (-p[0], p[1])):
        if i in used_i or j in used_j:
            continue
        used_i.add(i)
        used_j.add(j)
        out[i] = j
    return out


# --------------------------------------------------------------------------- normalize + attach


MATERIALS_PER_ITEM = 12
RUBRIC_CRITERIA_LIMIT = 12
RUBRIC_TEXT_LIMIT = 300
HISTORY_LIMIT = 12
DRIVE_FILES_PER_FOLDER = 80
# Attachment fields the script exports that we keep verbatim (v2 export shape).
_MATERIAL_PASSTHROUGH = ("id", "mime", "share_mode", "modified_at", "owned_by_student")


def _compact_materials(materials, names):
    out = []
    for m in materials or []:
        title = strip_student_name(m.get("title"), names)
        entry = {"kind": m.get("kind") or "link", "title": title, "url": m.get("url") or ""}
        for key in _MATERIAL_PASSTHROUGH:
            if m.get(key) not in (None, "", False):
                entry[key] = m[key]
        if m.get("note"):
            entry["note"] = _clip(m["note"], 120)
        text = (m.get("text_excerpt") or "").strip()
        if text:
            entry["text_excerpt"] = _clip(text, MATERIAL_TEXT_LIMIT)
        out.append(entry)
    return out[:MATERIALS_PER_ITEM]


def compact_rubric(raw):
    """Criteria and levels only; ids and spreadsheet references are dropped."""
    if not isinstance(raw, dict):
        return None
    criteria = []
    for c in (raw.get("criteria") or [])[:RUBRIC_CRITERIA_LIMIT]:
        levels = [
            {
                "title": (lv.get("title") or "").strip(),
                "description": _clip(lv.get("description"), RUBRIC_TEXT_LIMIT),
                "points": lv.get("points"),
            }
            for lv in c.get("levels") or []
        ]
        points = [lv["points"] for lv in levels if isinstance(lv.get("points"), (int, float))]
        criteria.append({
            "title": (c.get("title") or "").strip(),
            "description": _clip(c.get("description"), RUBRIC_TEXT_LIMIT),
            "max_points": max(points) if points else None,
            "levels": levels,
        })
    if not criteria:
        return None
    totals = [c["max_points"] for c in criteria if c["max_points"] is not None]
    return {"criteria": criteria, "max_points": sum(totals) if totals else None}


def _compact_history(history):
    out = []
    for h in history or []:
        if not isinstance(h, dict):
            continue
        entry = {"kind": h.get("kind") or "", "on": _iso_day(h.get("at")), "at": h.get("at") or ""}
        if h.get("kind") == "grade":
            entry["points_earned"] = h.get("points_earned")
            entry["max_points"] = h.get("max_points")
            entry["change"] = h.get("change") or ""
        else:
            entry["state"] = (h.get("state") or "").upper()
        out.append(entry)
    return out[-HISTORY_LIMIT:]


def _compact_grade_category(raw):
    if not isinstance(raw, dict) or not (raw.get("name") or "").strip():
        return None
    return {
        "name": raw["name"].strip(),
        "weight": raw.get("weight"),
        "default_denominator": raw.get("default_denominator"),
    }


def _compact_teachers(raw):
    out = []
    for t in raw or []:
        if not isinstance(t, dict):
            continue
        name = (t.get("name") or "").strip()
        if not name:
            continue
        out.append({"name": name, "is_owner": bool(t.get("is_owner"))})
    return out


def compact_drive_folders(raw, names):
    """The Drive Classroom/ index: enough to find and re-read a file later, no student names."""
    out = []
    for folder in raw or []:
        if not isinstance(folder, dict):
            continue
        files = []
        for f in (folder.get("files") or [])[:DRIVE_FILES_PER_FOLDER]:
            if not isinstance(f, dict):
                continue
            entry = {
                "id": f.get("id") or "",
                "title": strip_student_name(f.get("title"), names),
                "mime": f.get("mime") or "",
                "url": f.get("url") or "",
                "modified_at": f.get("modified_at") or "",
                "owned_by_student": bool(f.get("owned_by_student")),
            }
            files.append(entry)
        out.append({
            "id": folder.get("id") or "",
            "name": strip_student_name(folder.get("name"), names),
            "url": folder.get("url") or "",
            "created_at": folder.get("created_at") or "",
            "current_year": folder.get("current_year"),
            "files": files,
        })
    return out


def _drive_text_stats(courses):
    """(drive files, with text, flagged unreadable) across every item and submission."""
    total = with_text = unreadable = 0
    for course in courses or []:
        for item in course.get("items") or []:
            mats = list(item.get("materials") or [])
            mats += list(((item.get("submission") or {}).get("attachments")) or [])
            for m in mats:
                if m.get("kind") != "drive":
                    continue
                total += 1
                if m.get("text_excerpt"):
                    with_text += 1
                if m.get("note"):
                    unreadable += 1
    return total, with_text, unreadable


def instructions_for(item):
    """Instructions text: the description first, then the first readable teacher doc."""
    parts = []
    desc = (item.get("description") or "").strip()
    if desc:
        parts.append(desc)
    for m in item.get("materials") or []:
        text = (m.get("text_excerpt") or "").strip()
        if text:
            parts.append(text)
            break
    return _clip("\n\n".join(parts), INSTRUCTIONS_LIMIT)


def normalize_item(raw, names, today):
    sub = raw.get("submission") or {}
    state = (sub.get("state") or "").upper()
    due_date = due_to_pacific_date(raw.get("due"))
    item = {
        "id": str(raw.get("id") or ""),
        "type": raw.get("type") or "assignment",
        "title": strip_student_name(raw.get("title"), names),
        "topic": (raw.get("topic") or "").strip(),
        "link": raw.get("link") or "",
        "due": due_date.isoformat() if due_date else "",
        "due_date": _to_aeries_date(due_date),
        "assigned_on": _iso_day(raw.get("assigned_at")),
        "updated_on": _iso_day(raw.get("updated_at")),
        "max_points": raw.get("max_points"),
        "description": _clip(raw.get("description"), EXCERPT_LIMIT),
        "instructions": instructions_for(raw),
        "materials": _compact_materials(raw.get("materials"), names),
        "aeries_match": None,
    }
    # v2 export fields: kept as-is when present, absent otherwise, so v1 exports
    # and fixtures keep working.
    for key in ("work_type", "state", "assignee_mode", "submission_modification_mode"):
        if raw.get(key):
            item[key] = raw[key]
    if raw.get("scheduled_at"):
        item["scheduled_on"] = _iso_day(raw["scheduled_at"])
    category = _compact_grade_category(raw.get("grade_category"))
    if category:
        item["grade_category"] = category
    if raw.get("choices"):
        item["choices"] = [str(c) for c in raw["choices"]][:12]
    rubric = compact_rubric(raw.get("rubric"))
    if rubric:
        item["rubric"] = rubric
    if due_date and today is not None:
        item["days_until_due"] = (due_date - today.date()).days
    if sub:
        item["submission"] = {
            "state": state,
            "state_label": STATE_LABELS.get(state, state.replace("_", " ").title()),
            "late": bool(sub.get("late")),
            "turned_in_on": _iso_day(sub.get("turned_in_at")),
            "assigned_grade": sub.get("assigned_grade"),
            "attachments": _compact_materials(sub.get("attachments"), names),
        }
        if sub.get("draft_grade") is not None:
            item["submission"]["draft_grade"] = sub["draft_grade"]
        if sub.get("answer") not in (None, ""):
            item["submission"]["answer"] = _clip(str(sub["answer"]), EXCERPT_LIMIT)
        if sub.get("history"):
            item["submission"]["history"] = _compact_history(sub["history"])
        if sub.get("updated_at"):
            item["submission"]["updated_on"] = _iso_day(sub["updated_at"])
    return item


def _turned_in(item):
    return ((item.get("submission") or {}).get("state") or "") in TURNED_IN_STATES


def _stamp_material(m):
    """Assignment-row material: title, link, and a short Doc/Slides excerpt when we have one."""
    if not isinstance(m, dict):
        return None
    entry = {
        "kind": m.get("kind") or "link",
        "title": m.get("title") or "",
        "url": m.get("url") or "",
    }
    if m.get("mime"):
        entry["mime"] = m["mime"]
    if m.get("text_excerpt"):
        entry["text_excerpt"] = _clip(m["text_excerpt"], STAMP_TEXT_LIMIT)
    if m.get("note"):
        entry["note"] = _clip(m["note"], 120)
    if m.get("owned_by_student"):
        entry["owned_by_student"] = True
    if not entry["title"] and not entry["url"] and not entry.get("text_excerpt"):
        return None
    return entry


def _stamp_rubric(rubric):
    """Criteria with levels so the parent UI can show how the teacher scores it."""
    if not isinstance(rubric, dict):
        return []
    out = []
    for c in (rubric.get("criteria") or [])[:8]:
        entry = {
            "title": c.get("title") or "",
            "max_points": c.get("max_points"),
        }
        if c.get("description"):
            entry["description"] = c["description"]
        levels = []
        for lv in c.get("levels") or []:
            level = {"title": lv.get("title") or "", "points": lv.get("points")}
            if lv.get("description"):
                level["description"] = lv["description"]
            levels.append(level)
        if levels:
            entry["levels"] = levels
        out.append(entry)
    return out


def _assignment_link_payload(item):
    sub = item.get("submission") or {}
    payload = {
        "id": item.get("id"),
        "link": item.get("link") or "",
        "state": sub.get("state") or "",
        "state_label": sub.get("state_label") or "",
        "late": bool(sub.get("late")),
        "turned_in_on": sub.get("turned_in_on") or "",
        "due": item.get("due") or "",
        "topic": item.get("topic") or "",
        "max_points": item.get("max_points"),
        "instructions": item.get("instructions") or "",
        "materials": [m for m in (_stamp_material(x) for x in (item.get("materials") or [])[:8]) if m],
        "grade_category": (item.get("grade_category") or {}).get("name") or "",
        "rubric": _stamp_rubric(item.get("rubric")),
    }
    weight = (item.get("grade_category") or {}).get("weight")
    if weight is not None:
        payload["grade_category_weight"] = weight
    if sub.get("assigned_grade") is not None:
        payload["assigned_grade"] = sub["assigned_grade"]
    if sub.get("draft_grade") is not None:
        payload["draft_grade"] = sub["draft_grade"]
    if sub.get("answer"):
        payload["answer"] = sub["answer"]
    history = sub.get("history") or []
    if history:
        payload["history"] = history[-8:]
    attachments = [m for m in (_stamp_material(x) for x in (sub.get("attachments") or [])[:6]) if m]
    if attachments:
        payload["attachments"] = attachments
    return {k: v for k, v in payload.items() if v not in ("", None, [], False) or k in ("state", "late")}


def attach_classroom(student_data, export, today, assignments_for_class, overrides=None):
    """Normalize one export onto ``student_data``.

    Sets ``student_data["classroom"]`` and stamps a compact ``classroom`` dict on
    every Aeries assignment row that matches a Classroom item. Returns the
    normalized block. ``assignments_for_class`` is scraper.assignments_for_class.
    """
    names = _student_first_names()
    classes = student_data.get("classes") or []
    courses_raw = export.get("courses") or []
    mapping = map_courses_to_classes(courses_raw, classes, overrides=overrides or load_course_overrides())
    bounds = school_year_bounds(today)
    year_id, year_start, year_end = bounds if bounds else ("", None, None)
    dropped_items = 0

    # Clear stale stamps from a previous run before re-matching.
    for group in student_data.get("assignments_by_class") or []:
        for a in group.get("assignments") or []:
            a.pop("classroom", None)

    courses_out = []
    unmatched = []
    matched_items = 0
    for raw in courses_raw:
        cid = str(raw.get("id") or "")
        cm = mapping.get(cid)
        items = [normalize_item(it, names, today) for it in raw.get("items") or []]
        if year_start and year_end:
            kept = [it for it in items if item_in_school_year(it, year_start, year_end)]
            dropped_items += len(items) - len(kept)
            items = kept
        items.sort(key=lambda it: (it.get("due") or "9999", it.get("updated_on") or ""), reverse=False)
        course = {
            "id": cid,
            "name": strip_student_name(raw.get("name"), names),
            "section": (raw.get("section") or "").strip(),
            "link": raw.get("link") or "",
            "aeries_period": _as_int(cm.get("period")) if cm else None,
            "aeries_course_name": (cm.get("course_name") or "").strip() if cm else "",
            "items": items,
        }
        # v2 export fields, passed through when the script sent them.
        if raw.get("room"):
            course["room"] = str(raw["room"]).strip()
        if raw.get("description_heading"):
            course["description_heading"] = _clip(raw["description_heading"], 200)
        if raw.get("description"):
            course["description"] = _clip(raw["description"], EXCERPT_LIMIT)
        teachers = _compact_teachers(raw.get("teachers"))
        if teachers:
            course["teachers"] = teachers
        topics = [
            {"id": str(t.get("id") or ""), "name": (t.get("name") or "").strip()}
            for t in raw.get("topics") or []
            if isinstance(t, dict) and (t.get("name") or "").strip()
        ]
        if topics:
            course["topics"] = topics[:40]
        if cm:
            assignments = assignments_for_class(student_data, cm)
            for i, j in match_items_to_assignments(items, assignments, today).items():
                items[i]["aeries_match"] = {
                    "number": assignments[j].get("number"),
                    "description": assignments[j].get("description") or "",
                }
                assignments[j]["classroom"] = _assignment_link_payload(items[i])
                matched_items += 1
        else:
            unmatched.append(course["name"])
        courses_out.append(course)

    block = {
        "source": export.get("source") or "classroom_apps_script",
        "export_version": export.get("export_version"),
        "script_version": export.get("script_version") or "",
        "captured_at": export.get("captured_at") or "",
        "courses": courses_out,
        "unmatched_courses": unmatched,
        "notes": [_clip(n, 200) for n in export.get("notes") or []][:10],
    }
    if year_id:
        block["school_year"] = year_id
        block["school_year_start"] = year_start.isoformat()
        block["school_year_end"] = year_end.isoformat()
    drive_folders = compact_drive_folders(export.get("drive_folders"), names)
    if year_start and year_end:
        before = len(drive_folders)
        drive_folders = [f for f in drive_folders if drive_folder_in_year(f, year_start, year_end)]
        dropped_folders = before - len(drive_folders)
    else:
        dropped_folders = 0
    if drive_folders:
        block["drive_folders"] = drive_folders
    doc_text = export.get("doc_text")
    if isinstance(doc_text, dict):
        block["doc_text"] = {
            k: doc_text.get(k)
            for k in ("total", "with_text", "reused", "failed", "skipped_time_budget")
            if doc_text.get(k) is not None
        }
    student_data["classroom"] = block
    print(
        f"  Classroom: {len(courses_out)} courses ({len(courses_out) - len(unmatched)} mapped to Aeries), "
        f"{matched_items} items matched to gradebook rows"
        + (f", script v{block['script_version']}" if block["script_version"] else "")
        + (f", year {year_id}" if year_id else "")
    )
    if dropped_items or dropped_folders:
        print(
            f"  Classroom: dropped {dropped_items} items and {dropped_folders} Drive folders "
            f"outside the {year_id or 'current'} school year"
        )
    if unmatched:
        print(f"  Classroom: unmapped courses: {', '.join(unmatched)}")
    total, with_text, unreadable = _drive_text_stats(courses_out)
    print(f"  Classroom: {total} Drive files on items, {with_text} with text, {unreadable} flagged unreadable")
    if block.get("doc_text"):
        dt = block["doc_text"]
        print(
            f"  Classroom: script read text for {dt.get('with_text', 0)} of {dt.get('total', 0)} files "
            f"({dt.get('reused', 0)} reused, {dt.get('skipped_time_budget', 0)} skipped for time, "
            f"{dt.get('failed', 0)} failed)"
        )
    if drive_folders:
        n_files = sum(len(f.get("files") or []) for f in drive_folders)
        print(f"  Classroom: Drive index has {len(drive_folders)} class folders, {n_files} files")
    for note in block["notes"][:3]:
        print(f"  Classroom: export note: {_clip(note, 160)}")
    return block


# --------------------------------------------------------------------------- per-class context


def _course_for_class(block, class_meta):
    period = _as_int((class_meta or {}).get("period"))
    name_n = _norm((class_meta or {}).get("course_name"))
    for course in (block or {}).get("courses") or []:
        if period is not None and course.get("aeries_period") == period:
            return course
        if name_n and _norm(course.get("aeries_course_name")) == name_n:
            return course
    return None


def class_context(student_data, class_meta, today, assignments=None):
    """Dashboard/Grok facts for one Aeries class from its mapped Classroom course."""
    block = (student_data or {}).get("classroom") or {}
    course = _course_for_class(block, class_meta)
    if not course:
        return None

    today_d = today.date() if hasattr(today, "date") else today
    matched_by_number = {}
    for a in assignments or []:
        c = a.get("classroom")
        if c and c.get("id"):
            matched_by_number[str(c["id"])] = a

    classroom_only = []
    turned_in_aeries_missing = []
    not_started_due_soon = []
    late_count = 0
    announcements = []
    materials = []
    current_topic = ""
    for item in sorted(
        course.get("items") or [],
        key=lambda it: it.get("updated_on") or it.get("assigned_on") or "",
        reverse=True,
    ):
        if item.get("topic") and not current_topic:
            current_topic = item["topic"]
    for item in course.get("items") or []:
        kind = item.get("type")
        if kind == "announcement":
            if len(announcements) < 5 and item.get("description"):
                announcements.append({
                    "text": _clip(item["description"], ANNOUNCEMENT_LIMIT),
                    "posted_on": item.get("updated_on") or item.get("assigned_on") or "",
                    "link": item.get("link") or "",
                })
            continue
        if kind == "material":
            if len(materials) < 8:
                excerpt = ""
                for m in item.get("materials") or []:
                    if m.get("text_excerpt"):
                        excerpt = _clip(m["text_excerpt"], STAMP_TEXT_LIMIT)
                        break
                entry = {
                    "title": item.get("title") or "",
                    "topic": item.get("topic") or "",
                    "link": item.get("link") or "",
                    "posted_on": item.get("updated_on") or item.get("assigned_on") or "",
                    "materials": [
                        m for m in (_stamp_material(x) for x in (item.get("materials") or [])[:4]) if m
                    ],
                }
                if excerpt:
                    entry["excerpt"] = excerpt
                materials.append(entry)
            continue

        sub = item.get("submission") or {}
        days = item.get("days_until_due")
        turned_in = _turned_in(item)
        if turned_in and sub.get("late"):
            late_count += 1

        aeries_row = matched_by_number.get(str(item.get("id")))
        if aeries_row is not None:
            if (
                turned_in
                and aeries_row.get("points_earned") is None
                and (aeries_row.get("aeries_missing") or aeries_row.get("status") == "missing")
            ):
                turned_in_aeries_missing.append({
                    "title": item.get("title"),
                    "turned_in_on": sub.get("turned_in_on") or "",
                    "link": item.get("link") or "",
                })
            continue

        if turned_in:
            continue
        due_d = due_to_pacific_date(item.get("due"))
        assigned_d = None
        if item.get("assigned_on"):
            try:
                assigned_d = datetime.strptime(item["assigned_on"], "%Y-%m-%d").date()
            except ValueError:
                assigned_d = None
        in_window = False
        if due_d is not None:
            in_window = -RECENT_WINDOW_DAYS <= (due_d - today_d).days <= UPCOMING_WINDOW_DAYS
        elif assigned_d is not None:
            in_window = 0 <= (today_d - assigned_d).days <= UPCOMING_WINDOW_DAYS
        if not in_window:
            continue
        entry = {
            "id": item.get("id"),
            "title": item.get("title"),
            "type": kind,
            "due": item.get("due") or "",
            "due_date": item.get("due_date") or "",
            "days_until_due": days,
            "state": sub.get("state") or "",
            "state_label": sub.get("state_label") or "",
            "link": item.get("link") or "",
            "topic": item.get("topic") or "",
            "max_points": item.get("max_points"),
            "instructions": _clip(item.get("instructions"), EXCERPT_LIMIT),
        }
        if item.get("rubric"):
            entry["rubric"] = _stamp_rubric(item["rubric"])
        if item.get("grade_category"):
            entry["grade_category"] = item["grade_category"].get("name") or ""
            if item["grade_category"].get("weight") is not None:
                entry["grade_category_weight"] = item["grade_category"]["weight"]
        stamped_mats = [m for m in (_stamp_material(x) for x in (item.get("materials") or [])[:8]) if m]
        if stamped_mats:
            entry["materials"] = stamped_mats
        classroom_only.append(entry)
        if days is not None and 0 <= days <= 2 and (sub.get("state") or "") in NOT_STARTED_STATES:
            not_started_due_soon.append({"title": item.get("title"), "days_until_due": days})

    classroom_only.sort(key=lambda e: (e.get("due") or "9999", e.get("title") or ""))
    topics = [t.get("name") for t in course.get("topics") or [] if t.get("name")]
    drive = _drive_for_course(block, course)
    out = {
        "course_name": course.get("name") or "",
        "link": course.get("link") or "",
        "teachers": [t.get("name") for t in course.get("teachers") or []],
        "captured_at": block.get("captured_at") or "",
        "school_year": block.get("school_year") or "",
        "classroom_only": classroom_only[:12],
        "turned_in_aeries_missing": turned_in_aeries_missing,
        "not_started_due_soon": not_started_due_soon,
        "late_turn_ins": late_count,
        "announcements": announcements,
        "materials": materials,
    }
    if course.get("room"):
        out["room"] = course["room"]
    if course.get("description"):
        out["description"] = course["description"]
    if course.get("description_heading"):
        out["description_heading"] = course["description_heading"]
    if topics:
        out["topics"] = topics
    if current_topic:
        out["current_topic"] = current_topic
    if drive:
        out["drive"] = drive
    return out


def _drive_for_course(block, course):
    """Current-year Drive class folder that matches this Classroom course."""
    folders = (block or {}).get("drive_folders") or []
    if not folders:
        return None
    name_n = _norm(course.get("name"))
    match = None
    for folder in folders:
        if name_n and _norm(folder.get("name")) == name_n:
            match = folder
            break
    if match is None:
        current = [f for f in folders if f.get("current_year")]
        match = current[0] if len(current) == 1 else None
    if match is None:
        return None
    files = []
    for f in (match.get("files") or [])[:12]:
        title = (f.get("title") or "").strip()
        if not title:
            continue
        entry = {
            "title": title,
            "url": f.get("url") or "",
            "mime": f.get("mime") or "",
            "owned_by_student": bool(f.get("owned_by_student")),
        }
        if f.get("modified_at"):
            entry["modified_on"] = _iso_day(f["modified_at"])
        files.append(entry)
    return {
        "name": match.get("name") or "",
        "url": match.get("url") or "",
        "files": files,
    }


def grok_context(context):
    """Trimmed copy of class_context for the Grok analytics payload (no links)."""
    if not context:
        return None
    return {
        "classroom_only_upcoming": [
            {
                "title": e.get("title"),
                "due_date": e.get("due_date"),
                "days_until_due": e.get("days_until_due"),
                "state_label": e.get("state_label"),
                "instructions": _clip(e.get("instructions"), 300),
                **({"rubric_criteria": [c.get("title") for c in e["rubric"]][:6]} if e.get("rubric") else {}),
            }
            for e in context.get("classroom_only") or []
        ][:6],
        "turned_in_on_classroom_but_aeries_missing": [
            e.get("title") for e in context.get("turned_in_aeries_missing") or []
        ],
        "not_started_due_soon": context.get("not_started_due_soon") or [],
        "late_turn_ins": context.get("late_turn_ins") or 0,
        "recent_announcements": [a.get("text") for a in context.get("announcements") or []],
    }


def pseudo_assignments(context):
    """Classroom-only work due soon, shaped like Aeries rows for the tonight plan."""
    out = []
    for e in (context or {}).get("classroom_only") or []:
        if not e.get("due_date"):
            continue
        out.append({
            "description": e.get("title") or "",
            "category": "Classroom",
            "due_date": e.get("due_date"),
            "points_possible": e.get("max_points"),
            "points_earned": None,
            "source": "classroom",
            "classroom": {
                "id": e.get("id"),
                "link": e.get("link"),
                "state": e.get("state"),
                "state_label": e.get("state_label"),
                "instructions": e.get("instructions"),
            },
        })
    return out
