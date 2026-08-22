#!/bin/bash
# Run the nightly study automatically at 16:30 on weekdays.
#
#   ./install-study.sh
#
# Replays the day's candidates against the fixed grid, appends to the
# history, and pushes a one-line summary. Decides nothing.

set -uo pipefail
cd "$(dirname "$0")" || exit 1
REPO="$(pwd)"
PLIST="$HOME/Library/LaunchAgents/com.trading.study.plist"

PYTHON="${PYTHON:-$(command -v python3)}"
[ -x "$PYTHON" ] || PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"

mkdir -p "$HOME/Library/LaunchAgents" data/study

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.trading.study</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON}</string>
    <string>-m</string>
    <string>tools.nightly</string>
  </array>
  <key>WorkingDirectory</key><string>${REPO}</string>
  <key>StandardOutPath</key><string>${REPO}/data/study/study.log</string>
  <key>StandardErrorPath</key><string>${REPO}/data/study/study.log</string>
  <key>StartCalendarInterval</key>
  <array>
$(for d in 1 2 3 4 5; do
    echo "    <dict><key>Weekday</key><integer>${d}</integer>"
    echo "          <key>Hour</key><integer>16</integer>"
    echo "          <key>Minute</key><integer>30</integer></dict>"
  done)
  </array>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST" || { echo "FAILED to load $PLIST" >&2; exit 1; }

echo ""
echo "Nightly study scheduled: weekdays at 16:30."
echo "  log:     ${REPO}/data/study/study.log"
echo "  history: ${REPO}/data/study/history.jsonl"
echo ""
echo "It records and reports. It never edits config."
echo ""
echo "Read it when your phone says a change clears the bar — otherwise"
echo "there is nothing to do."
