#!/bin/bash
# Prepare the session, then BECOME the trader.
#
#   ./start.sh
#
# The exec at the end is the whole point. This script used to launch the
# trader with `nohup ... &` and then exit — which works from an interactive
# shell but not under launchd: when the job's main process exits, launchd
# reaps the entire process group and the backgrounded trader dies with it.
# Silently, with no traceback, seconds after printing "Listening".
#
# That killed the 03:30 session twice. Replacing this shell with the trader
# means launchd supervises the trader itself, so it lives as long as the job
# does — and KeepAlive can restart it if it crashes.

set -uo pipefail
cd "$(dirname "$0")" || exit 1

export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
PYTHON="${PYTHON:-$(command -v python3)}"
[ -x "$PYTHON" ] || PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"

FEED="${FEED:-sip}"
SCANNER_CONFIG="${SCANNER_CONFIG:-config/scanner.yaml}"
MODE="${MODE:-}"

TODAY=$(date +%F)
REFS="data/refs/${TODAY}.json"
[ "$FEED" = "iex" ] && REFS="data/refs/${TODAY}-iex.json"

mkdir -p data/live data/refs data/mosquito
echo ""
echo "=== $(date '+%F %H:%M:%S %Z') — startup, feed ${FEED} ==="

DOW=$(date +%u)
if [ "$DOW" -gt 5 ]; then
    echo "  weekend, not starting."
    exit 0
fi

# Only one stream connection is allowed, so clear anything already holding it.
if pgrep -f "drivers.pattern_live" > /dev/null; then
    echo "  stopping the previous trader..."
    pkill -f "drivers.pattern_live"
    sleep 5
fi

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

echo "  starting paper trader (in the foreground, as this process)..."
echo ""

# caffeinate -i keeps the machine awake for as long as the trader runs, and
# exec replaces this shell entirely — so the process launchd is watching IS
# the trader. Nothing to orphan.
exec caffeinate -i "$PYTHON" -u -m drivers.pattern_live \
    --feed "$FEED" --scanner-config "$SCANNER_CONFIG" $MODE
