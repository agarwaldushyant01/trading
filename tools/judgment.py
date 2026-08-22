"""Did your judgment help?

    python -m tools.judgment
    python -m tools.judgment --date 2026-08-21

Joins the approval log to the trade journal and answers two questions:

  1. What did the trades you approved actually do?
  2. What would the ones you skipped or let expire have done?

The second is the harder and more interesting one. A gate that only ever
approves winners is worth keeping; one whose rejections would have done just
as well is only costing you attention. Nothing else in this project can
answer that, because it requires knowing the outcome of trades that were
never taken — reconstructed here from what the price did afterwards.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from data.reference import load_credentials

ET = ZoneInfo("America/New_York")


def load(path: str, day: str | None) -> list[dict]:
    p = pathlib.Path(path)
    if not p.exists():
        return []
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    if day:
        rows = [r for r in rows if r.get("at", "").startswith(day)]
    return rows


def outcome_after(symbol: str, when: datetime, entry: float,
                  stop: float, target: float) -> tuple[str, float]:
    """What a trade would have done, from the minute bars after entry.

    Walks forward through the session applying the same stop, target and
    15:50 exit the live trader uses, so a skipped trade is measured by the
    rules it would actually have been managed under — not by its best or
    final price.
    """
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    key, secret = load_credentials()
    client = StockHistoricalDataClient(key, secret)

    close_time = when.replace(hour=15, minute=50, second=0, microsecond=0)
    if when >= close_time:
        return ("no time", 0.0)

    try:
        bars = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=[symbol], timeframe=TimeFrame.Minute,
            start=when, end=close_time, feed=DataFeed.SIP)).data.get(symbol, [])
    except Exception:                                     # noqa: BLE001
        return ("no data", 0.0)

    if not bars:
        return ("no data", 0.0)

    for bar in bars:
        # Stop takes precedence when a bar spans both — the pessimistic
        # assumption, and the one that matches the backtest engine.
        if float(bar.low) <= stop:
            return ("stop", (stop / entry - 1) * 100)
        if float(bar.high) >= target:
            return ("target", (target / entry - 1) * 100)

    last = float(bars[-1].close)
    return ("time", (last / entry - 1) * 100)


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--date", default=None, help="YYYY-MM-DD, default all")
    p.add_argument("--no-counterfactual", action="store_true",
                   help="skip the what-if pass (avoids the data fetches)")
    args = p.parse_args()

    approvals = load("data/mosquito/approvals.jsonl", args.date)
    trades = load("data/mosquito/trades.jsonl", args.date)
    exits = {r["symbol"]: r for r in trades if r["kind"] == "exit"}

    if not approvals:
        print("No approval decisions recorded"
              f"{' for ' + args.date if args.date else ''}.")
        return

    taken = [r for r in approvals if r["approved"]]
    passed = [r for r in approvals if not r["approved"]]

    print(f"\n{'=' * 70}")
    print(f"  JUDGMENT REPORT — {args.date or 'all sessions'}")
    print(f"{'=' * 70}")

    # --- what you took --------------------------------------------------
    print(f"\nAPPROVED ({len(taken)})")
    if not taken:
        print("  none")
    results = []
    for r in taken:
        exit_row = exits.get(r["symbol"])
        if exit_row:
            pnl = exit_row["pnl_pct"]
            dollars = pnl / 100 * r["shares"] * r["price"]
            results.append(pnl)
            print(f"  {r['symbol']:<6} {r['pct_change']:>+7.1f}% at entry  "
                  f"->  {pnl:>+6.1f}%  ${dollars:>+9,.0f}  "
                  f"({exit_row['exit_reason']})")
        else:
            print(f"  {r['symbol']:<6} {r['pct_change']:>+7.1f}% at entry  "
                  f"->  no exit recorded")

    if results:
        wins = [x for x in results if x > 0]
        total = sum(r_["pnl_pct"] / 100 * a["shares"] * a["price"]
                    for a in taken if (r_ := exits.get(a["symbol"])))
        print(f"\n  {len(wins)} of {len(results)} profitable   "
              f"average {mean(results):+.1f}%   total ${total:+,.0f}")

    # --- what you passed on ---------------------------------------------
    print(f"\nNOT TAKEN ({len(passed)})")
    by_reason = defaultdict(list)
    for r in passed:
        by_reason[r["resolved_by"]].append(r)
    for reason, rows in by_reason.items():
        label = "you rejected" if reason == "manual" else "expired unanswered"
        print(f"  {len(rows):>3} {label}")

    if args.no_counterfactual:
        print("\n  (skipped the what-if pass)")
        return

    print(f"\n  what they would have done, under the same stops and targets:")
    print(f"  {'':<8}{'entry %':>9}{'outcome':>10}{'result':>9}")

    would = []
    for r in passed:
        when = datetime.fromisoformat(r["at"]).astimezone(ET)
        how, pnl = outcome_after(r["symbol"], when, r["price"],
                                 r["stop"], r["target"])
        if how in ("no data", "no time"):
            continue
        would.append(pnl)
        print(f"  {r['symbol']:<8}{r['pct_change']:>+8.1f}%{how:>10}"
              f"{pnl:>+8.1f}%")

    if would:
        wins = [x for x in would if x > 0]
        print(f"\n  {len(wins)} of {len(would)} would have been profitable   "
              f"average {mean(would):+.1f}%")

    # --- the comparison --------------------------------------------------
    if results and would:
        print(f"\n{'-' * 70}")
        print(f"  approved:   {mean(results):+.1f}% average over {len(results)}")
        print(f"  passed on:  {mean(would):+.1f}% average over {len(would)}")
        gap = mean(results) - mean(would)
        print()
        if gap > 2:
            print(f"  Your picks beat the ones you passed by {gap:.1f} points.")
            print(f"  On this sample the gate is adding something.")
        elif gap < -2:
            print(f"  The ones you passed on did {-gap:.1f} points BETTER.")
            print(f"  On this sample the gate is costing you.")
        else:
            print(f"  No meaningful difference ({gap:+.1f} points).")
            print(f"  On this sample the gate is not distinguishing much.")
        print()
        print(f"  Sample is small either way — read it as a first signal,")
        print(f"  not a verdict.")
    print()


if __name__ == "__main__":
    main()
