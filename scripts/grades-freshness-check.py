#!/usr/bin/env python3
"""Weekday check of family-data's public grade stamp.

GET https://family-data.davidcasas.workers.dev/v1/grades/freshness only.
The body is {"ok": true, "updatedAt": "<ISO UTC>"}. This script never reads
grades and never uses the PIN.

A stamp more than 14 hours old is stale. A failed probe (network error,
timeout, non-200, bad JSON, ok not true, or a missing or unparseable
updatedAt) is probe_broken. Fresh exits 0 and sends nothing.

An alert is one POST of JSON to WEBHOOK_URL with Authorization: Bearer
WEBHOOK_KEY, both read from a mode 0600 env file. At most one alert of each
kind goes out per local day. The key and the webhook URL are never printed.

Exit codes: 0 fresh, 1 stale, 2 probe_broken. --test-alert exits 0 when the
labeled test POST succeeds and 1 when it does not.
"""

import argparse
import json
import os
import socket
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

FAMILY_DATA_URL = "https://family-data.davidcasas.workers.dev"
FRESHNESS_PATH = "/v1/grades/freshness"
FRESHNESS_URL = FAMILY_DATA_URL + FRESHNESS_PATH
STALE_AFTER = timedelta(hours=14)
USER_AGENT = "grades-freshness-check/1"
GET_TIMEOUT = 20
POST_TIMEOUT = 10
MAX_BODY = 8192
APP_DIR = Path("Library") / "Application Support" / "aeries-dashboard"


@dataclass(frozen=True)
class Probe:
    result: str
    updated_at: Optional[str]
    age: Optional[timedelta]
    http_status: Optional[int]
    error: Optional[str]


def _urlopen(request, timeout):
    return urllib.request.urlopen(request, timeout=timeout)


def support_dir():
    return Path.home() / APP_DIR


def default_env_file():
    override = os.environ.get("GRADES_FRESHNESS_ENV")
    if override:
        return Path(override).expanduser()
    return support_dir() / "grades-freshness.env"


def default_log_file():
    override = os.environ.get("GRADES_FRESHNESS_LOG")
    if override:
        return Path(override).expanduser()
    return support_dir() / "grades-freshness.log"


def default_state_file(log_path):
    override = os.environ.get("GRADES_FRESHNESS_STATE")
    if override:
        return Path(override).expanduser()
    return Path(log_path).with_name("grades-freshness-state.json")


def build_parser():
    parser = argparse.ArgumentParser(description="Check the public grade stamp is under 14 hours old.")
    parser.add_argument(
        "--env-file",
        default=None,
        help="mode 0600 file with WEBHOOK_URL and WEBHOOK_KEY "
        "(default: GRADES_FRESHNESS_ENV or ~/Library/Application Support/aeries-dashboard/grades-freshness.env)",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="one line per run (default: GRADES_FRESHNESS_LOG or "
        "~/Library/Application Support/aeries-dashboard/grades-freshness.log)",
    )
    parser.add_argument(
        "--state",
        default=None,
        help="dedupe state, next to the log unless GRADES_FRESHNESS_STATE or this flag is set",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the alert that would be sent; do not POST")
    parser.add_argument("--test-alert", action="store_true", help="POST one alert whose summary starts with TEST")
    return parser


def resolve_env_file(args):
    if args.env_file:
        return Path(args.env_file).expanduser()
    return default_env_file()


def resolve_log_file(args):
    if args.log:
        return Path(args.log).expanduser()
    return default_log_file()


def resolve_state_file(args, log_path):
    if args.state:
        return Path(args.state).expanduser()
    if os.environ.get("GRADES_FRESHNESS_STATE"):
        return default_state_file(log_path)
    return Path(log_path).with_name("grades-freshness-state.json")


def as_aware(now):
    if now.tzinfo is None:
        return now.astimezone()
    return now


def local_day(now):
    return as_aware(now).astimezone().date().isoformat()


def parse_stamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    return stamp


def stamp_age(value, now):
    """(updatedAt or None, age or None). A short unparseable string is kept and has no age."""
    if not isinstance(value, str):
        return None, None
    raw = value.strip()
    stamp = parse_stamp(raw)
    if stamp is None:
        if not raw or len(raw) > 80 or any(ch in raw for ch in "\n\r"):
            return None, None
        return raw, None
    return raw, as_aware(now) - stamp


def age_hours(age):
    if age is None:
        return None
    return round(age.total_seconds() / 3600, 2)


def format_age(age):
    hours = age_hours(age)
    if hours is None:
        return "na"
    return f"{hours:.2f}"


def _broken(status, error, updated_at=None, age=None):
    return Probe("probe_broken", updated_at, age, status, error)


def fetch_freshness(now, opener=None, url=FRESHNESS_URL):
    """GET the public stamp. opener is urllib.request.urlopen's signature, for tests."""
    open_url = opener or _urlopen
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with open_url(request, GET_TIMEOUT) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            body = response.read(MAX_BODY)
            if response.read(1):
                return _broken(status, "response too large")
    except urllib.error.HTTPError as exc:
        return _broken(exc.code, f"HTTP {exc.code}")
    except (TimeoutError, socket.timeout):
        return _broken(None, "timeout")
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return _broken(None, "timeout")
        name = type(reason).__name__ if reason is not None else "URLError"
        return _broken(None, name)
    except OSError as exc:
        return _broken(None, type(exc).__name__)
    if status != 200:
        return _broken(status, f"HTTP {status}")
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return _broken(status, "bad JSON")
    if not isinstance(data, dict):
        return _broken(status, "bad JSON")
    updated_at, age = stamp_age(data.get("updatedAt"), now)
    if data.get("ok") is not True:
        return _broken(status, "ok is not true", updated_at if age is not None else None, age if age is not None else None)
    if age is None:
        return _broken(status, "missing or unparseable updatedAt")
    result = "stale" if age > STALE_AFTER else "fresh"
    return Probe(result, updated_at, age, status, None)


def summary_for(probe):
    if probe.result == "stale":
        return f"Grade stamp is {format_age(probe.age)} hours old (older than 14)."
    detail = probe.error or "failed"
    return f"Grade freshness probe failed ({detail})."


def build_payload(kind, summary, updated_at, age, http_status, error, now, host):
    return {
        "kind": kind,
        "summary": summary,
        "updatedAt": updated_at,
        "age_hours": age_hours(age),
        "http_status": http_status,
        "error": error,
        "ran_at": as_aware(now).isoformat(timespec="seconds"),
        "host": host,
    }


def payload_for(probe, now, host):
    return build_payload(
        probe.result,
        summary_for(probe),
        probe.updated_at,
        probe.age,
        probe.http_status,
        probe.error,
        now,
        host,
    )


def test_payload(now, host):
    return build_payload("test", "TEST grade freshness check", None, None, None, None, now, host)


def log_line(now, result, age, alert_sent):
    stamp = as_aware(now).isoformat(timespec="seconds")
    return f"{stamp} result={result} age_hours={format_age(age)} alert_sent={alert_sent}"


def exit_code(result):
    return {"fresh": 0, "stale": 1, "probe_broken": 2}.get(result, 1)


def _webhook_url(path):
    """(url or None, key or None, error or None).

    A missing file is an error. A mode other than 0600 is not read.
    WEBHOOK_KEY is the bearer token. Callers must not log it or the URL.
    """
    path = Path(path)
    if not path.is_file():
        return None, None, "webhook env file is missing"
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        return None, None, "webhook env file is not mode 0600"
    url = None
    key = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            name, _, value = line.partition("=")
            value = value.strip().strip("\"'")
            if name.strip() in ("WEBHOOK_URL", "URL"):
                url = value or None
            elif name.strip() == "WEBHOOK_KEY" and value:
                key = value
        elif line.startswith("https://") or line.startswith("http://"):
            url = line
    if not url:
        return None, None, "webhook url missing"
    if not key:
        return None, None, "webhook key missing"
    return url, key, None


def _post_json(url, payload, key, opener=None):
    open_url = opener or _urlopen
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    with open_url(request, POST_TIMEOUT) as response:
        response.read()


def _webhook_secrets(url, key):
    """Pieces of the webhook that must not be written into a log."""
    found = []
    if key:
        found.append(key)
        found.append("Bearer " + key)
    if not url:
        return found
    found.append(url)
    parts = urllib.parse.urlsplit(url)
    if parts.username:
        found.append(urllib.parse.unquote(parts.username))
    if parts.password:
        found.append(urllib.parse.unquote(parts.password))
    if parts.netloc:
        found.append(parts.netloc)
    if parts.scheme and parts.netloc:
        found.append("%s://%s" % (parts.scheme, parts.netloc))
    for _name, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=False):
        if value:
            found.append(value)
    return found


def _scrub_secrets(text, secrets):
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    return text


def deliver(env_path, payload, opener=None):
    """POST payload. The returned note never contains the key or the webhook URL."""
    url, key, error = _webhook_url(env_path)
    secrets = _webhook_secrets(url or "", key or "")
    if error:
        return False, _scrub_secrets(error, secrets)
    try:
        _post_json(url, payload, key, opener)
    except urllib.error.HTTPError as exc:
        return False, _scrub_secrets(f"HTTP {exc.code}", secrets)
    except (TimeoutError, socket.timeout):
        return False, "timeout"
    except urllib.error.URLError as exc:
        reason = exc.reason
        name = "timeout" if isinstance(reason, (TimeoutError, socket.timeout)) else (
            type(reason).__name__ if reason is not None else "URLError"
        )
        return False, _scrub_secrets(name, secrets)
    except Exception as exc:
        return False, _scrub_secrets(type(exc).__name__, secrets)
    return True, None


def ensure_private_parent(path):
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)
    if parent.name == "aeries-dashboard":
        os.chmod(parent, 0o700)


def append_log(path, line):
    path = Path(path)
    ensure_private_parent(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (line + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def load_state(path):
    path = Path(path)
    if not path.is_file():
        return {"alerts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError, ValueError):
        return {"alerts": {}}
    alerts = data.get("alerts") if isinstance(data, dict) else None
    clean = {}
    if isinstance(alerts, dict):
        for kind, day in alerts.items():
            if kind in ("stale", "probe_broken") and isinstance(day, str):
                clean[kind] = day
    return {"alerts": clean}


def save_state(path, state):
    path = Path(path)
    ensure_private_parent(path)
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps({"alerts": state.get("alerts", {})}, indent=2, sort_keys=True) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def already_alerted(state, kind, day):
    return (state.get("alerts") or {}).get(kind) == day


def mark_alerted(state, kind, day):
    alerts = dict(state.get("alerts") or {})
    alerts[kind] = day
    state["alerts"] = alerts


def _print_dry_run(payload, alert_sent, line):
    if alert_sent == "deduped":
        print("dry-run: no POST (already sent this kind today)")
    elif payload is None:
        print("dry-run: no alert")
    else:
        print("dry-run: would POST")
        print(json.dumps(payload, indent=2, sort_keys=True))
    print(line)


def _note_failure(note):
    if note:
        print(f"alert not sent: {note}", file=sys.stderr)


def run_check(args, now, host, log_path, state_path, env_path, opener):
    probe = fetch_freshness(now, opener)
    alert_sent = "no"
    payload = None
    if probe.result != "fresh":
        state = load_state(state_path)
        day = local_day(now)
        if already_alerted(state, probe.result, day):
            alert_sent = "deduped"
        else:
            payload = payload_for(probe, now, host)
            if args.dry_run:
                alert_sent = "yes"
            else:
                sent, note = deliver(env_path, payload, opener)
                if sent:
                    mark_alerted(state, probe.result, day)
                    save_state(state_path, state)
                    alert_sent = "yes"
                else:
                    alert_sent = "no"
                    _note_failure(note)
    line = log_line(now, probe.result, probe.age, alert_sent)
    if args.dry_run:
        _print_dry_run(payload, alert_sent, line)
        return exit_code(probe.result)
    append_log(log_path, line)
    print(line)
    return exit_code(probe.result)


def run_test_alert(args, now, host, log_path, env_path, opener):
    payload = test_payload(now, host)
    line = log_line(now, "test", None, "yes")
    if args.dry_run:
        _print_dry_run(payload, "yes", line)
        return 0
    sent, note = deliver(env_path, payload, opener)
    line = log_line(now, "test", None, "yes" if sent else "no")
    append_log(log_path, line)
    print(line)
    if not sent:
        _note_failure(note)
        return 1
    return 0


def main(argv=None, now=None, opener: Optional[Callable] = None, host=None):
    args = build_parser().parse_args(argv)
    now = as_aware(now if now is not None else datetime.now().astimezone())
    host = host or socket.gethostname()
    log_path = resolve_log_file(args)
    state_path = resolve_state_file(args, log_path)
    env_path = resolve_env_file(args)
    if args.test_alert:
        return run_test_alert(args, now, host, log_path, env_path, opener)
    return run_check(args, now, host, log_path, state_path, env_path, opener)


if __name__ == "__main__":
    sys.exit(main())
