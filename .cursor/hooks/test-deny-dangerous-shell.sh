#!/bin/sh
# Tests for .cursor/hooks/deny-dangerous-shell.sh. Run from the repo root:
#   sh .cursor/hooks/test-deny-dangerous-shell.sh
# Exits nonzero if any case gets the wrong decision.

HOOK="$(dirname "$0")/deny-dangerous-shell.sh"
pass=0
fail=0

check() {
  expected="$1"; label="$2"; input="$3"
  got=$(printf '%s' "$input" | sh "$HOOK" 2>/dev/null)
  case "$got" in
    *'"permission": "deny"'*|*'"permission":"deny"'*) decision=deny ;;
    *'"permission": "allow"'*|*'"permission":"allow"'*) decision=allow ;;
    *) decision="invalid-output" ;;
  esac
  if [ "$decision" = "$expected" ]; then
    pass=$((pass + 1)); echo "ok    $expected  $label"
  else
    fail=$((fail + 1)); echo "FAIL  expected $expected, got $decision  $label"
  fi
}

j() { printf '{"command": "%s", "cwd": "/tmp/repo", "sandbox": false}' "$1"; }

# Deny: dangerous words, in any position
check deny  "launchctl"                   "$(j 'launchctl bootout gui/501/com.example.job')"
check deny  "sudo"                        "$(j 'sudo rm -rf /tmp/x')"
check deny  "reboot"                      "$(j 'reboot')"
check deny  "shutdown"                    "$(j 'shutdown -r now')"
check deny  "halt"                        "$(j 'halt')"
check deny  "pmset"                       "$(j 'pmset -a sleep 0')"
check deny  "after ;"                     "$(j 'echo hi; sudo true')"
check deny  "after &&"                    "$(j 'git pull && launchctl kickstart -k gui/501/x')"
check deny  "after |"                     "$(j 'echo pw | sudo -S true')"
check deny  "inside \$( )"                "$(j 'echo $(pmset -g)')"
check deny  "inside backticks"            "$(j 'echo `reboot`')"
check deny  "env prefix"                  "$(j 'env FOO=1 launchctl list')"
check deny  "command/exec prefix"         "$(j 'command exec /sbin/shutdown -h now')"
check deny  "quoted evasion su''do"       "$(j "su''do true")"
check deny  "uppercase SUDO"              "$(j 'SUDO true')"
# Deny: LaunchAgents / LaunchDaemons writes
check deny  "cp into ~/Library/LaunchAgents"   "$(j 'cp x.plist ~/Library/LaunchAgents/')"
check deny  "redirect into \$HOME LaunchAgents" "$(j 'cat x > $HOME/Library/LaunchAgents/x.plist')"
check deny  "plutil on /Library/LaunchDaemons"  "$(j 'plutil -lint /Library/LaunchDaemons/x.plist')"
check deny  "tee into /Users/*/Library"         "$(j 'tee /Users/someone/Library/LaunchAgents/x.plist')"
# Allow: ordinary commands
check allow "git status"                  "$(j 'git status')"
check allow "python3 -m pytest"           "$(j 'python3 -m pytest -q')"
check allow "word inside another word"    "$(j 'grep -r pseudocode asphalt.txt')"
check allow "npm test"                    "$(j 'npm test')"
# Fail closed: bad input
check deny  "empty input"                 ""
check deny  "malformed JSON"              '{"command": "git status"'
check deny  "missing command field"       '{"cwd": "/tmp/repo"}'
check deny  "non-string command"          '{"command": ["sudo", "true"]}'
check deny  "JSON array, not object"      '["git status"]'

# Fail closed: no python3 available (run a copy that points at missing paths)
tmp=$(mktemp -d)
sed 's#/usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3#/nonexistent/python3#' "$HOOK" > "$tmp/hook.sh"
got=$(j 'git status' | sh "$tmp/hook.sh" 2>/dev/null)
rm -rf "$tmp"
case "$got" in
  *'"permission":"deny"'*) pass=$((pass + 1)); echo "ok    deny  no python3 available" ;;
  *) fail=$((fail + 1)); echo "FAIL  expected deny  no python3 available" ;;
esac

echo "passed: $pass  failed: $fail"
[ "$fail" -eq 0 ]
