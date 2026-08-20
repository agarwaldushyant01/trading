"""Is the scanner alive? — pushes only when something is wrong.

    python -m tools.healthcheck

Run on a schedule during market hours. Silent when healthy; a high-priority
push when not.

This exists because absence of alerts is ambiguous. A dead scanner and a
quiet market look identical from a phone, and that ambiguity already cost a
full session. A positive "something is wrong" message removes it.

Checks, in order:
  1. Is a drivers.live process running at all?
  2. Has it received a bar recently? A connected socket delivering nothing
     is as useless as no socket.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from alerts.notify import Notifier

ET = ZoneInfo("America/New_York")
CONSOLE_LOG = pathlib.Path("data/live/console.log")

# IEX does not open until 08:00, so a check before then would fire every day
# for a reason that is not a fault.
MARKET_OPEN = time(8, 15)
MARKET_CLOSE = time(16, 0)

# A heartbeat prints every 5 minutes, so nothing for 20 means trouble.
STALE_MINUTES = 20


def process_running() -> bool:
    result = subprocess.run(["pgrep", "-f", "drivers.live"],
                            capture_output=True, text=True)
    return bool(result.stdout.strip())


def last_heartbeat() -> datetime | None:
    """Most recent heartbeat time from the console log.

    Heartbeat lines look like:  [09:35] 2,140 bars received, last 2s ago, ...
    """
    if not CONSOLE_LOG.exists():
        return None

    tail = CONSOLE_LOG.read_text(errors="ignore").splitlines()[-400:]
    today = datetime.now(ET).date()
    latest = None

    for line in tail:
        match = re.search(r"\[(\d{2}):(\d{2})\]\s+[\d,]+ bars received", line)
        if match:
            latest = datetime.combine(
                today, time(int(match.group(1)), int(match.group(2))), tzinfo=ET
            )
    return latest


def main() -> None:
    now = datetime.now(ET)

    if now.weekday() > 4 or not (MARKET_OPEN <= now.time() <= MARKET_CLOSE):
        print(f"{now:%H:%M} outside market hours, not checking.")
        return

    cfg_path = pathlib.Path("config/alerts.yaml")
    cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    notifier = Notifier(cfg.get("alerts", {}))

    if not process_running():
        print(f"{now:%H:%M} FAIL: no scanner process")
        notifier.send(
            "SCANNER IS DOWN",
            f"No scanner process at {now:%H:%M} ET.\n"
            f"Nothing is watching the market.\n"
            f"Fix: cd to the repo and run ./start.sh",
            priority="urgent",
        )
        sys.exit(1)

    beat = last_heartbeat()
    if beat is None:
        print(f"{now:%H:%M} FAIL: running but no heartbeat yet")
        notifier.send(
            "SCANNER NOT RECEIVING DATA",
            f"Process is running but no bars have arrived by {now:%H:%M} ET.\n"
            f"The websocket is connected to nothing useful.",
            priority="urgent",
        )
        sys.exit(1)

    age = (now - beat).total_seconds() / 60
    if age > STALE_MINUTES:
        print(f"{now:%H:%M} FAIL: last heartbeat {age:.0f} min ago")
        notifier.send(
            "SCANNER STALLED",
            f"Last data {age:.0f} minutes ago (at {beat:%H:%M}).\n"
            f"The stream has probably dropped. Restart with ./start.sh",
            priority="urgent",
        )
        sys.exit(1)

    print(f"{now:%H:%M} OK: heartbeat {age:.0f} min ago")


if __name__ == "__main__":
    main()
