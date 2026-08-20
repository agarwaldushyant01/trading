#!/bin/bash
# Start the paper trader for today. Safe to run by hand or from launchd.
#
#   ./start.sh
#
# Stops anything already running, rebuilds reference data if it is not
# today's, starts the trader, and confirms it is alive before returning.
# Exits non-zero on failure so a silent no-op cannot look like success.

set -uo pipefail
cd "$(dirname "$0")" || exit 1

# launchd gives a process almost no PATH, so nothing here may rely on the
# environment an interactive shell would have.
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
PYTHON="${PYTHON:-$(command -v python3)}"
[ -x "$PYTHON" ] || PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"

# Real-time SIP via Algo Trader Plus. The old IEX path needed a separate
# scaled config and was blind before 08:00; neither applies now.
FEED="${FEED:-sip}"
SCANNER_CONFIG="${SCANNER_CONFIG:-config/scanner.yaml}"
MODE="${MODE:-}"                    # set MODE=--dry-run to decide without ordering

TODAY=$(date +%F)
REFS="data/refs/${TODAY}.json"
[ "$FEED" = "iex" ] && REFS="data/refs/${TODAY}-iex.json"

mkdir -p data/live data/refs data/mosquito
echo ""
echo "=== $(date '+%F %H:%M:%S %Z') — startup, feed ${FEED} ==="
echo "  python: $PYTHON"

DOW=$(date +%u)
if [ "$DOW" -gt 5 ]; then
    echo "  weekend, not starting."
    exit 0
fi

# 1. Only one connection is allowed, so clear anything already holding it.
for proc in drivers.paper_live drivers.live drivers.premarket; do
    if pgrep -f "$proc" > /dev/null; then
        echo "  stopping $proc..."
        pkill -f "$proc"
    fi
done
sleep 5

# 2. Reference data must be today's. Yesterday's silently computes percent
#    change against a stale prior close.
if [ -f "$REFS" ]; then
    echo "  reference data: $REFS (already built)"
else
    echo "  building reference data — about 10 minutes..."
    FEED_ARG=""
    [ "$FEED" = "iex" ] && FEED_ARG="--feed iex"
    if ! "$PYTHON" -m data.reference --date "$TODAY" $FEED_ARG; then
        echo "  FAILED to build reference data. Not starting." >&2
        exit 1
    fi
fi

# 3. Start. caffeinate keeps the machine awake for as long as it runs.
echo "  starting paper trader..."
nohup caffeinate -is "$PYTHON" -u -m drivers.paper_live \
    --feed "$FEED" --scanner-config "$SCANNER_CONFIG" $MODE \
    >> data/live/console.log 2>&1 &

# 4. Verify. A background job that dies instantly still returns a PID, so
#    check it is genuinely alive rather than trusting the launch.
sleep 20
if pgrep -f "drivers.paper_live" > /dev/null; then
    echo "  RUNNING (pid $(pgrep -f 'drivers.paper_live' | head -1))"
    echo "  Expect a 'Paper trader started' notification on your phone."
    echo ""
    echo "  watch:  tail -f data/live/console.log"
    echo "  stop:   pkill -f drivers.paper_live"
else
    echo "  FAILED TO START. Last lines of the log:" >&2
    tail -20 data/live/console.log >&2
    exit 1
fi
