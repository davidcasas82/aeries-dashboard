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
ANNOUNCEMENT_LIMIT = 240
UPCOMING_WINDOW_DAYS = 14
RECENT_WINDOW_DAYS = 7

_STOP_TOKENS = {"the", "of", "and", "a", "an", "to", "for", "in", "on", "with", "&", "-"}
_TITLE_NOISE = {"hw", "homework", "assignment", "worksheet", "ws", "pg", "pgs", "p", "pp", "due"}


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

    period = _as_int(class_meta.get("period"))
    cp = classroom_period(course)
    if period is not None and cp is not None:
        score += 2 if cp == period else -2

    a_toks = [t for t in _tokens(class_meta.get("course_name")) if not t.isdigit() or len(t) > 1]
    c_toks = set(_tokens(text))
    if a_toks:
        hits = 0
        for t in a_toks:
            if t in c_toks:
                hits += 1
                continue
            if len(t) >= 3 and any(
                (c.startswith(t) or t.startswith(c)) and min(len(c), len(t)) >= 3 for c in c_toks
            ):
                hits += 0.7
        score += 2.5 * hits / len(a_toks)
    return round(score, 2)


def map_courses_to_classes(courses, classes, overrides=None):
    """Return {course_id: class_meta} using overrides (course_id -> period) then scoring."""
    overrides = overrides or {}
    by_period = {}
    for c in classes or []:
        p = _as_int(c.get("period"))
        if p is not None and (c.get("course_name") or "").strip():
            by_period.setdefault(p, c)

    mapping = {}
    taken = set()
    candidates = []
    for course in courses or []:
        cid = str(course.get("id") or "")
        forced = overrides.get(cid)
        if forced is not None and _as_int(forced) in by_period:
            mapping[cid] = by_period[_as_int(forced)]
            taken.add(id(mapping[cid]))
            continue
        for cm in classes or []:
            if not (cm.get("course_name") or "").strip():
                continue
            s = score_course_match(course, cm)
            if s >= 2.5:
                candidates.append((s, cid, cm))
    for s, cid, cm in sorted(candidates, key=lambda x: -x[0]):
        if cid in mapping or id(cm) in taken:
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


def title_similarity(a, b):
    ta, tb = set(_tokens(a, drop_noise=True)), set(_tokens(b, drop_noise=True))
    if not ta or not tb:
        ta, tb = set(_tokens(a)), set(_tokens(b))
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    if not inter:
        return 0.0
    if ta <= tb or tb <= ta:
        return max(0.75, 2 * len(inter) / (len(ta) + len(tb)))
    return 2 * len(inter) / (len(ta) + len(tb))


def match_items_to_assignments(items, assignments, today=None):
    """One-to-one match of Classroom items onto Aeries rows by title, then due date."""
    pairs = []
    for i, item in enumerate(items or []):
        if item.get("type") not in ("assignment", "question"):
            continue
        i_due = due_to_pacific_date(item.get("due"))
        for j, a in enumerate(assignments or []):
            sim = title_similarity(item.get("title"), a.get("description"))
            if sim < 0.6:
                continue
            a_due = _aeries_date(a.get("due_date"))
            gap = abs((i_due - a_due).days) if (i_due and a_due) else None
            if gap is not None and gap > 3 and sim < 0.9:
                continue
            if gap is None and sim < 0.75:
                continue
            pairs.append((sim, -(gap or 0), i, j))
    used_i, used_j, out = set(), set(), {}
    for sim, _neg_gap, i, j in sorted(pairs, key=lambda p: (-p[0], p[1])):
        if i in used_i or j in used_j:
            continue
        used_i.add(i)
        used_j.add(j)
        out[i] = j
    return out


# --------------------------------------------------------------------------- normalize + attach


def _compact_materials(materials, names):
    out = []
    for m in materials or []:
        title = strip_student_name(m.get("title"), names)
        entry = {"kind": m.get("kind") or "link", "title": title, "url": m.get("url") or ""}
        text = (m.get("text_excerpt") or "").strip()
        if text:
            entry["text_excerpt"] = _clip(text, MATERIAL_TEXT_LIMIT)
        out.append(entry)
    return out[:8]


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
    return item


def _turned_in(item):
    return ((item.get("submission") or {}).get("state") or "") in TURNED_IN_STATES


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
        "materials": [
            {"kind": m.get("kind"), "title": m.get("title"), "url": m.get("url")}
            for m in item.get("materials") or []
        ][:5],
    }
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
        "captured_at": export.get("captured_at") or "",
        "courses": courses_out,
        "unmatched_courses": unmatched,
        "notes": list(export.get("notes") or [])[:10],
    }
    student_data["classroom"] = block
    print(
        f"  Classroom: {len(courses_out)} courses ({len(courses_out) - len(unmatched)} mapped to Aeries), "
        f"{matched_items} items matched to gradebook rows"
    )
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
    for item in course.get("items") or []:
        kind = item.get("type")
        if kind == "announcement":
            if len(announcements) < 3 and item.get("description"):
                announcements.append({
                    "text": _clip(item["description"], ANNOUNCEMENT_LIMIT),
                    "posted_on": item.get("updated_on") or item.get("assigned_on") or "",
                    "link": item.get("link") or "",
                })
            continue
        if kind == "material":
            if len(materials) < 5:
                materials.append({
                    "title": item.get("title") or "",
                    "topic": item.get("topic") or "",
                    "link": item.get("link") or "",
                    "posted_on": item.get("updated_on") or item.get("assigned_on") or "",
                })
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
        classroom_only.append(entry)
        if days is not None and 0 <= days <= 2 and (sub.get("state") or "") in NOT_STARTED_STATES:
            not_started_due_soon.append({"title": item.get("title"), "days_until_due": days})

    classroom_only.sort(key=lambda e: (e.get("due") or "9999", e.get("title") or ""))
    return {
        "course_name": course.get("name") or "",
        "link": course.get("link") or "",
        "captured_at": block.get("captured_at") or "",
        "classroom_only": classroom_only[:12],
        "turned_in_aeries_missing": turned_in_aeries_missing,
        "not_started_due_soon": not_started_due_soon,
        "late_turn_ins": late_count,
        "announcements": announcements,
        "materials": materials,
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
