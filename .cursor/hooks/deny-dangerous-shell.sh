#!/bin/sh
# Cursor beforeShellExecution hook: fail-closed deny list.
#
# Blocks agent shell commands that touch launchd, privilege escalation or
# power state:
#   - launchctl, sudo, reboot, shutdown, halt, pmset (as a word anywhere,
#     including after ; && || | $( ` and env/command/exec prefixes)
#   - anything that mentions LaunchAgents or LaunchDaemons
# Fails closed: empty or invalid JSON, a missing or non-string "command"
# field, or no usable python3 all result in a deny.
#
# Input (stdin):  {"command": "...", "cwd": "...", ...}
# Output (stdout): {"permission": "allow"} or
#                  {"permission": "deny", "user_message": ..., "agent_message": ...}
# To remove: delete .cursor/hooks.json and .cursor/hooks/ (or revert the commit).

deny_raw() {
  printf '%s\n' '{"permission":"deny","user_message":"Blocked by the repo deny hook: '"$1"'","agent_message":"This shell command was blocked by .cursor/hooks/deny-dangerous-shell.sh ('"$1"'). launchd, sudo, reboot/shutdown/halt, pmset and LaunchAgents/LaunchDaemons changes need the owner to run them by hand. Do not retry or work around this."}'
  exit 0
}

PY=""
for cand in /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
  if [ -x "$cand" ]; then PY="$cand"; break; fi
done
[ -n "$PY" ] || deny_raw "python3 not available, failing closed"

INPUT=$(cat) || deny_raw "could not read hook input"
[ -n "$INPUT" ] || deny_raw "empty hook input"

HOOK_INPUT="$INPUT" "$PY" - <<'PYEOF' || deny_raw "hook parser failed"
import json, os, re, sys

def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.exit(0)

def deny(reason):
    out({
        "permission": "deny",
        "user_message": "Blocked by the repo deny hook: " + reason,
        "agent_message": (
            "This shell command was blocked by .cursor/hooks/deny-dangerous-shell.sh ("
            + reason + "). launchd, sudo, reboot/shutdown/halt, pmset and "
            "LaunchAgents/LaunchDaemons changes need the owner to run them by hand. "
            "Do not retry or work around this."
        ),
    })

raw = os.environ.get("HOOK_INPUT", "")
if not raw.strip():
    deny("empty hook input")
try:
    payload = json.loads(raw)
except Exception:
    deny("hook input is not valid JSON")
if not isinstance(payload, dict) or "command" not in payload:
    deny("hook input has no command field")
cmd = payload["command"]
if not isinstance(cmd, str):
    deny("command field is not a string")

# Check the raw command and a copy with quotes/backslashes stripped,
# so su''do, "sudo" and s\udo are caught too.
variants = [cmd, re.sub(r"[\"'\\]", "", cmd)]

WORDS = re.compile(
    r"(?<![A-Za-z0-9_-])(launchctl|sudo|reboot|shutdown|halt|pmset)(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
LAUNCHD_DIRS = re.compile(r"launch(agents|daemons)", re.IGNORECASE)

for v in variants:
    m = WORDS.search(v)
    if m:
        deny("command uses '" + m.group(1).lower() + "'")
    if LAUNCHD_DIRS.search(v):
        deny("command mentions LaunchAgents/LaunchDaemons")

out({"permission": "allow"})
PYEOF
