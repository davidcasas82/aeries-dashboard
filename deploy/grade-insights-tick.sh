#!/bin/sh
# launchd's entry for the grade-insights runner (deploy/com.davidcasas.grade-insights.plist.template).
#
# Runs insights_runner.py under the SSD venv's Python (Homebrew 3.12, which has mlx-lm) and
# nothing else: no python3, no /usr/bin/env, no PATH lookup. The plist passes that full path as
# $1 and it must match PINNED exactly, character for character (no symlink, no trailing slash,
# no ..). Each problem logs one line a day, not one every tick:
#   - a different interpreter argument: refused, exit 64;
#   - the SSD's mount point is missing: "the SSD is not mounted";
#   - the mount point is there but the interpreter isn't runnable (a venv symlink to a Homebrew
#     python@3.12 that's gone): "the venv Python is missing".
# With it all in place, the runner's own check (ssd_mounted: a mount point named "David C SSD")
# still decides the night.
set -u
PINNED='/Volumes/David C SSD/grade-insights/venv/bin/python'
MOUNT="${PINNED%/grade-insights/*}"
PY="${1-$PINNED}"
STATE="${GRADE_INSIGHTS_HOME:-$HOME/Library/Application Support/grade-insights}"
TODAY=$(/bin/date +%F)

once_a_day() {  # once_a_day <stamp name> <message>: print the message the first time today only
    /bin/mkdir -p "$STATE" || return 0
    if [ "$(/bin/cat "$STATE/$1.day" 2>/dev/null)" != "$TODAY" ]; then
        echo "$TODAY" > "$STATE/$1.day"
        echo "grade-insights: $TODAY $2"
    fi
}

if [ "$PY" != "$PINNED" ]; then
    once_a_day bad-interpreter "refusing to run under anything but the SSD venv's Python" >&2
    exit 64
fi
REPO=$(cd "$(/usr/bin/dirname "$0")/.." && pwd -P) || exit 70
if [ ! -x "$PY" ]; then
    if [ -d "$MOUNT" ]; then
        once_a_day no-venv "skipped, the venv Python is missing"
    else
        once_a_day no-ssd "skipped, the SSD is not mounted"
    fi
    exit 0
fi
exec "$PY" "$REPO/insights_runner.py"
