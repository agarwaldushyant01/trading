#!/bin/bash
# Schedule the scanner to start itself every weekday morning.
#
#   ./install-schedule.sh          install (default 03:30 local)
#   ./install-schedule.sh 03:00    install at a different time
#   ./install-schedule.sh --remove
#
# Two separate things have to be true for a scheduled job to work on a Mac,
# and the second one is what people miss:
#
#   1. launchd must know to run the job          <- this script does it
#   2. the Mac must be AWAKE at that hour        <- needs pmset, see below
#
# A sleeping Mac does not run scheduled jobs on time; launchd defers them
# until the machine wakes, which for a 03:30 job means it fires whenever you
# open the lid. That is the failure mode this script warns about at the end.

set -uo pipefail
cd "$(dirname "$0")" || exit 1

REPO="$(pwd)"
LABEL="com.trading.scanner"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
HEALTH_LABEL="com.trading.healthcheck"
HEALTH_PLIST="$HOME/Library/LaunchAgents/${HEALTH_LABEL}.plist"

if [ "${1:-}" = "--remove" ]; then
    launchctl unload "$PLIST" 2>/dev/null
    launchctl unload "$HEALTH_PLIST" 2>/dev/null
    rm -f "$PLIST" "$HEALTH_PLIST"
    echo "Removed. The scanner will no longer start on its own."
    echo "To also stop waking the Mac:  sudo pmset repeat cancel"
    exit 0
fi

WHEN="${1:-03:30}"
HOUR="${WHEN%%:*}"
MINUTE="${WHEN##*:}"
HOUR=$((10#$HOUR))
MINUTE=$((10#$MINUTE))

# Wake five minutes before the job, so the machine is up when launchd fires.
WAKE_MINUTE=$((MINUTE - 5))
WAKE_HOUR=$HOUR
if [ "$WAKE_MINUTE" -lt 0 ]; then
    WAKE_MINUTE=$((WAKE_MINUTE + 60))
    WAKE_HOUR=$((HOUR - 1))
fi
WAKE=$(printf "%02d:%02d:00" "$WAKE_HOUR" "$WAKE_MINUTE")

mkdir -p "$HOME/Library/LaunchAgents" "$REPO/data/live"

{
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0"><dict>'
    echo "  <key>Label</key><string>${LABEL}</string>"
    echo '  <key>ProgramArguments</key><array>'
    echo '    <string>/bin/bash</string>'
    echo "    <string>${REPO}/start.sh</string>"
    echo '  </array>'
    echo "  <key>WorkingDirectory</key><string>${REPO}</string>"
    echo '  <key>StartCalendarInterval</key><array>'
    for DAY in 1 2 3 4 5; do
        echo "    <dict><key>Weekday</key><integer>${DAY}</integer>"
        echo "          <key>Hour</key><integer>${HOUR}</integer>"
        echo "          <key>Minute</key><integer>${MINUTE}</integer></dict>"
    done
    echo '  </array>'
    echo "  <key>StandardOutPath</key><string>${REPO}/data/live/schedule.log</string>"
    echo "  <key>StandardErrorPath</key><string>${REPO}/data/live/schedule.log</string>"
    echo '  <key>RunAtLoad</key><false/>'
    echo '</dict></plist>'
} > "$PLIST"

launchctl unload "$PLIST" 2>/dev/null
if ! launchctl load "$PLIST"; then
    echo "launchctl load failed. Check $PLIST" >&2
    exit 1
fi

# Health check: pushes only when something is wrong. Without it, a scanner
# that dies mid-morning is indistinguishable from a quiet market.
PYTHON_BIN="$(command -v python3)"
{
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0"><dict>'
    echo "  <key>Label</key><string>${HEALTH_LABEL}</string>"
    echo '  <key>ProgramArguments</key><array>'
    echo "    <string>${PYTHON_BIN}</string>"
    echo '    <string>-m</string><string>tools.healthcheck</string>'
    echo '  </array>'
    echo "  <key>WorkingDirectory</key><string>${REPO}</string>"
    echo '  <key>StartCalendarInterval</key><array>'
    for DAY in 1 2 3 4 5; do
        for CHECK_HOUR in 8 9 11 14; do
            echo "    <dict><key>Weekday</key><integer>${DAY}</integer>"
            echo "          <key>Hour</key><integer>${CHECK_HOUR}</integer>"
            echo "          <key>Minute</key><integer>20</integer></dict>"
        done
    done
    echo '  </array>'
    echo "  <key>StandardOutPath</key><string>${REPO}/data/live/healthcheck.log</string>"
    echo "  <key>StandardErrorPath</key><string>${REPO}/data/live/healthcheck.log</string>"
    echo '  <key>RunAtLoad</key><false/>'
    echo '</dict></plist>'
} > "$HEALTH_PLIST"

launchctl unload "$HEALTH_PLIST" 2>/dev/null
launchctl load "$HEALTH_PLIST" 2>/dev/null

echo ""
echo "Scheduled: weekdays at ${WHEN} local time."
echo "Health checks at 08:20, 09:20, 11:20 and 14:20 — these push ONLY if"
echo "the scanner is down or has stopped receiving data."
echo "  job:   $PLIST"
echo "  log:   ${REPO}/data/live/schedule.log"
echo ""
echo "ONE MORE STEP — the Mac must be awake. Run this once:"
echo ""
echo "    sudo pmset repeat wakeorpoweron MTWRF ${WAKE}"
echo ""
echo "Without it the job waits until you next open the lid, which defeats"
echo "the point. Verify afterwards with:  pmset -g sched"
echo ""
echo "Then leave the Mac plugged in, lid open. To check the schedule is"
echo "registered:  launchctl list | grep ${LABEL}"
