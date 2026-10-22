"""Grade-insights runner for the Mac mini. launchd starts it a few minutes after each grade sync.

A start checks that a fresh sync landed (the public freshness stamp), then reads the grades once
(one unlock, GETs only) and fingerprints what the engine uses, per student. When every fingerprint
matches the last successful run it logs "skip: no change" and stops: no model, no PUT, the stored
notes stay. Otherwise the writer (WriterWork) writes each student's write-up and publishes them
through InsightsPublisher, which sends only with GRADE_INSIGHTS_PUBLISH=1. The fingerprint is saved
only after a publish or a deliberate no-op, so a failed run tries again on the next start.
"""

import fcntl
import hashlib
import importlib
import json
import os
import plistlib
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from class_match import assignments_for_class


PACIFIC = ZoneInfo("America/Los_Angeles")
FAMILY_DATA_URL = "https://family-data.davidcasas.workers.dev"
FRESHNESS_URL = f"{FAMILY_DATA_URL}/v1/grades/freshness"
USER_AGENT = "grade-insights-runner/1"
# A sync is fresh for this long after it lands. launchd starts the runner about 10 and 40 minutes after
# the 4:45 PM and 8:07 PM PT scrapes (deploy/com.davidcasas.grade-insights.plist), so the 2 AM scrapes
# are always older than this by then and never start a run.
FRESH_FOR = timedelta(hours=2)
CLOCK_SKEW = timedelta(minutes=5)
# How a start that reached the work can end. Each of these marks the sync handled; anything else
# (waiting for the machine, a crash) leaves it for the next start.
HANDLED = ("no_change", "published", "nothing_to_send", "publish_off")
# The parent PIN is a generic password in the mini's login Keychain.
PIN_KEYCHAIN_SERVICE = "family-data-parent-pin"
PIN_KEYCHAIN_ACCOUNT = "aeries"
# The insights write secret (family-data AERIES_INSIGHTS_SECRET) is a second generic password.
# It can write the insights record and nothing else; the runner never prints or logs it.
INSIGHTS_KEYCHAIN_SERVICE = "family-data-insights-write"
INSIGHTS_KEYCHAIN_ACCOUNT = "aeries"
INSIGHTS_PATH = "/v1/grades/insights"
PUBLISH_FLAG = "GRADE_INSIGHTS_PUBLISH"
SSD_VOLUME_NAME = "David C SSD"
DEFAULT_SSD = f"/Volumes/{SSD_VOLUME_NAME}"
# Installed on the SSD with mlx-lm 0.32.2. Smoke-test peaks: Qwen 5.2 GB at 32 tok/s, Gemma 8.1 GB at 20 tok/s.
MODEL_REPOS = {
    "qwen3-8b": "mlx-community/Qwen3-8B-4bit",
    "gemma3-12b": "mlx-community/gemma-3-text-12b-it-4bit",
}
# The bake-off's pick (qwen-v11: 30/30 stories) and its settings.
WRITER_MODEL = "qwen3-8b"
MIN_FREE_MEMORY_PCT = 35
BUSY_PROCESSES = ("Blender", "blender", "ffmpeg")
# The engine and writer sources go into every fingerprint, so a code update writes the notes again once.
FINGERPRINT_CODE = ("insights.py", "insights_writer.py")


@dataclass(frozen=True)
class Machine:
    """What the runner reads from the box it runs on. Tests pass fakes."""

    now: Callable[[], datetime]
    freshness: Callable[[], str]
    ssd_mounted: Callable[[], bool]
    free_memory_pct: Callable[[], int]
    busy: Callable[[], bool]


def fresh_sync(stamp, now, handled=None):
    """True when stamp is a sync this runner hasn't handled yet and it landed within FRESH_FOR of now."""
    if not isinstance(stamp, str) or not stamp or stamp == handled:
        return False
    try:
        landed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if landed.tzinfo is None:
        return False
    return -CLOCK_SKEW <= now - landed <= FRESH_FOR


def machine_free(machine):
    """No render running and enough free memory for the model."""
    return not machine.busy() and machine.free_memory_pct() >= MIN_FREE_MEMORY_PCT


class State:
    """The runner's small state file: the last sync handled and how the last start ended. No grades."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def mark(self, **fields):
        self.data.update(fields)
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.replace(tmp, self.path)


def tick(state, machine, run_sync, log=print):
    """One launchd start. run_sync(stamp) does the work for a fresh sync and returns how it ended.

    A sync that isn't fresh, or was already handled, ends the start quietly. A missing SSD skips the
    sync with one line and keeps the last good write-up. A busy machine waits for the next start.
    """
    stamp = machine.freshness()
    if not fresh_sync(stamp, machine.now(), state.get("handled")):
        return "waiting_for_sync"
    if not machine.ssd_mounted():
        state.mark(handled=stamp, status="skipped_no_ssd")
        log("grade-insights: sync skipped, the SSD is not mounted; the last write-up stays")
        return "skipped_no_ssd"
    if not machine_free(machine):
        state.mark(status="waiting_for_machine")
        return "waiting_for_machine"
    state.mark(stamp=stamp, status="running")
    status = run_sync(stamp)
    if status in HANDLED:
        state.mark(handled=stamp, status=status)
    else:
        state.mark(status=status)
    return status


# ── The real machine ─────────────────────────────────────────────────────────


def read_freshness(url=FRESHNESS_URL):
    """Stdlib only, so the runner works under the SSD venv (which has no requests)."""
    # Cloudflare answers 403 to urllib's default User-Agent.
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))["updatedAt"]


def free_memory_pct():
    out = subprocess.run(["/usr/bin/memory_pressure"], capture_output=True, text=True, timeout=30).stdout
    for line in out.splitlines():
        if "free percentage" in line:
            return int(line.rsplit(":", 1)[1].strip().rstrip("%"))
    return 0


def render_running():
    for name in BUSY_PROCESSES:
        if subprocess.run(["/usr/bin/pgrep", "-x", name], capture_output=True).returncode == 0:
            return True
    return False


class KeychainError(RuntimeError):
    pass


def keychain_secret(service, account):
    """A generic-password item from the login Keychain. The value is returned, never printed."""
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise KeychainError(f"no Keychain item for service {service!r} (exit {result.returncode})")
    return result.stdout.rstrip("\n")


def parent_pin():
    """The parent PIN from the login Keychain, for ReadOnlyFamilyData's one unlock. Never printed."""
    return keychain_secret(PIN_KEYCHAIN_SERVICE, PIN_KEYCHAIN_ACCOUNT)


class FamilyDataError(RuntimeError):
    """Carries the status and path only, never a PIN or token."""


class ReadOnlyFamilyData:
    """The runner's only family-data client for the PIN session. GET-only by construction.

    The Keychain PIN is the family PIN, so its session has the write role. This client can
    send exactly one non-GET, the unlock POST, and has no put, post, patch or delete methods.
    Publishing uses InsightsPublisher and the separate AERIES_INSIGHTS_SECRET bearer, never this session.
    """

    UNLOCK_PATH = "/auth/unlock"

    def __init__(self, pin=parent_pin, base=FAMILY_DATA_URL, opener=None):
        self._pin = pin
        self._base = base.rstrip("/")
        self._open = opener or _open_request
        self._token = None

    def latest_grades(self):
        return self._get("/v1/grades/latest")

    def scored_log(self):
        return self._get("/v1/docs/aeries/scored-log")

    def _get(self, path):
        if not path.startswith("/v1/"):
            raise FamilyDataError(f"refusing path {path!r}")
        if self._token is None:
            self._unlock()
        status, body = self._send("GET", path, {"Authorization": f"Bearer {self._token}"})
        if status == 401:
            # One fresh session for an expired token; a second 401 falls through to the error below.
            self._token = None
            self._unlock()
            status, body = self._send("GET", path, {"Authorization": f"Bearer {self._token}"})
        if status != 200:
            raise FamilyDataError(f"GET {path} returned {status}")
        return json.loads(body.decode("utf-8")) if body else {}

    def _unlock(self):
        payload = json.dumps({"pin": self._pin(), "keepUnlocked": False}).encode("utf-8")
        status, body = self._send("POST", self.UNLOCK_PATH, {"Content-Type": "application/json"}, payload)
        if status != 200:
            raise FamilyDataError(f"unlock returned {status}")
        token = (json.loads(body.decode("utf-8")) if body else {}).get("token")
        if not token:
            raise FamilyDataError("unlock returned no token")
        self._token = token

    def _send(self, method, path, headers, data=None):
        if method != "GET" and (method, path) != ("POST", self.UNLOCK_PATH):
            raise FamilyDataError(f"read-only client refuses {method} {path}")
        request = urllib.request.Request(
            self._base + path,
            data=data,
            method=method,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json", **headers},
        )
        return self._open(request)


def insights_secret():
    """The insights write secret from the login Keychain. Read only when a publish is sent."""
    return keychain_secret(INSIGHTS_KEYCHAIN_SERVICE, INSIGHTS_KEYCHAIN_ACCOUNT)


class InsightsPublisher:
    """Sends the published write-ups to family-data: one PUT to /v1/grades/insights, nothing else.

    Authenticates with the AERIES_INSIGHTS_SECRET bearer from the Keychain, never the PIN session.
    No redirects are followed. Errors carry the status only.
    """

    def __init__(self, secret=insights_secret, base=FAMILY_DATA_URL, opener=None):
        self._secret = secret
        self._base = base.rstrip("/")
        self._open = opener or _open_request

    def put(self, write_ups):
        """True after a 200. False, with nothing sent, when no write-up has a student key."""
        doc = insights_doc(write_ups)
        if not doc["students"]:
            return False
        body = insights_body(doc)
        request = urllib.request.Request(
            self._base + INSIGHTS_PATH,
            data=body,
            method="PUT",
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._secret()}",
            },
        )
        status, _ = self._open(request)
        if status != 200:
            raise FamilyDataError(f"PUT {INSIGHTS_PATH} returned {status}")
        return True


# A per-student port of family-data src/insights.js cleanInsights/cleanLine (pinned copy and a real-
# Worker test in tests/fixtures/family_data_insights.js), plus the Worker's 256 KB body cap. The Worker
# rejects the whole body on one bad field; this never does. It drops the bad line, then a student
# left with nothing, then stories until the body fits. Limits are inclusive and lengths are UTF-16
# code units, as in JS.
PUBLISH_MAX_BYTES = 256 * 1024
PUBLISH_MAX_STUDENTS = 8
PUBLISH_MAX_STORIES = 40
PUBLISH_MAX_TEXT = 600
PUBLISH_MAX_CITES = 12
PUBLISH_MAX_CITE = 120
PUBLISH_MAX_CLASS = 120
PUBLISH_MAX_AS_OF = 40
PUBLISH_STORY_TAGS = ("easiest_win", "biggest_risk")
STUDENT_KEY_RE = re.compile(r"[A-Za-z0-9-]{1,32}", re.ASCII)
# String.prototype.trim's set: WhiteSpace (incl. U+FEFF and Zs) and LineTerminator. Not Python's strip().
JS_WHITESPACE = ("\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
                 "\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff")


def _has_surrogate(text):
    """A lone surrogate can't be encoded, so a string holding one never fits."""
    return any("\ud800" <= ch <= "\udfff" for ch in text)


def _js_len(text):
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def _is_text(value, limit):
    """cleanInsights isText: a string, not blank by JS trim, at most `limit` UTF-16 units."""
    return (isinstance(value, str) and not _has_surrogate(value) and value.strip(JS_WHITESPACE) != ""
            and _js_len(value) <= limit)


def _clean_line(value, field):
    """cleanLine, with cites deduped in order and at least one required. None drops the line whole."""
    if not isinstance(value, dict) or not _is_text(value.get(field), PUBLISH_MAX_TEXT):
        return None
    raw = value.get("cites")
    if not isinstance(raw, list):
        return None
    cites = []
    for cite in raw:
        if not _is_text(cite, PUBLISH_MAX_CITE):
            return None
        if cite not in cites:
            cites.append(cite)
    if not cites or len(cites) > PUBLISH_MAX_CITES:
        return None
    return {field: value[field], "cites": cites}


def _clean_student(up):
    """One student as the Worker stores it, or None when nothing of it survives."""
    if not isinstance(up, dict):
        return None
    as_of = up.get("as_of")
    stories = []
    raw = up.get("stories")
    for story in raw if isinstance(raw, list) else []:
        line = _clean_line(story, "story")
        if line and _is_text(story.get("class"), PUBLISH_MAX_CLASS):
            kept = {"class": story["class"], **line}
            # cleanInsights keeps tags from the fixed set, one of each; bad tags go alone, the story stays.
            if _is_tags(story.get("tags")) and story["tags"]:
                kept["tags"] = list(story["tags"])
            stories.append(kept)
    # Sorted (class name, then a stable tie-break) before the story cap, so input order never changes the bytes.
    stories = sorted(stories, key=_story_order)[:PUBLISH_MAX_STORIES]
    _one_of_each_tag(stories)
    if not stories:
        return None
    return {"as_of": as_of if _is_text(as_of, PUBLISH_MAX_AS_OF) else None, "stories": stories}


def _story_order(story):
    """Publish order, the same as insights_writer.story_order (the writer loads lazily, so it isn't imported
    here): class name, then the story's own text, cites and tags as a stable tie-break."""
    return (str(story.get("class")), str(story.get("story")),
            json.dumps(story.get("cites"), sort_keys=True), json.dumps(story.get("tags"), sort_keys=True))


def _one_of_each_tag(stories):
    """At most one easiest_win and one biggest_risk per kid: the first story in publish order keeps each,
    later stories lose it, and a story left with no tag has no tags key (as cleanInsights stores it)."""
    seen = set()
    for story in stories:
        if "tags" not in story:
            continue
        story["tags"] = [tag for tag in story["tags"] if tag not in seen]
        seen.update(story["tags"])
        if not story["tags"]:
            del story["tags"]


def _is_tags(value):
    return (isinstance(value, list) and all(isinstance(tag, str) and tag in PUBLISH_STORY_TAGS for tag in value)
            and len(set(value)) == len(value))


def insights_body(doc):
    """The exact bytes InsightsPublisher sends for `doc`."""
    return json.dumps(doc).encode("utf-8")


def _fit_body(doc):
    """Drop stories until the body is within PUBLISH_MAX_BYTES: always the last story of the largest
    student (ties go to the later student); a student left with no story goes."""
    students = doc["students"]
    size = len(insights_body(doc))
    sizes = {key: len(json.dumps(up)) for key, up in students.items()}
    while size > PUBLISH_MAX_BYTES and students:
        order = list(students)
        key = max(order, key=lambda k: (sizes[k], order.index(k)))
        up = students[key]
        up["stories"].pop()
        if not up["stories"]:
            del students[key], sizes[key]
            size = len(insights_body(doc))
            continue
        new = len(json.dumps(up))
        size += new - sizes[key]
        sizes[key] = new
    if len(insights_body(doc)) > PUBLISH_MAX_BYTES:
        # Can't happen with the drops above; if it ever does, send nothing rather than fail the night.
        students.clear()
    return doc


def insights_doc(write_ups):
    """{"students": {sn: {"as_of", "stories"}}} that the Worker's cleanInsights accepts unchanged.

    A write-up whose student key isn't 1-32 ASCII letters, digits or dashes is skipped, as is one with
    nothing left after its bad lines are dropped. At most 8 students; the body stays under 256 KB.
    """
    students = {}
    past_cap = 0
    for up in write_ups or []:
        sn = (up or {}).get("sn") if isinstance(up, dict) else None
        if isinstance(sn, bool) or not isinstance(sn, (str, int)):
            continue
        sn = str(sn).strip(JS_WHITESPACE)
        if not STUDENT_KEY_RE.fullmatch(sn) or sn in students:
            continue
        clean = _clean_student(up)
        if clean is None:
            continue
        if len(students) == PUBLISH_MAX_STUDENTS:
            past_cap += 1
            continue
        students[sn] = clean
    if past_cap:
        # The count only: no student keys, names or scores.
        print(f"grade-insights: publish keeps {PUBLISH_MAX_STUDENTS} students; {past_cap} more left out",
              file=sys.stderr)
    return _fit_body({"students": students})


def publisher_put(env=None, publisher=None):
    """The publish callable for WriterWork, or None while GRADE_INSIGHTS_PUBLISH is not "1"."""
    if (env if env is not None else os.environ).get(PUBLISH_FLAG) != "1":
        return None
    return (publisher or InsightsPublisher()).put


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect, so the bearer token cannot be resent to another host.

    Returning None makes urllib raise HTTPError with the 3xx status, which _open_request
    turns into a plain status and the client reports as an error.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# The PIN session's opener: plain HTTPS, no redirects.
_NO_REDIRECT_OPENER = urllib.request.build_opener(_RefuseRedirects)


def _open_request(request, opener=None):
    """(status, body bytes). An HTTP error status, including any redirect, comes back as a status."""
    opener = opener or _NO_REDIRECT_OPENER
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, b""


# ── Loading the students ─────────────────────────────────────────────────────


def class_inputs(student):
    """The per-class inputs the scrape gives the facts engine: name, counted_insight, assignments.

    Built from the latest payload the same way the scrape builds them (class_match), so an
    upcoming item lands in the class and bucket its scores would.
    """
    out = []
    for meta in student.get("classes") or []:
        name = (meta.get("course_name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "counted_insight": meta.get("counted_insight"),
            "assignments": assignments_for_class(student, meta),
        })
    return out


WRITER_MODULE = "insights_writer"


def writer_attr(name, log=print):
    """One attribute of the writer (PR #52), or None.

    Quiet only when the writer module truly isn't in this checkout: a ModuleNotFoundError naming
    insights_writer itself. Anything else (a missing name, another missing module, a syntax error,
    any exception while importing) logs one line with the exception type only and returns None,
    so the night still runs without the writer's piece.
    """
    try:
        return getattr(importlib.import_module(WRITER_MODULE), name)
    except ModuleNotFoundError as err:
        if err.name == WRITER_MODULE:
            return None
        failure = type(err).__name__
    except AttributeError:
        failure = "ImportError"
    except Exception as err:  # noqa: BLE001 - a broken writer must never stop the runner
        failure = type(err).__name__
    log(f"grade-insights: warning: writer {name} unavailable ({failure}); 0 upcoming items")
    return None


def writer_upcoming(log=print):
    """The writer's upcoming_items, or None (see writer_attr)."""
    return writer_attr("upcoming_items", log)


def load_students(client, upcoming_items=None, log=print):
    """[{"sn", "insight_facts", "log_items", "upcoming", "fingerprint"}] per student with facts, for the writer.

    Two GETs on the read-only session: the latest grades and the scored log. Students are
    matched to their log by the same key the scrape writes under; nothing here is logged.
    upcoming is [] until the writer provides upcoming_items.
    """
    latest = client.latest_grades() or {}
    latest = latest.get("latest") if isinstance(latest.get("latest"), dict) else latest
    remote_log = client.scored_log() or {}
    log_doc = remote_log.get("doc") if isinstance(remote_log.get("doc"), dict) else {}
    logged = log_doc.get("students") or {}
    build = upcoming_items or writer_upcoming(log)
    code = code_version()
    students = []
    for student in latest.get("students") or []:
        facts = student.get("insight_facts")
        if not isinstance(facts, dict) or not facts.get("facts"):
            continue
        entry = logged.get(str(student.get("sn"))) or {}
        classes = class_inputs(student)
        upcoming = build(classes, facts["as_of"]) if build and facts.get("as_of") else []
        sn, log_items = str(student.get("sn") or ""), entry.get("items") or []
        students.append({"sn": sn, "insight_facts": facts, "log_items": log_items, "upcoming": upcoming,
                         "fingerprint": student_fingerprint(sn, classes, facts, log_items, upcoming, code)})
    return students


def writer_work(client, model, store, put=None, env=None, log=print):
    """The writer's nightly steps fed by load_students, or None without a usable writer.

    Same import rule as writer_attr: quiet when the writer isn't in the checkout, one
    type-only warning when it is there but broken. None means there is no writer to run.
    The load is read once per runner process, so a run unlocks at most once.
    """
    work_class = writer_attr("WriterWork", log)
    if work_class is None:
        return None
    loaded = []

    def load():
        if not loaded:
            loaded.append(load_students(client, log=log))
        return loaded[0]

    return work_class(load, model, store, put=put, env=env)


def model_dir(ssd_path):
    """Models live on the SSD, never on the internal disk."""
    return Path(ssd_path) / "grade-insights"


def venv_python(ssd_path):
    """The SSD venv (Homebrew Python 3.12) that has mlx-lm."""
    return model_dir(ssd_path) / "venv" / "bin" / "python"


def model_link(ssd_path, name):
    """A model's symlink under <SSD>/grade-insights/models/, named after its repo."""
    if name not in MODEL_REPOS:
        raise KeyError(f"unknown model {name!r}")
    return model_dir(ssd_path) / "models" / MODEL_REPOS[name].split("/", 1)[1]


def model_env(ssd_path, base=None):
    """Environment for a model process: the SSD's HF cache, offline, so nothing downloads."""
    env = dict(os.environ if base is None else base)
    env["HF_HOME"] = str(model_dir(ssd_path) / "hf-cache")
    env["HF_HUB_OFFLINE"] = "1"
    return env


def ssd_mounted(ssd_path, volume_name=SSD_VOLUME_NAME):
    """True when ssd_path is a mount point and the volume there is named volume_name.

    The name check keeps a stray folder or a second disk mounted as "David C SSD 1" from passing.
    """
    if not os.path.ismount(ssd_path):
        return False
    try:
        result = subprocess.run(
            ["/usr/sbin/diskutil", "info", "-plist", str(ssd_path)], capture_output=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    try:
        info = plistlib.loads(result.stdout)
    except Exception:
        return False
    return info.get("VolumeName") == volume_name


def real_machine(ssd_path):
    return Machine(
        now=lambda: datetime.now(PACIFIC),
        freshness=read_freshness,
        ssd_mounted=lambda: ssd_mounted(ssd_path),
        free_memory_pct=free_memory_pct,
        busy=render_running,
    )


# ── Change check ─────────────────────────────────────────────────────────────


def code_version(root=None):
    """sha256 over the engine and writer sources, or "" when they aren't there."""
    root = Path(root) if root else Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in FINGERPRINT_CODE:
        path = root / name
        digest.update(name.encode() + b"\0" + (path.read_bytes() if path.exists() else b"") + b"\0")
    return digest.hexdigest()


def student_fingerprint(sn, classes, insight_facts, log_items, upcoming, code=""):
    """sha256 of what the engine and writer use for one student: the student key, every class's scored and
    unscored assignments, the engine's facts, the scored log and the upcoming items, plus the code version.

    The facts' as_of date is left out, so a new day with nothing new is no change; an item that falls due
    still changes the upcoming list. Only the hash is kept, never what went into it.
    """
    facts = {k: v for k, v in (insight_facts or {}).items() if k != "as_of"}
    material = {"v": 1, "sn": sn, "classes": classes, "facts": facts, "log_items": log_items,
                "upcoming": upcoming, "code": code}
    text = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_fingerprints(path):
    """The sorted per-student hashes from the last successful run, or None (no file, or unreadable)."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    hashes = data.get("students") if isinstance(data, dict) else None
    if not isinstance(hashes, list) or not all(isinstance(h, str) for h in hashes):
        return None
    return sorted(hashes)


def write_fingerprints(path, hashes):
    """Save the hashes on the SSD: mode 600 in a 700 folder, written atomically. Hashes only."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    tmp.touch(mode=0o600)
    os.chmod(tmp, 0o600)
    tmp.write_text(json.dumps({"v": 1, "students": sorted(hashes)}))
    os.replace(tmp, path)


def run_id(stamp):
    """The WriterWork store folder for one sync: its stamp, letters and digits only."""
    return "sync-" + re.sub(r"[^0-9A-Za-z]", "", stamp)


def run_sync(stamp, load, work_class, model, put, machine, store, prints_path, env=None, log=print):
    """One fresh sync: the change check, then either a skip or the write-ups and the publish.

    load() is load_students on the read-only session (one unlock, two GETs). Returns one of HANDLED, or
    "waiting_for_machine" when a render starts or memory runs low between students (the next start picks
    up from WriterWork's saved steps). Logs carry counts and outcomes only: no names, keys or scores.
    """
    students = load()
    hashes = sorted(s["fingerprint"] for s in students)
    last = read_fingerprints(prints_path)
    if hashes == last:
        log("grade-insights: skip: no change")
        return "no_change"
    changed = len(set(hashes) - set(last or []))
    log(f"grade-insights: {len(hashes)} students, {changed} changed; writing")
    run = run_id(stamp)
    work = work_class(lambda: students, model, store, put=put, env=env)
    for step in work.plan(run):
        if not machine_free(machine):
            return "waiting_for_machine"
        work.run_step(run, step)
    # WriterWork saves the publish step's result as <store>/<run>/publish.json.
    published = json.loads((Path(store) / run / "publish.json").read_text()).get("published")
    if published:
        write_fingerprints(prints_path, hashes)
        log("grade-insights: published")
        return "published"
    if put is None:
        log("grade-insights: publish is off, nothing sent")
        return "publish_off"
    # The writer left nothing publishable: nothing was sent, the stored notes stay, and that is final for these grades.
    write_fingerprints(prints_path, hashes)
    log("grade-insights: nothing to send, the stored notes stay")
    return "nothing_to_send"


# ── The model ────────────────────────────────────────────────────────────────


class MlxModel:
    """The writer's Model on the SSD's Qwen3 8B through mlx-lm, with the bake-off's settings.

    Loaded on the first call only, so a start that skips never touches the model. Offline: the SSD's
    HF cache with HF_HUB_OFFLINE=1, nothing downloads. Prompts and replies are never logged.
    """

    MAX_TOKENS = {"draft": 512, "vote": 128}

    def __init__(self, ssd_path, name=WRITER_MODEL):
        self.ssd_path, self.name = ssd_path, name
        self._loaded = None

    def _load(self):
        if self._loaded is None:
            os.environ.update({k: v for k, v in model_env(self.ssd_path).items() if k.startswith("HF_")})
            from mlx_lm import load
            from mlx_lm.sample_utils import make_sampler

            model, tokenizer = load(str(model_link(self.ssd_path, self.name)))
            self._loaded = (model, tokenizer, make_sampler(temp=0.7, top_p=0.9))
        return self._loaded

    def complete(self, system, user):
        from mlx_lm import stream_generate

        model, tokenizer, sampler = self._load()
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        extra = {"enable_thinking": False} if self.name.startswith("qwen") else {}
        prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False, **extra)
        role = "vote" if system == importlib.import_module(WRITER_MODULE).VOTE_SYSTEM else "draft"
        text = "".join(part.text for part in stream_generate(model, tokenizer, prompt, max_tokens=self.MAX_TOKENS[role],
                                                             sampler=sampler))
        return clean_reply(text)


def clean_reply(text):
    """The JSON object in a model reply: no think block, no code fence, no text around the braces."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    if text and not text.startswith("{") and "{" in text and "}" in text:
        text = text[text.index("{"): text.rindex("}") + 1]
    return text


def main(argv=None, machine=None, client=None, model=None, publisher=None, env=None):
    """One launchd start. The keyword arguments are for tests; launchd passes none of them."""
    argv = argv if argv is not None else sys.argv[1:]
    env = os.environ if env is None else env
    home = Path(env.get("GRADE_INSIGHTS_HOME", "~/Library/Application Support/grade-insights")).expanduser()
    ssd = env.get("GRADE_INSIGHTS_SSD", DEFAULT_SSD)
    os.umask(0o077)  # every file this run makes (state, write-ups, fingerprint) is the owner's only
    home.mkdir(parents=True, exist_ok=True)
    if os.getpriority(os.PRIO_PROCESS, 0) < 10:
        os.setpriority(os.PRIO_PROCESS, 0, 10)
    machine = machine or real_machine(ssd)
    with open(home / "runner.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        state = State(home / "state.json")

        def sync(stamp):
            work_class = writer_attr("WriterWork")
            if work_class is None:
                return "no_writer"
            reader = client or ReadOnlyFamilyData()
            loaded = []

            def load():  # once per process: one unlock, two GETs
                if not loaded:
                    loaded.append(load_students(reader))
                return loaded[0]

            return run_sync(stamp, load, work_class, model or MlxModel(ssd), publisher_put(env, publisher),
                            machine, model_dir(ssd) / "runs", model_dir(ssd) / "state" / "fingerprint.json",
                            env=env)

        try:
            status = tick(state, machine, sync)
        except Exception as err:  # noqa: BLE001 - the type only, so no grade data can reach the log
            state.mark(status="failed")
            print(f"grade-insights: run failed ({type(err).__name__})", file=sys.stderr)
            return 1
    if "--verbose" in argv:
        print(f"grade-insights: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
