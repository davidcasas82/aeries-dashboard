#!/bin/sh
# Tests for the Cursor deny hook. Run from the repo root:
#   sh .cursor/hooks/test-deny-dangerous-shell.sh
# Exits nonzero if any case gets the wrong result.

DIR="$(dirname "$0")"
HOOK="$DIR/deny-dangerous-shell.sh"
CONFIG="$DIR/../hooks.json"
pass=0
fail=0

PY=""
for cand in /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
  if [ -x "$cand" ]; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then echo "FAIL  python3 needed to run the matcher tests"; exit 1; fi

ok()  { pass=$((pass + 1)); echo "ok    $1"; }
bad() { fail=$((fail + 1)); echo "FAIL  $1"; }

# --- 1. Hook script decisions -------------------------------------------
check() {
  expected="$1"; label="$2"; input="$3"
  got=$(printf '%s' "$input" | sh "$HOOK" 2>/dev/null)
  decision=$(printf '%s' "$got" | "$PY" -c 'import json,sys
try:
    d = json.loads(sys.stdin.read()); print(d.get("permission", "missing") if isinstance(d, dict) else "invalid-output")
except Exception:
    print("invalid-output")')
  if [ "$decision" = "$expected" ]; then ok "hook $expected  $label"; else bad "hook expected $expected, got $decision  $label"; fi
}

j() { printf '{"command": "%s", "cwd": "/tmp/repo", "sandbox": false}' "$1"; }

check deny "launchctl"                    "$(j 'launchctl bootout gui/501/com.example.job')"
check deny "sudo"                         "$(j 'sudo rm -rf /tmp/x')"
check deny "reboot"                       "$(j 'reboot')"
check deny "shutdown"                     "$(j 'shutdown -r now')"
check deny "halt"                         "$(j 'halt')"
check deny "pmset"                        "$(j 'pmset -a sleep 0')"
check deny "after ;"                      "$(j 'echo hi; sudo true')"
check deny "after &&"                     "$(j 'git pull && launchctl kickstart -k gui/501/x')"
check deny "after |"                      "$(j 'echo pw | sudo -S true')"
check deny "inside \$( )"                 "$(j 'echo $(pmset -g)')"
check deny "inside backticks"             "$(j 'echo `reboot`')"
check deny "env prefix"                   "$(j 'env FOO=1 launchctl list')"
check deny "command/exec prefix"          "$(j 'command exec /sbin/shutdown -h now')"
check deny "mixed case SuDo"              "$(j 'SuDo true')"
check deny "upper case LAUNCHCTL"         "$(j 'LAUNCHCTL list')"
check deny "quoted su''do (if it reaches the hook)" "$(j "su''do true")"
check deny "cp into ~/Library/LaunchAgents"    "$(j 'cp x.plist ~/Library/LaunchAgents/')"
check deny "redirect into \$HOME LaunchAgents" "$(j 'cat x > $HOME/Library/LaunchAgents/x.plist')"
check deny "plutil on /Library/LaunchDaemons"  "$(j 'plutil -lint /Library/LaunchDaemons/x.plist')"
check deny "tee into /Users/*/Library"         "$(j 'tee /Users/someone/Library/LaunchAgents/x.plist')"
check ask  "false positive: asphalt"      "$(j 'cat asphalt.txt')"
check ask  "false positive: haltingly"    "$(j 'echo haltingly')"
check ask  "false positive: sudoers_note" "$(j 'grep sudoers_note docs.txt')"
check deny "fail closed: empty input"     ""
check deny "fail closed: malformed JSON"  '{"command": "sudo true"'
check deny "fail closed: missing command" '{"cwd": "/tmp/repo"}'
check deny "fail closed: non-string command" '{"command": ["sudo", "true"]}'
check deny "fail closed: JSON array"      '["sudo true"]'

tmp=$(mktemp -d)
sed 's#/usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3#/nonexistent/python3#' "$HOOK" > "$tmp/hook.sh"
got=$(j 'sudo true' | sh "$tmp/hook.sh" 2>/dev/null)
rm -rf "$tmp"
case "$got" in
  *'"permission":"deny"'*) ok "hook deny  fail closed: no python3 available" ;;
  *) bad "hook expected deny  fail closed: no python3 available" ;;
esac

# --- 2. The hook never auto-approves ------------------------------------
if grep -q '"allow"' "$HOOK"; then bad 'hook script contains "allow"'; else ok 'hook script never contains "allow"'; fi

# --- 3. hooks.json matcher -----------------------------------------------
# The matcher decides which commands reach the hook at all. Commands it
# does not match keep Cursor's normal approval. Checked with both search
# and full-match semantics.
matcher_out=$("$PY" - "$CONFIG" <<'PYEOF'
import json, re, sys
cfg = json.load(open(sys.argv[1]))
hook = cfg["hooks"]["beforeShellExecution"][0]
results = []
def r(ok, label): results.append(("ok    " if ok else "FAIL  ") + label)
r(cfg.get("version") == 1, "config version is 1")
r(hook.get("failClosed") is True, "config failClosed is true")
r(hook.get("timeout") == 10, "config timeout is 10")
pat = re.compile(hook["matcher"])
must_match = [
    "launchctl bootout gui/501/com.example.job", "sudo rm -rf /tmp/x", "reboot",
    "shutdown -r now", "halt", "pmset -a sleep 0", "echo hi; sudo true",
    "git pull && launchctl kickstart -k gui/501/x", "echo pw | sudo -S true",
    "echo $(pmset -g)", "echo `reboot`", "env FOO=1 launchctl list",
    "command exec /sbin/shutdown -h now", "SuDo true", "LAUNCHCTL list",
    "cp x.plist ~/Library/LaunchAgents/", "cat x > $HOME/Library/LaunchAgents/x.plist",
    "plutil -lint /Library/LaunchDaemons/x.plist", "tee /Users/someone/Library/LaunchAgents/x.plist",
    "echo ok\nsudo true", "cat asphalt.txt", "echo haltingly",
]
must_not_match = [
    "git status", "python3 -m pytest -q", "npm test", "grep -r pseudocode notes.txt",
    "ls -la", "make test",
]
for c in must_match:
    r(bool(pat.search(c)) and bool(pat.fullmatch(c)), "matcher hits      " + c.replace("\n", "\\n"))
for c in must_not_match:
    r(not pat.search(c) and not pat.fullmatch(c), "matcher skips     " + c)
print("\n".join(results))
PYEOF
) || matcher_out="FAIL  could not evaluate the hooks.json matcher"
printf '%s\n' "$matcher_out"
p=$(printf '%s\n' "$matcher_out" | grep -c '^ok')
f=$(printf '%s\n' "$matcher_out" | grep -c '^FAIL')
pass=$((pass + p)); fail=$((fail + f))

echo "known gap (not asserted): quote tricks like su''do skip the matcher and never reach the hook"
echo "passed: $pass  failed: $fail"
[ "$fail" -eq 0 ]
