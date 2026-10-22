#!/bin/sh
# launchd's entry for the grade-insights runner (deploy/com.davidcasas.grade-insights.plist.template).
#
# Runs insights_runner.py under the SSD venv's Python (Homebrew 3.12, which has mlx-lm) and
# nothing else: no python3, no /usr/bin/env, no PATH lookup. The plist passes that full path as
# $1 and it must match PINNED exactly. With the SSD unplugged the interpreter isn't there, so the
# tick is skipped with one log line a day and the runner never starts; with it mounted, the
# runner's own check (ssd_mounted: a mount point named "David C SSD") still decides the night.
set -u
PINNED='/Volumes/David C SSD/grade-insights/venv/bin/python'
PY="${1-$PINNED}"
if [ "$PY" != "$PINNED" ]; then
    echo "grade-insights: refusing to run under anything but the SSD venv's Python" >&2
    exit 64
fi
REPO=$(cd "$(/usr/bin/dirname "$0")/.." && pwd -P) || exit 70
if [ ! -x "$PY" ]; then
    STATE="${GRADE_INSIGHTS_HOME:-$HOME/Library/Application Support/grade-insights}"
    TODAY=$(/bin/date +%F)
    /bin/mkdir -p "$STATE" || exit 0
    if [ "$(/bin/cat "$STATE/no-ssd.day" 2>/dev/null)" != "$TODAY" ]; then
        echo "$TODAY" > "$STATE/no-ssd.day"
        echo "grade-insights: $TODAY skipped, the SSD is not mounted"
    fi
    exit 0
fi
exec "$PY" "$REPO/insights_runner.py"
