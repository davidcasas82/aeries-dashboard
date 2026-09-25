#!/usr/bin/env bash
# Launch, inspect, drive, and stop one local dashboard verification instance.
set -euo pipefail

SKILL_DIR=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$SKILL_DIR/../../.." && pwd)
STATE_DIR=/tmp/aeries-dashboard-verify
STATE="$STATE_DIR/state.json"
EVIDENCE=/cursor/stores/bc-f23fc86a-c458-4bdb-87a7-037a8949b15a/media/verify-aeries-dashboard

usage() {
  echo "usage: verify.sh launch|doctor|drive|cleanup" >&2
  exit 2
}

kill_group() {
  local pid=${1:-}
  [[ -z "$pid" ]] && return 0
  if kill -0 "$pid" 2>/dev/null; then
    kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
    local _i
    for _i in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$pid" 2>/dev/null || return 0
      sleep 0.2
    done
    kill -9 -- -"$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
  fi
}

read_state_field() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")' "$STATE" "$1"
}

cmd_cleanup() {
  if [[ -f "$STATE" ]]; then
    local chrome static api profile
    chrome=$(read_state_field chrome_pid || true)
    static=$(read_state_field static_pid || true)
    api=$(read_state_field api_pid || true)
    profile=$(read_state_field chrome_profile || true)
    kill_group "$chrome"
    kill_group "$static"
    kill_group "$api"
    if [[ -n "$profile" && "$profile" == /tmp/aeries-dashboard-verify/* ]]; then
      rm -rf "$profile"
    fi
    rm -f "$STATE"
  fi
  echo "cleaned up verification instance"
}

cmd_doctor() {
  [[ -f "$STATE" ]] || { echo "no verification state; run launch" >&2; exit 1; }
  python3 - "$STATE" <<'PY'
import json, os, sys, urllib.request
state = json.load(open(sys.argv[1]))
errors = []

def alive(pid, needle):
    path = f"/proc/{pid}/cmdline"
    if not pid or not os.path.exists(path):
        return False
    blob = open(path, "rb").read().replace(b"\0", b" ").decode("utf-8", "replace")
    return needle in blob

checks = [
    (state.get("api_pid"), "fixture_api.py", "fixture api"),
    (state.get("static_pid"), "http.server", "static server"),
    (state.get("chrome_pid"), "chrome", "chrome"),
]
for pid, needle, label in checks:
    if not alive(pid, needle):
        errors.append(f"{label} is not the process this run started")

def get(url):
    with urllib.request.urlopen(url, timeout=3) as res:
        return res.status, res.read().decode("utf-8", "replace")

try:
    status, body = get("http://127.0.0.1:8787/health")
    health = json.loads(body)
    if status != 200 or health.get("fixture") is not True:
        errors.append("port 8787 is not the verification fixture")
except Exception:
    errors.append("fixture health check failed")

try:
    status, html = get(state["base_url"])
    if status != 200 or 'id="pinInput"' not in html or "Open grades" not in html:
        errors.append("static server is not serving the dashboard lock screen")
except Exception:
    errors.append("static server did not answer")

try:
    req = urllib.request.Request(
        "http://127.0.0.1:8787/v1/grades/latest",
        headers={"Authorization": "Bearer fixture-session", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=3) as res:
        payload = json.loads(res.read().decode("utf-8"))
    students = payload.get("latest", {}).get("students") or []
    names = [s.get("name") for s in students]
    courses = []
    for student in students:
        for course in student.get("classes") or []:
            courses.append(course.get("course_name"))
    expected_names = ["Fixture Alpha", "Fixture Beta"]
    expected_courses = ["Algebra Fixture", "English Fixture", "Science Fixture"]
    if names != expected_names or courses != expected_courses:
        errors.append("fixture roster mismatch")
except Exception:
    errors.append("fixture grades payload was not readable")

try:
    with urllib.request.urlopen(f"http://127.0.0.1:{state['chrome_port']}/json/list", timeout=3) as res:
        targets = json.loads(res.read().decode("utf-8"))
    pages = [t for t in targets if t.get("type") == "page"]
    if not any(str(t.get("url") or "").startswith(state["base_url"]) for t in pages):
        errors.append("chrome is not open on the verification origin")
except Exception:
    errors.append("chrome debugging port did not answer")

if errors:
    print("doctor failed")
    for item in errors:
        print(f"- {item}")
    sys.exit(1)
print(f"doctor ok static={state['base_url']} api=http://127.0.0.1:8787 fixture")
PY
}

write_state() {
  python3 - "$STATE" "$1" "$2" "$3" "$4" "$5" "$6" "$7" <<'PY'
import json, sys
path, api, static, chrome, port, chrome_port, profile, base = sys.argv[1:]
json.dump({
    "api_pid": int(api),
    "static_pid": int(static),
    "chrome_pid": int(chrome),
    "api_port": 8787,
    "static_port": int(port),
    "chrome_port": int(chrome_port),
    "chrome_profile": profile,
    "base_url": base,
    "evidence_dir": "/cursor/stores/bc-f23fc86a-c458-4bdb-87a7-037a8949b15a/media/verify-aeries-dashboard",
}, open(path, "w"), indent=2)
PY
}

cmd_launch() {
  if [[ -f "$STATE" ]]; then
    echo "a verification state file already exists; run cleanup before launch" >&2
    exit 1
  fi
  mkdir -p "$STATE_DIR" "$EVIDENCE"
  cd "$ROOT"
  local port chrome_port profile api_pid="" static_pid="" chrome_pid="" base
  port=$(python3 - <<'PY'
import socket
for port in range(8765, 8866):
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        continue
    else:
        print(port)
        break
    finally:
        sock.close()
else:
    raise SystemExit("no free static port")
PY
)
  python3 - <<'PY'
import socket, sys
sock = socket.socket()
try:
    sock.bind(("127.0.0.1", 8787))
except OSError:
    sys.exit("127.0.0.1:8787 is already in use; refusing to share the fixture API")
finally:
    sock.close()
PY
  chrome_port=$(python3 - <<'PY'
import socket
for port in range(9333, 9434):
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        continue
    else:
        print(port)
        break
    finally:
        sock.close()
else:
    raise SystemExit("no free chrome port")
PY
)
  profile="$STATE_DIR/chrome-profile"
  rm -rf "$profile"
  mkdir -p "$profile"
  base="http://127.0.0.1:${port}/"

  setsid python3 "$SKILL_DIR/helpers/fixture_api.py" >"$STATE_DIR/api.log" 2>&1 &
  api_pid=$!
  setsid python3 -m http.server "$port" --bind 127.0.0.1 >"$STATE_DIR/static.log" 2>&1 &
  static_pid=$!
  setsid google-chrome \
    --headless=new \
    --disable-gpu \
    --no-sandbox \
    --disable-dev-shm-usage \
    --no-first-run \
    --disable-extensions \
    --remote-debugging-port="$chrome_port" \
    --remote-allow-origins='*' \
    --user-data-dir="$profile" \
    --window-size=1400,1000 \
    "$base" >"$STATE_DIR/chrome.log" 2>&1 &
  chrome_pid=$!
  write_state "$api_pid" "$static_pid" "$chrome_pid" "$port" "$chrome_port" "$profile" "$base"

  local _i ready=0
  for _i in $(seq 1 50); do
    if curl -fsS "http://127.0.0.1:8787/health" 2>/dev/null | grep -q '"fixture": true' \
      && curl -fsS "http://127.0.0.1:${chrome_port}/json/version" >/dev/null 2>&1 \
      && curl -fsS "$base" >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 0.2
  done
  if [[ "$ready" -ne 1 ]]; then
    echo "launch timed out waiting for fixture, static server, or chrome" >&2
    [[ -f "$STATE_DIR/chrome.log" ]] && tail -n 40 "$STATE_DIR/chrome.log" >&2 || true
    cmd_cleanup
    exit 1
  fi
  if ! node "$SKILL_DIR/helpers/drive.mjs" wait --selector "#pinInput" --timeout 20000; then
    echo "lock screen did not appear" >&2
    cmd_cleanup
    exit 1
  fi
  if ! cmd_doctor; then
    cmd_cleanup
    exit 1
  fi
  echo "launched $base"
}

COMMAND=${1:-}
shift || true
case "$COMMAND" in
  launch) cmd_launch ;;
  doctor) cmd_doctor ;;
  drive) node "$SKILL_DIR/helpers/drive.mjs" "$@" ;;
  cleanup) cmd_cleanup ;;
  *) usage ;;
esac
