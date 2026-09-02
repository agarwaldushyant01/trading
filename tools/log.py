"""Log a trade in one line.

    python -m tools.log GIPR 0.5941 0.97          # a winner
    python -m tools.log OLOX 2.52 2.24            # a loser
    python -m tools.log SIDU 3.55 --open          # still holding
    python -m tools.log --passed BTCT              # saw it, did not take it
    python -m tools.log --show                     # what is recorded

Every line here is fuel for the parameter search. The detector currently
agrees with the trader on 53% of decisions — chance — because fifteen
thresholds were being set by hand against 32 examples. A search over those
thresholds needs labelled trades, and the count is what decides whether the
result means anything: 32 fits noise, 100 becomes meaningful, 300 is solid.

Passes matter as much as trades. A rule that finds every winner is worthless
if it also takes everything else, and without recorded passes there is no way
to measure that.

Dates default to today. Entry and exit are prices, not percentages, because
that is what a broker statement shows and it avoids arithmetic at 4am.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
LOG = pathlib.Path("data/manual/trades.jsonl")


def load() -> list:
    if not LOG.exists():
        return []
    return [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]


def append(row: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def show() -> None:
    rows = load()
    if not rows:
        print("\n  Nothing logged yet.\n")
        return

    taken = [r for r in rows if r["kind"] == "trade"]
    passed = [r for r in rows if r["kind"] == "pass"]
    closed = [r for r in taken if r.get("exit")]

    print(f"\n  {len(taken)} trades, {len(passed)} passes\n")
    print(f"  {'date':<12}{'':<7}{'entry':>9}{'exit':>9}{'result':>9}  note")
    print(f"  {'-' * 62}")
    for r in rows[-25:]:
        if r["kind"] == "pass":
            print(f"  {r['date']:<12}{r['symbol']:<7}{'':>9}{'':>9}"
                  f"{'passed':>9}  {r.get('note', '')}")
            continue
        exit_px = r.get("exit")
        if exit_px:
            pnl = (exit_px / r["entry"] - 1) * 100
            print(f"  {r['date']:<12}{r['symbol']:<7}{r['entry']:>9.4f}"
                  f"{exit_px:>9.4f}{pnl:>+8.1f}%  {r.get('note', '')}")
        else:
            print(f"  {r['date']:<12}{r['symbol']:<7}{r['entry']:>9.4f}"
                  f"{'open':>9}{'':>9}  {r.get('note', '')}")

    if closed:
        rets = [(r["exit"] / r["entry"] - 1) * 100 for r in closed]
        wins = [x for x in rets if x > 0]
        losses = [x for x in rets if x <= 0]
        wr = len(wins) / len(rets)
        exp = (wr * (sum(wins) / len(wins) if wins else 0)
               + (1 - wr) * (sum(losses) / len(losses) if losses else 0))
        print(f"\n  {len(closed)} closed: {len(wins)} up, {len(losses)} down "
              f"({wr * 100:.0f}% win)")
        if wins:
            print(f"    winners {sum(wins) / len(wins):+.1f}%", end="")
        if losses:
            print(f"   losers {sum(losses) / len(losses):+.1f}%", end="")
        print(f"\n    expectancy {exp:+.2f}% per trade")

    # The number that governs whether tuning means anything.
    n = len(closed)
    print(f"\n  Labelled trades for the parameter search: {n}")
    if n < 100:
        print(f"    {100 - n} more before a fitted result is worth trusting.")
    elif n < 300:
        print(f"    Enough to tune. {300 - n} more for a solid answer.")
    else:
        print(f"    Solid sample.")
    print()


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("symbol", nargs="?")
    p.add_argument("entry", nargs="?", type=float)
    p.add_argument("exit", nargs="?", type=float)
    p.add_argument("--date", default=None, help="YYYY-MM-DD, default today")
    p.add_argument("--note", default="", help="why you took or passed it")
    p.add_argument("--open", action="store_true", help="still holding")
    p.add_argument("--passed", action="store_true",
                   help="you saw it and did not take it")
    p.add_argument("--show", action="store_true")
    args = p.parse_args()

    if args.show or not args.symbol:
        show()
        return

    when = args.date or datetime.now(ET).date().isoformat()
    symbol = args.symbol.upper()

    if args.passed:
        append({"kind": "pass", "date": when, "symbol": symbol,
                "note": args.note,
                "logged_at": datetime.now(ET).isoformat()})
        print(f"  passed: {symbol} {when}")
        return

    if args.entry is None:
        raise SystemExit("Need an entry price, or use --passed.")

    row = {"kind": "trade", "date": when, "symbol": symbol,
           "entry": args.entry, "note": args.note,
           "logged_at": datetime.now(ET).isoformat()}
    if args.exit is not None and not args.open:
        row["exit"] = args.exit
        pnl = (args.exit / args.entry - 1) * 100
        print(f"  logged: {symbol} {args.entry:.4f} -> {args.exit:.4f} "
              f"({pnl:+.1f}%)")
    else:
        print(f"  logged: {symbol} {args.entry:.4f} (open)")
    append(row)


if __name__ == "__main__":
    main()
