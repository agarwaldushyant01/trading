#!/bin/bash
# Start the scanner for today. Safe to run by hand or from launchd.
#
#   ./start.sh
#
# Stops anything already running, rebuilds reference data if it is not
# today's, starts the scanner, and confirms it is alive before returning.
# Exits non-zero on failure so a silent no-op cannot look like success.

set -uo pipefail
cd "$(dirname "$0")" || exit 1

# launchd gives a process almost no PATH, so nothing here may rely on the
# shell environment an interactive terminal would have.
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
PYTHON="${PYTHON:-$(command -v python3)}"
[ -x "$PYTHON" ] || PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"

FEED="${FEED:-iex}"
CONFIG="${CONFIG:-config/scanner-iex.yaml}"
TODAY=$(date +%F)
REFS="data/refs/${TODAY}-${FEED}.json"
[ "$FEED" = "sip" ] && REFS="data/refs/${TODAY}.json"

mkdir -p data/live data/refs
echo ""
echo "=== $(date '+%F %H:%M:%S %Z') — startup, feed ${FEED} ==="
echo "  python: $PYTHON"

# Skip weekends outright when run on a schedule.
DOW=$(date +%u)
if [ "$DOW" -gt 5 ]; then
    echo "  weekend, not starting."
    exit 0
fi

# 1. Nothing else holding the connection. Alpaca allows exactly one.
if pgrep -f "drivers.live" > /dev/null; then
    echo "  stopping the running scanner..."
    pkill -f "drivers.live"
    sleep 5
fi

# 2. Reference data must be today's. Yesterday's silently computes percent
#    change against a stale prior close.
if [ -f "$REFS" ]; then
    echo "  reference data: $REFS (already built)"
else
    echo "  building reference data — about 10 minutes..."
    if ! "$PYTHON" -m data.reference --date "$TODAY" --feed "$FEED"; then
        echo "  FAILED to build reference data. Not starting." >&2
        exit 1
    fi
fi

# 3. Start it. caffeinate keeps the machine awake for as long as it runs.
echo "  starting scanner..."
nohup caffeinate -is "$PYTHON" -u -m drivers.live \
    --feed "$FEED" --scanner-config "$CONFIG" \
    >> data/live/console.log 2>&1 &

# 4. Verify. A background job that dies instantly still returns a PID, so
#    check it is genuinely alive rather than trusting the launch.
sleep 20
if pgrep -f "drivers.live" > /dev/null; then
    echo "  RUNNING (pid $(pgrep -f 'drivers.live' | head -1))"
    echo "  Expect a 'Scanner started' notification on your phone."
else
    echo "  FAILED TO START. Last lines of the log:" >&2
    tail -20 data/live/console.log >&2
    exit 1
fi
