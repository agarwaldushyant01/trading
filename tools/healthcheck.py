"""Check the trader, and fix it. Do not ask.

    python -m tools.healthcheck              # check, restart if needed
    python -m tools.healthcheck --dry-run    # report only

Runs every 15 minutes during market hours. If the trader is dead or has
stopped receiving data, it restarts the launchd job and sends a note saying
what it did — not a question.

This used to only warn. That was useless twice over: it needed someone to
read the alert and act on it, and until 24 August the restart path it would
have pointed at was itself broken. A warning nobody can act on is worse than
nothing, because it looks like coverage.

Two states count as failure:

  DEAD    — no process. launchd's KeepAlive should catch this, so seeing it
            here means KeepAlive did not fire, which is itself worth knowing.

  STALLED — process alive but no heartbeat for longer than the threshold.
            KeepAlive cannot see this: the process is running perfectly, just
            not receiving anything. This is the case this file exists for.

Restarts are rate-limited. A restart loop against a real outage — expired
credentials, a feed down — would produce dozens of notifications and fix
nothing, so after a few attempts it stops and says so once.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ET = ZoneInfo("America/New_York")
CONSOLE = pathlib.Path("data/live/console.log")
STATE = pathlib.Path("data/live/healthcheck-state.json")
JOB = "com.trading.scanner"

SESSION_START = time(4, 0)
SESSION_END = time(20, 0)
STALL_MINUTES = 20          # premarket can be genuinely quiet
MAX_RESTARTS_PER_DAY = 6
MIN_MINUTES_BETWEEN = 10

# A status line every half hour, whether or not anything is wrong. Sent at
# low priority so it lands silently in the notification list: the point is a
# scrollable record you can glance at, not an interruption. Without it,
# silence is ambiguous — a healthy scanner and a dead phone look identical.
STATUS_EVERY_MINUTES = 30

HEARTBEAT = re.compile(r"\[(\d{2}):(\d{2})\]\s+([\d,]+) bars")


def in_session(now: datetime) -> bool:
    return now.weekday() < 5 and SESSION_START <= now.time() <= SESSION_END


def process_alive() -> bool:
    return subprocess.run(["pgrep", "-f", "drivers.paper_live"],
                          capture_output=True).returncode == 0


def last_heartbeat(now: datetime) -> datetime | None:
    """Timestamp of the most recent heartbeat line.

    Heartbeats carry only HH:MM, so the date comes from today, and a time in
    the future is read as yesterday's.
    """
    if not CONSOLE.exists():
        return None
    try:
        tail = CONSOLE.read_text(errors="replace").splitlines()[-400:]
    except Exception:                                     # noqa: BLE001
        return None

    for line in reversed(tail):
        m = HEARTBEAT.search(line)
        if not m:
            continue
        hh, mm = int(m.group(1)), int(m.group(2))
        stamp = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if stamp > now + timedelta(minutes=5):
            stamp -= timedelta(days=1)
        return stamp
    return None


def todays_trades(today: str) -> tuple[list, list]:
    """Entries and exits recorded so far today."""
    path = pathlib.Path("data/mosquito/trades.jsonl")
    if not path.exists():
        return ([], [])
    entries, exits = [], []
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip() or today not in line:
            continue
        try:
            r = json.loads(line)
        except Exception:                                 # noqa: BLE001
            continue
        if r.get("at", "").startswith(today):
            if r.get("kind") == "entry":
                entries.append(r)
            elif r.get("kind") == "exit":
                exits.append(r)
    return (entries, exits)


def account_snapshot() -> str:
    """Equity and open positions, straight from the broker."""
    try:
        from alpaca.trading.client import TradingClient
        from data.reference import load_credentials
        key, secret = load_credentials()
        client = TradingClient(key, secret, paper=True)
        acct = client.get_account()
        positions = client.get_all_positions()
        equity = float(acct.equity)
        start = float(acct.last_equity)
        change = equity - start
        line = f"${equity:,.0f} ({change:+,.0f} today), {len(positions)} open"
        if positions:
            line += "\n" + "\n".join(
                f"  {p.symbol} {float(p.unrealized_plpc) * 100:+.1f}%"
                for p in positions[:4])
        return line
    except Exception as exc:                              # noqa: BLE001
        return f"broker unreachable ({type(exc).__name__})"


def send_status(now: datetime, state: dict, healthy: bool,
                problem: str | None) -> None:
    """The half-hourly line: is it alive, and what has it done today."""
    last = state.get("last_status")
    if last:
        since = (now - datetime.fromisoformat(last)).total_seconds() / 60
        if since < STATUS_EVERY_MINUTES - 1:
            return

    today = now.date().isoformat()
    entries, exits = todays_trades(today)
    beat = last_heartbeat(now)
    age = f"{(now - beat).total_seconds() / 60:.0f}m ago" if beat else "none yet"

    if healthy:
        headline = f"Running · {len(entries)} bought, {len(exits)} sold"
    else:
        headline = f"PROBLEM · {problem}"

    body = [f"Last data: {age}", account_snapshot()]

    recent = sorted(entries + exits, key=lambda r: r["at"])[-4:]
    if recent:
        body.append("")
        for r in recent:
            when = r["at"][11:16]
            if r["kind"] == "entry":
                body.append(f"{when} BUY  {r['symbol']} x{r['shares']:,} "
                            f"@ {r['signal_price']:.2f}")
            else:
                body.append(f"{when} SELL {r['symbol']} "
                            f"{r.get('pnl_pct', 0):+.1f}% ({r.get('exit_reason','')})")
    else:
        body.append("")
        body.append("No trades yet today.")

    notify(f"{now:%H:%M} {headline}", "\n".join(body), priority="low")
    state["last_status"] = now.isoformat()
    save_state(state)


def load_state(today: str) -> dict:
    if STATE.exists():
        try:
            s = json.loads(STATE.read_text())
            if s.get("date") == today:
                return s
        except Exception:                                 # noqa: BLE001
            pass
    return {"date": today, "restarts": 0, "last_restart": None,
            "gave_up": False, "last_status": None}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))


def restart() -> tuple[bool, str]:
    """Restart the launchd job. kickstart -k kills and relaunches in one step.

    Falls back to unload/load for older launchd.
    """
    uid = os.getuid()
    for domain in ("gui", "user"):
        cmd = ["launchctl", "kickstart", "-k", f"{domain}/{uid}/{JOB}"]
        if subprocess.run(cmd, capture_output=True).returncode == 0:
            return (True, f"kickstart {domain}")

    plist = pathlib.Path.home() / f"Library/LaunchAgents/{JOB}.plist"
    if plist.exists():
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        r = subprocess.run(["launchctl", "load", str(plist)], capture_output=True)
        if r.returncode == 0:
            subprocess.run(["launchctl", "start", JOB], capture_output=True)
            return (True, "unload/load")

    return (False, "no working restart path")


def notify(title: str, body: str, priority: str = "high") -> None:
    try:
        import yaml
        from alerts.notify import Notifier
        p = pathlib.Path("config/alerts.yaml")
        cfg = yaml.safe_load(p.read_text()) if p.exists() else {}
        Notifier(cfg.get("alerts", {})).send(title, body, priority=priority)
    except Exception as exc:                              # noqa: BLE001
        print(f"  could not notify: {exc}", file=sys.stderr)


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    now = datetime.now(ET)
    if not in_session(now):
        print(f"{now:%H:%M} outside market hours, not checking.")
        return

    state = load_state(now.date().isoformat())
    alive = process_alive()
    beat = last_heartbeat(now)
    stale_for = (now - beat).total_seconds() / 60 if beat else None

    if alive and stale_for is not None and stale_for < STALL_MINUTES:
        print(f"{now:%H:%M} healthy — last heartbeat {stale_for:.0f}m ago.")
        if not args.dry_run:
            send_status(now, state, healthy=True, problem=None)
        return

    if alive and beat is None and now.time() < time(4, 30):
        print(f"{now:%H:%M} alive, no heartbeat yet (just started).")
        if not args.dry_run:
            send_status(now, state, healthy=True, problem="starting up")
        return

    if not alive:
        problem = "process is not running"
    elif stale_for is None:
        problem = "no heartbeat found in the log"
    else:
        problem = f"no data for {stale_for:.0f} minutes"

    print(f"{now:%H:%M} PROBLEM: {problem}")

    if args.dry_run:
        print("  dry run, not restarting.")
        return

    send_status(now, state, healthy=False, problem=problem)

    if state["gave_up"]:
        print("  already gave up today, not retrying.")
        return

    if state["restarts"] >= MAX_RESTARTS_PER_DAY:
        state["gave_up"] = True
        save_state(state)
        notify("Scanner keeps failing",
               f"Restarted {state['restarts']} times today and it will not "
               f"stay up ({problem}). Not trying again — this needs a look.",
               priority="urgent")
        print("  restart limit reached; giving up for today.")
        return

    if state["last_restart"]:
        since = (now - datetime.fromisoformat(
            state["last_restart"])).total_seconds() / 60
        if since < MIN_MINUTES_BETWEEN:
            print(f"  restarted {since:.0f}m ago, waiting.")
            return

    ok, how = restart()
    state["restarts"] += 1
    state["last_restart"] = now.isoformat()
    save_state(state)

    if ok:
        print(f"  restarted via {how} (attempt {state['restarts']} today)")
        notify("Scanner restarted",
               f"It was down ({problem}). Restarted automatically — attempt "
               f"{state['restarts']} today. Nothing needed from you.",
               priority="default")
    else:
        state["gave_up"] = True
        save_state(state)
        print(f"  RESTART FAILED: {how}", file=sys.stderr)
        notify("Scanner down, restart failed",
               f"{problem}, and the automatic restart did not work ({how}). "
               f"This one needs a person.", priority="urgent")


if __name__ == "__main__":
    main()
