"""Nightly grade-insights runner for the Mac mini. One launchd tick every 5 minutes.

A tick decides whether tonight's work may run now, then does the next unfinished steps
until a hard stop, a busy machine, or the end of the plan. Progress is saved after every
step, so a pause, crash or reboot picks up where it stopped. The work itself (model calls,
writing, checking) sits behind NightWork; this file never calls a model.
"""

import fcntl
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

import requests

PACIFIC = ZoneInfo("America/Los_Angeles")
FRESHNESS_URL = "https://family-data.davidcasas.workers.dev/v1/grades/freshness"
EVENING_HOUR = 19
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
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return response.json()["updatedAt"]


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


def real_machine(ssd_path):
    return Machine(
        now=lambda: datetime.now(PACIFIC),
        freshness=read_freshness,
        ssd_mounted=lambda: os.path.ismount(ssd_path),
        free_memory_pct=free_memory_pct,
        busy=render_running,
    )


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    home = Path(os.environ.get("GRADE_INSIGHTS_HOME", "~/Library/Application Support/grade-insights")).expanduser()
    ssd = os.environ.get("GRADE_INSIGHTS_SSD", "/Volumes/David C SSD")
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
