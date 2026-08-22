"""When to enter, and which candidates are worth entering at all.

    python -m tools.entry_study
    python -m tools.entry_study --stop 8 --target 15

Two questions, one dataset — the 147 real candidates from the live sessions,
using the bars already cached by tools.exit_sweep.

  1. WHEN to enter. Buying the bar after the alert loses money at every
     stop/target combination tested. These are mechanical versions of what a
     discretionary trader does instead: wait for the first pullback, wait for
     the pullback to be reclaimed, wait for a red bar then buy the green one.

  2. WHICH to enter. Split the same candidates by outcome and compare their
     features. If percent-change at entry, or relative volume, or float
     turnover separates winners from losers, that is a filter — and it is
     testable here rather than over another month of live sessions.

"Wait for a pullback" sounds like judgment but is not: a pullback is a
measurable retracement from the high made after the alert. What is genuinely
hard to mechanise is reading whether a chart looks right, and this does not
attempt that.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from statistics import mean, median
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.exit_sweep import Candidate, bars_for, collect

ET = ZoneInfo("America/New_York")


# --------------------------------------------------------------- entry rules

def entry_immediate(bars: list, alert_price: float) -> tuple[int, float] | None:
    """Buy the first bar after the alert. The current behaviour."""
    return (0, alert_price) if bars else None


def entry_pullback(bars: list, alert_price: float, pct: float = 5.0,
                   window: int = 20) -> tuple[int, float] | None:
    """Wait for a retracement of pct% from the running high, then buy.

    The mechanical form of "let it come back to me". Buying strength after a
    vertical move means paying the top of the range; this waits for the first
    pause. Many candidates never pull back — those are simply skipped, which
    is itself a filter.
    """
    high = alert_price
    for i, bar in enumerate(bars[:window]):
        high = max(high, bar["h"])
        trigger = high * (1 - pct / 100)
        if bar["l"] <= trigger:
            return (i, trigger)
    return None


def entry_reclaim(bars: list, alert_price: float, pct: float = 5.0,
                  window: int = 30) -> tuple[int, float] | None:
    """Wait for a pullback, then buy only when the prior high is reclaimed.

    Stricter than buying the dip: it demands the pullback be bought by
    someone else first. Catches fewer trades, but never enters a name that
    simply keeps falling.
    """
    high = alert_price
    pulled_back = False
    for i, bar in enumerate(bars[:window]):
        if not pulled_back:
            high = max(high, bar["h"])
            if bar["l"] <= high * (1 - pct / 100):
                pulled_back = True
                continue
        elif bar["h"] >= high:
            return (i, high)
    return None


def entry_red_then_green(bars: list, alert_price: float,
                         window: int = 20) -> tuple[int, float] | None:
    """Wait for one down bar, buy the close of the next up bar.

    The crudest possible "wait for it to settle". Included as a control: if
    it performs like the more elaborate rules, the benefit is simply from
    waiting, not from anything clever.
    """
    prev = alert_price
    saw_red = False
    for i, bar in enumerate(bars[:window]):
        if bar["c"] < prev:
            saw_red = True
        elif saw_red and bar["c"] > prev:
            return (i, bar["c"])
        prev = bar["c"]
    return None


ENTRY_RULES = {
    "immediate":        lambda b, p: entry_immediate(b, p),
    "pullback 3%":      lambda b, p: entry_pullback(b, p, 3.0),
    "pullback 5%":      lambda b, p: entry_pullback(b, p, 5.0),
    "pullback 8%":      lambda b, p: entry_pullback(b, p, 8.0),
    "reclaim after 5%": lambda b, p: entry_reclaim(b, p, 5.0),
    "red then green":   lambda b, p: entry_red_then_green(b, p),
}


def run_after_entry(bars: list, start: int, entry: float,
                    stop_pct: float, target_pct: float) -> float:
    """Outcome from the entry bar onward, stop taking precedence."""
    stop = entry * (1 - stop_pct / 100)
    target = entry * (1 + target_pct / 100)
    for bar in bars[start + 1:]:
        if bar["l"] <= stop:
            return -stop_pct
        if bar["h"] >= target:
            return target_pct
    if len(bars) > start + 1:
        return (bars[-1]["c"] / entry - 1) * 100
    return 0.0


# ------------------------------------------------------------------ features

def load_features() -> dict:
    """Feature values per candidate, keyed by symbol and alert minute."""
    out = {}
    path = pathlib.Path("data/mosquito/approvals.jsonl")
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            key = (r["symbol"], r["at"][:16])
            out[key] = {
                "pct_change": r.get("pct_change"),
                "rel_volume_1m": r.get("rel_volume_1m"),
                "float_turnover": r.get("float_turnover"),
                "setup": r.get("setup"),
            }

    path = pathlib.Path("data/mosquito/trades.jsonl")
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r["kind"] != "skip":
                continue
            key = (r["symbol"], r["at"][:16])
            out.setdefault(key, {
                "pct_change": r.get("pct_change"),
                "rel_volume_1m": r.get("rel_volume_1m"),
                "float_turnover": r.get("float_turnover"),
                "setup": r.get("setup"),
            })
    return out


def describe(name: str, winners: list, losers: list) -> None:
    w = [x for x in winners if x is not None]
    l = [x for x in losers if x is not None]
    if len(w) < 5 or len(l) < 5:
        return
    mw, ml = median(w), median(l)
    ratio = mw / ml if ml else float("inf")
    flag = "  <-- separates" if (ratio > 1.5 or ratio < 0.67) else ""
    print(f"  {name:<18} winners {mw:>10.2f}   losers {ml:>10.2f}{flag}")


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--date", default=None)
    p.add_argument("--stop", type=float, default=8.0)
    p.add_argument("--target", type=float, default=15.0)
    args = p.parse_args()

    candidates = collect(args.date)
    loaded = []
    for c in candidates:
        bars = bars_for(c)
        if len(bars) > 3:
            loaded.append((c, bars))

    if not loaded:
        print("No cached bars. Run tools.exit_sweep first.")
        return

    print(f"\n{'=' * 68}")
    print(f"  ENTRY STUDY — {len(loaded)} candidates, "
          f"{args.stop:.0f}% stop / {args.target:.0f}% target")
    print(f"{'=' * 68}")

    # --- 1. when to enter -------------------------------------------------
    print(f"\nWHEN TO ENTER\n")
    print(f"  {'rule':<20}{'taken':>7}{'win%':>7}{'avg':>9}{'total':>10}")
    print(f"  {'-' * 51}")

    best_rule, best_avg = None, -999
    for name, rule in ENTRY_RULES.items():
        results = []
        for c, bars in loaded:
            found = rule(bars, c.entry)
            if found is None:
                continue                    # rule never triggered: no trade
            idx, price = found
            results.append(run_after_entry(bars, idx, price,
                                           args.stop, args.target))
        if not results:
            continue
        wins = [x for x in results if x > 0]
        avg = mean(results)
        if avg > best_avg:
            best_rule, best_avg = name, avg
        print(f"  {name:<20}{len(results):>7}"
              f"{len(wins) / len(results) * 100:>6.0f}%"
              f"{avg:>+9.2f}{sum(results):>+10.0f}")

    print(f"\n  Best: {best_rule} at {best_avg:+.2f}% per trade")
    if best_avg <= 0:
        print(f"  Still negative. Waiting does not rescue these entries.")
    else:
        print(f"  Positive — but chosen by looking at this same sample.")

    # --- 2. which to enter ------------------------------------------------
    print(f"\n\nWHICH TO ENTER  (immediate entry, split by outcome)\n")

    features = load_features()
    winners, losers = defaultdict(list), defaultdict(list)
    setup_split = defaultdict(lambda: [0, 0])

    for c, bars in loaded:
        pnl = run_after_entry(bars, 0, c.entry, args.stop, args.target)
        key = (c.symbol, c.at.isoformat()[:16])
        f = features.get(key, {})
        bucket = winners if pnl > 0 else losers
        for field in ("pct_change", "rel_volume_1m", "float_turnover"):
            bucket[field].append(f.get(field))
        if f.get("setup"):
            setup_split[f["setup"]][0 if pnl > 0 else 1] += 1

    print(f"  median feature values:\n")
    describe("percent change", winners["pct_change"], losers["pct_change"])
    describe("rel volume 1m", winners["rel_volume_1m"], losers["rel_volume_1m"])
    describe("float turnover", winners["float_turnover"],
             losers["float_turnover"])

    if setup_split:
        print(f"\n  by setup:\n")
        for setup, (w, l) in sorted(setup_split.items()):
            total = w + l
            if total >= 5:
                print(f"  {setup:<18} {w:>3}/{total:<4} "
                      f"({w / total * 100:.0f}% profitable)")

    print(f"\n  A feature only helps if the two medians differ by enough to")
    print(f"  filter on. Similar numbers mean it carries no information about")
    print(f"  the outcome, whatever it does for finding candidates.\n")


if __name__ == "__main__":
    main()
