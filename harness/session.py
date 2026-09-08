"""Assert what must be true of a whole session.

    python -m harness.session --date 2026-09-08
    python -m harness.session --date 2026-09-08 --verbose

The five checks in harness.replay each reproduce one bug that already
happened. They are a regression suite: they prove old defects stay fixed and
say nothing about new ones.

Every defect this fortnight was temporal, and none was a component failure:

    the loss cap comparing against a baseline that rolls at midnight
    exits submitted as market orders when they could not fill until 09:30
    a flatten at 15:50 on a day the market closes at 13:00
    entries at 15:51, 15:56, 15:59 and 16:01, each closed seconds later

They share a shape. The code was written from a picture of a function and its
inputs, not of a session and its clock — so it was right at the moment being
imagined and wrong at 04:00, or 15:51, or on Christmas Eve.

This asserts properties of the DAY. "No trade shorter than two minutes" and
"nothing opened within 45 minutes of the flatten" cannot be written without
thinking about the shape of a session, which is exactly the thinking that was
missing.

Run against a recorded day, it either passes or names the rule broken and the
trades that broke it.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ET = ZoneInfo("America/New_York")


@dataclass
class Violation:
    rule: str
    detail: str
    symbol: str = ""
    at: str = ""


def load_day(day: str) -> tuple[list, list]:
    """Entries and exits the bot recorded for one session."""
    path = pathlib.Path("data/mosquito/trades.jsonl")
    if not path.exists():
        return ([], [])

    entries, exits = [], []
    for line in path.read_text().splitlines():
        if not line.strip() or day not in line:
            continue
        try:
            r = json.loads(line)
        except Exception:                                 # noqa: BLE001
            continue
        if not r.get("at", "").startswith(day):
            continue
        if r.get("kind") == "entry":
            entries.append(r)
        elif r.get("kind") == "exit":
            exits.append(r)
    return (entries, exits)


def when(row: dict) -> datetime:
    return datetime.fromisoformat(row["at"])


# ------------------------------------------------------------------ rules

def rule_no_late_entries(entries, exits, cfg, day) -> list:
    """Nothing opened close to the flatten.

    A position opened at 15:51 has nine minutes to live and is closed by the
    time rule before it can do anything except pay the spread.
    """
    from tools.calendar import flatten_time

    flatten = flatten_time(day, cfg["execution"]["hard_exit_time"])
    buffer_min = cfg["execution"].get("no_entry_before_close_min", 45)
    cutoff_minutes = flatten.hour * 60 + flatten.minute - buffer_min

    out = []
    for e in entries:
        t = when(e)
        if t.hour * 60 + t.minute >= cutoff_minutes:
            out.append(Violation(
                "no entries near the close",
                f"opened at {t:%H:%M}, flatten is {flatten:%H:%M}",
                e["symbol"], f"{t:%H:%M:%S}"))
    return out


def rule_no_flash_trades(entries, exits, cfg, day) -> list:
    """No trade may live for less than two minutes.

    A position that opens and closes inside a minute has not been traded, it
    has been churned. Six of these happened on 2026-09-08 and each one paid
    the spread twice for nothing.
    """
    opened = {e["symbol"]: when(e) for e in entries}
    out = []
    for x in exits:
        start = opened.get(x["symbol"])
        if start is None:
            continue
        held = (when(x) - start).total_seconds()
        if held < 120:
            out.append(Violation(
                "no trade under two minutes",
                f"held {held:.0f}s, exited on {x.get('exit_reason')}",
                x["symbol"], f"{when(x):%H:%M:%S}"))
    return out


def rule_everything_flat(entries, exits, cfg, day) -> list:
    """Every position opened must be closed by the flatten."""
    from tools.calendar import flatten_time

    flatten = flatten_time(day, cfg["execution"]["hard_exit_time"])
    closed = {x["symbol"] for x in exits}
    out = []
    for e in entries:
        if e["symbol"] not in closed:
            out.append(Violation(
                "everything flat by the close",
                f"opened at {when(e):%H:%M} and never exited",
                e["symbol"]))

    for x in exits:
        t = when(x)
        if t.time() > time(flatten.hour, flatten.minute + 5
                           if flatten.minute < 55 else 59):
            out.append(Violation(
                "everything flat by the close",
                f"exited at {t:%H:%M}, after the {flatten:%H:%M} flatten",
                x["symbol"], f"{t:%H:%M:%S}"))
    return out


def rule_position_limit(entries, exits, cfg, day) -> list:
    """Concurrent positions must never exceed the configured limit.

    Reconstructed by walking the day's opens and closes in order, which
    catches an overshoot that lasted seconds and was gone before anyone
    looked at the broker.
    """
    limit = cfg["risk"]["max_concurrent"]
    events = ([(when(e), 1, e["symbol"]) for e in entries]
              + [(when(x), -1, x["symbol"]) for x in exits])
    events.sort(key=lambda ev: ev[0])

    out = []
    held = 0
    peak_reported = False
    for t, delta, symbol in events:
        held += delta
        if held > limit and not peak_reported:
            out.append(Violation(
                "position limit",
                f"{held} concurrent against a limit of {limit}",
                symbol, f"{t:%H:%M:%S}"))
            peak_reported = True
    return out


def rule_stops_are_respected(entries, exits, cfg, day) -> list:
    """An exit on a stop must fill near the stop.

    A stop that fills far below its level did not work: on 2026-09-03 SDST's
    stop was 0.2818 and the shares left at 0.2150, because a market order
    submitted in premarket could not fill until the open.
    """
    floor = cfg["execution"].get("min_stop_pct", 5.0)
    out = []
    for x in exits:
        if x.get("exit_reason") != "stop":
            continue
        stop = x.get("stop")
        fill = x.get("exit_price")
        if not stop or not fill:
            continue
        slip = (fill / stop - 1) * 100
        if slip < -3.0:
            out.append(Violation(
                "stops fill near their level",
                f"stop {stop:.4f}, filled {fill:.4f} ({slip:+.1f}%)",
                x["symbol"], f"{when(x):%H:%M:%S}"))
    return out


def rule_every_entry_has_an_exit_record(entries, exits, cfg, day) -> list:
    """The journal must record what actually happened.

    Exits went unlogged on several sessions, so the journal reported intended
    prices while the broker filled elsewhere — which is how the premarket
    stop failure stayed invisible for three days.
    """
    closed = {x["symbol"] for x in exits}
    missing = [e for e in entries if e["symbol"] not in closed]
    if not missing:
        return []
    return [Violation(
        "every entry has an exit record",
        f"{len(missing)} entries with no exit logged: "
        f"{', '.join(e['symbol'] for e in missing[:5])}")]


RULES = [
    ("no entries near the close", rule_no_late_entries),
    ("no trade under two minutes", rule_no_flash_trades),
    ("everything flat by the close", rule_everything_flat),
    ("position limit", rule_position_limit),
    ("stops fill near their level", rule_stops_are_respected),
    ("every entry has an exit record", rule_every_entry_has_an_exit_record),
]


def main() -> None:
    import argparse

    import yaml

    p = argparse.ArgumentParser()
    p.add_argument("--date", default=date.today().isoformat())
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    day = date.fromisoformat(args.date)
    cfg = yaml.safe_load(pathlib.Path("config/rules.yaml").read_text())
    entries, exits = load_day(args.date)

    print(f"\n{'=' * 70}")
    print(f"  SESSION ASSERTIONS — {args.date}")
    print(f"{'=' * 70}\n")

    if not entries:
        print(f"  No entries recorded. Nothing to assert.\n")
        return

    print(f"  {len(entries)} entries, {len(exits)} exits\n")

    total = 0
    for name, rule in RULES:
        try:
            violations = rule(entries, exits, cfg, day)
        except Exception as exc:                          # noqa: BLE001
            print(f"  [ERROR] {name}: {type(exc).__name__}: {exc}")
            total += 1
            continue

        if not violations:
            print(f"  [PASS]  {name}")
            continue

        total += len(violations)
        print(f"  [FAIL]  {name} — {len(violations)} violation(s)")
        for v in violations[:6 if not args.verbose else 100]:
            where = f"{v.symbol} {v.at}".strip()
            print(f"            {where:<18} {v.detail}")
        if len(violations) > 6 and not args.verbose:
            print(f"            ... and {len(violations) - 6} more "
                  f"(--verbose for all)")

    print()
    if total:
        print(f"  {total} violation(s). These are properties of the session,")
        print(f"  not of any single function — which is why component tests")
        print(f"  passed while every one of them shipped.\n")
        sys.exit(1)
    print(f"  Session clean.\n")


if __name__ == "__main__":
    main()
