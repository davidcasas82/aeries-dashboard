"""Nightly grade-insights runner for the Mac mini. One launchd tick every 5 minutes.

A tick decides whether tonight's work may run now, then does the next unfinished steps
until a hard stop, a busy machine, or the end of the plan. Progress is saved after every
step, so a pause, crash or reboot picks up where it stopped. The work itself (model calls,
writing, checking) sits behind NightWork; this file never calls a model.
"""

import fcntl
import json
import os
import plistlib
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from class_match import assignments_for_class


PACIFIC = ZoneInfo("America/Los_Angeles")
FAMILY_DATA_URL = "https://family-data.davidcasas.workers.dev"
FRESHNESS_URL = f"{FAMILY_DATA_URL}/v1/grades/freshness"
EVENING_HOUR = 19
USER_AGENT = "grade-insights-runner/1"
SUNDAY = 6

# Hard stops: (weekdays or None for every day, start, end). The runner never works inside one.
# 3:05-4:40 leaves the 3:17 AM backup and the 4:30 AM Photo Drop clear alone.
# Sunday 1:00-6:30 covers the Amazon Photos window (1:07-5:07) and the 5:37 restart.
# 6:30-19:00 is daytime: tonight's run is over.
BLOCKED = (
    (None, time(3, 5), time(4, 40)),
    (None, time(6, 30), time(19, 0)),
    ({SUNDAY}, time(1, 0), time(6, 30)),
)
# The parent PIN is a generic password in the mini's login Keychain. Code only for now.
PIN_KEYCHAIN_SERVICE = "family-data-parent-pin"
PIN_KEYCHAIN_ACCOUNT = "aeries"
SSD_VOLUME_NAME = "David C SSD"
DEFAULT_SSD = f"/Volumes/{SSD_VOLUME_NAME}"
# Installed on the SSD with mlx-lm 0.32.2. Smoke-test peaks: Qwen 5.2 GB at 32 tok/s, Gemma 8.1 GB at 20 tok/s.
MODEL_REPOS = {
    "qwen3-8b": "mlx-community/Qwen3-8B-4bit",
    "gemma3-12b": "mlx-community/gemma-3-text-12b-it-4bit",
}
MIN_FREE_MEMORY_PCT = 35
BUSY_PROCESSES = ("Blender", "blender", "ffmpeg")


class NightWork(Protocol):
    """The model and writer side. plan() lists step ids; run_step() does one and is safe to repeat."""

    def plan(self, night: str) -> list: ...

    def run_step(self, night: str, step: str) -> None: ...


class PendingWork:
    """Stand-in until the writer is decided: a night with nothing to do."""

    def plan(self, night):
        return []

    def run_step(self, night, step):
        raise NotImplementedError("writer not built yet")


@dataclass(frozen=True)
class Machine:
    """What the runner reads from the box it runs on. Tests pass fakes."""

    now: Callable[[], datetime]
    freshness: Callable[[], str]
    ssd_mounted: Callable[[], bool]
    free_memory_pct: Callable[[], int]
    busy: Callable[[], bool]


def blocked(now):
    """True inside a hard-stop window, on the Pacific clock."""
    clock = now.timetz().replace(tzinfo=None)
    for days, start, end in BLOCKED:
        if days is not None and now.weekday() not in days:
            continue
        if start <= clock < end:
            return True
    return False


def evening_night(stamp):
    """Pacific date of the evening scrape a freshness stamp came from, or None for any other scrape."""
    landed = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(PACIFIC)
    if landed.hour >= EVENING_HOUR:
        return landed.date().isoformat()
    return None


def tonight(now):
    """The night a tick belongs to: before 7 AM is still the previous evening's night."""
    day = now.date() - timedelta(days=1) if now.hour < 7 else now.date()
    return day.isoformat()


class State:
    """Progress file: which night is in flight and which steps are done. Written atomically."""

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

    def start_night(self, night, stamp):
        self.data = {"night": night, "stamp": stamp, "status": "running", "done": []}
        self.save()

    def mark(self, **fields):
        self.data.update(fields)
        self.save()

    def step_done(self, step):
        self.data.setdefault("done", []).append(step)
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.replace(tmp, self.path)


def tick(state, work, machine, log=print):
    """One launchd tick. Returns the status it left the night in."""
    now = machine.now()
    if blocked(now):
        return "blocked"
    night = tonight(now)

    if state.get("night") != night or state.get("status") in (None, "waiting_for_scrape"):
        stamp = machine.freshness()
        if evening_night(stamp) != night:
            state.mark(night=night, status="waiting_for_scrape")
            return "waiting_for_scrape"
        if not machine.ssd_mounted():
            state.mark(night=night, stamp=stamp, status="skipped_no_ssd", done=[])
            log(f"grade-insights: {night} skipped, the SSD is not mounted")
            return "skipped_no_ssd"
        state.start_night(night, stamp)
        log(f"grade-insights: {night} started from the evening scrape")

    if state.get("status") in ("done", "skipped_no_ssd"):
        return state.get("status")

    done = set(state.get("done") or [])
    for step in work.plan(night):
        if step in done:
            continue
        if blocked(machine.now()):
            state.mark(status="paused")
            return "paused"
        if machine.busy() or machine.free_memory_pct() < MIN_FREE_MEMORY_PCT:
            state.mark(status="waiting_for_machine")
            return "waiting_for_machine"
        state.mark(status="running")
        work.run_step(night, step)
        state.step_done(step)
    state.mark(status="done")
    log(f"grade-insights: {night} done, {len(state.get('done') or [])} steps")
    return "done"


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
    """The parent PIN from the login Keychain. Nothing calls this yet."""
    return keychain_secret(PIN_KEYCHAIN_SERVICE, PIN_KEYCHAIN_ACCOUNT)


class FamilyDataError(RuntimeError):
    """Carries the status and path only, never a PIN or token."""


class ReadOnlyFamilyData:
    """The runner's only family-data client for the PIN session. GET-only by construction.

    The Keychain PIN is the family PIN, so its session has the write role. This client can
    send exactly one non-GET, the unlock POST, and has no put, post, patch or delete methods.
    Publishing (PR5) uses the separate AERIES_INSIGHTS_SECRET bearer, never this session.
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


# ── Loading tonight's students ───────────────────────────────────────────────


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


def writer_upcoming():
    """The writer's upcoming_items, or None while the writer (PR #52) isn't in this checkout.

    Imported here rather than at the top so the runner keeps working without the writer.
    """
    try:
        from insights_writer import upcoming_items
    except ImportError:
        return None
    return upcoming_items


def load_students(client, upcoming_items=None):
    """[{"insight_facts", "log_items", "upcoming"}] per student with facts, for the writer.

    Two GETs on the read-only session: the latest grades and the scored log. Students are
    matched to their log by the same key the scrape writes under; nothing here is logged.
    upcoming is [] until the writer provides upcoming_items.
    """
    latest = client.latest_grades() or {}
    latest = latest.get("latest") if isinstance(latest.get("latest"), dict) else latest
    remote_log = client.scored_log() or {}
    log_doc = remote_log.get("doc") if isinstance(remote_log.get("doc"), dict) else {}
    logged = log_doc.get("students") or {}
    build = upcoming_items or writer_upcoming()
    students = []
    for student in latest.get("students") or []:
        facts = student.get("insight_facts")
        if not isinstance(facts, dict) or not facts.get("facts"):
            continue
        entry = logged.get(str(student.get("sn"))) or {}
        upcoming = build(class_inputs(student), facts["as_of"]) if build and facts.get("as_of") else []
        students.append({"insight_facts": facts, "log_items": entry.get("items") or [], "upcoming": upcoming})
    return students


def writer_work(client, model, store, put=None, env=None):
    """The writer's nightly steps fed by load_students, or None while the writer isn't merged.

    The load is read once per runner process, so a run unlocks at most once.
    """
    try:
        from insights_writer import WriterWork
    except ImportError:
        return None
    loaded = []

    def load():
        if not loaded:
            loaded.append(load_students(client))
        return loaded[0]

    return WriterWork(load, model, store, put=put, env=env)


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


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    home = Path(os.environ.get("GRADE_INSIGHTS_HOME", "~/Library/Application Support/grade-insights")).expanduser()
    ssd = os.environ.get("GRADE_INSIGHTS_SSD", DEFAULT_SSD)
    home.mkdir(parents=True, exist_ok=True)
    if os.getpriority(os.PRIO_PROCESS, 0) < 10:
        os.setpriority(os.PRIO_PROCESS, 0, 10)
    with open(home / "runner.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        status = tick(State(home / "state.json"), PendingWork(), real_machine(ssd))
    if "--verbose" in argv:
        print(f"grade-insights: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
