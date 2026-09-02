"""Does letting winners run rescue a mediocre entry?

    python -m tools.hold_test

Reuses the bars already cached by tools.backtest_reclaim. No new fetching.

THE QUESTION

Every mechanical entry tested so far lands at a 25-30% hit rate against the
50% of the manual log. But hit rate is only half of expectancy, and the
manual trades made their money on payoff, not accuracy: +47.4% average
winner against -11.9% average loser, 4:1.

At 25% accuracy with that payoff:  0.25(47) + 0.75(-12) = +3.0% per trade.
At 25% accuracy with 15%/-8%:      0.25(15) + 0.75(-8)  = -2.3% per trade.

So the same entries can be profitable or not depending entirely on whether
winners are allowed to run. And the previous backtest never allowed it: 62
of 107 trades exited on a 45-minute give-up before the trail could do
anything, which is why winners averaged +1.8% instead of anything like +47%.

This strips the give-up out and holds to the stop, the trail, or the close.

WHAT A POSITIVE RESULT WOULD AND WOULD NOT MEAN

Would mean: automation is viable without matching anyone's judgment — take
more trades at a worse hit rate and let the payoff carry it.

Would not mean: it is safe to trade. The universe still excludes delisted
names, fills are still assumed at the bar close, and holding through a
drawdown is far harder to live with than a spreadsheet suggests.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections import defaultdict
from statistics import mean, stdev

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

CACHE = pathlib.Path("data/bars/reclaim")


def load_cached() -> list:
    """Every cached session, as (symbol, day, bars)."""
    out = []
    if not CACHE.exists():
        return out
    for path in sorted(CACHE.glob("*.json")):
        try:
            bars = json.loads(path.read_text())
        except Exception:                                 # noqa: BLE001
            continue
        if len(bars) < 40:
            continue
        stem = path.stem
        symbol, day = stem.rsplit("-", 3)[0], "-".join(stem.rsplit("-", 3)[1:])
        out.append((symbol, day, bars))
    return out


def find_entry(bars: list, min_bars_below: int = 15,
               min_off_high: float = 8.0,
               min_vol_ratio: float = 2.0) -> int | None:
    """First reclaim signal in the session. Same rule as before."""
    cum_pv = cum_v = 0.0
    high = 0.0
    bars_below = 0
    decline_vol = []
    armed = False

    for i, b in enumerate(bars):
        typical = (b["h"] + b["l"] + b["c"]) / 3
        cum_pv += typical * b["v"]
        cum_v += b["v"]
        if cum_v <= 0:
            continue
        vwap = cum_pv / cum_v
        high = max(high, b["h"])

        if b["c"] < vwap:
            bars_below += 1
            decline_vol.append(b["v"])
            off = (b["c"] / high - 1) * 100 if high else 0
            if bars_below >= min_bars_below and off <= -min_off_high:
                armed = True
            continue

        if not armed:
            continue
        avg = mean(decline_vol) if decline_vol else 0
        if avg <= 0 or b["v"] < avg * min_vol_ratio:
            continue
        return i
    return None


def simulate(bars: list, i: int, stop_pct: float, trail_pct: float | None,
             arm_at: float, give_up: int | None) -> tuple[str, float]:
    """Hold from bar i under the given exit rules."""
    entry = bars[i]["c"]
    stop = entry * (1 - stop_pct / 100)
    peak = entry

    for j, b in enumerate(bars[i + 1:], start=1):
        if b["l"] <= stop:
            return ("stop" if peak <= entry * 1.05 else "trail",
                    (stop / entry - 1) * 100)
        if b["h"] > peak:
            peak = b["h"]
            if trail_pct and peak >= entry * (1 + arm_at / 100):
                stop = max(stop, peak * (1 - trail_pct / 100))
        if give_up and j >= give_up and b["c"] < entry:
            return ("gave_up", (b["c"] / entry - 1) * 100)

    return ("close", (bars[-1]["c"] / entry - 1) * 100)


def report(name: str, results: list) -> dict:
    if not results:
        print(f"  {name:<28} no trades")
        return {}
    rets = [r for _, r in results]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    avg = mean(rets)
    se = stdev(rets) / len(rets) ** 0.5 if len(rets) > 1 else 0

    print(f"  {name:<28}{len(rets):>5} trades  "
          f"{len(wins) / len(rets) * 100:>3.0f}% win  "
          f"{avg:>+7.2f}% ±{se:.2f}   "
          f"W {mean(wins) if wins else 0:>+6.1f}%  "
          f"L {mean(losses) if losses else 0:>+6.1f}%")
    return {"n": len(rets), "avg": avg, "se": se,
            "win_rate": len(wins) / len(rets),
            "avg_win": mean(wins) if wins else 0,
            "avg_loss": mean(losses) if losses else 0}


def main() -> None:
    sessions = load_cached()
    if not sessions:
        print("No cached bars. Run tools.backtest_reclaim first.")
        return

    signals = []
    for symbol, day, bars in sessions:
        i = find_entry(bars)
        if i is not None and i < len(bars) - 10:
            signals.append((symbol, day, bars, i))

    print(f"\n{'=' * 78}")
    print(f"  HOLD TEST — {len(signals)} entries from {len(sessions)} cached sessions")
    print(f"{'=' * 78}\n")
    print(f"  Same entries throughout. Only the exit changes.\n")

    variants = [
        ("old: 8% stop, 45m give-up",  8.0,  12.0, 10.0, 45),
        ("12% stop, 45m give-up",     12.0,  12.0, 10.0, 45),
        ("12% stop, no give-up",      12.0,  12.0, 10.0, None),
        ("12% stop, 20% trail",       12.0,  20.0, 10.0, None),
        ("12% stop, 30% trail",       12.0,  30.0, 10.0, None),
        ("12% stop, no trail at all", 12.0,  None, 10.0, None),
        ("20% stop, 30% trail",       20.0,  30.0, 15.0, None),
    ]

    stats = {}
    for label, stop, trail, arm, give_up in variants:
        results = [(sym, simulate(bars, i, stop, trail, arm, give_up)[1])
                   for sym, day, bars, i in signals]
        stats[label] = report(label, results)

    print()
    best = max((s for s in stats.values() if s), key=lambda s: s["avg"])
    best_name = [k for k, v in stats.items() if v is best][0]

    print(f"  Best: {best_name} at {best['avg']:+.2f}% per trade")

    if best["avg"] > 2 * best["se"]:
        print(f"\n  Positive beyond the noise band ({best['se']:.2f} standard error).")
        print(f"  A {best['win_rate']*100:.0f}% hit rate carried by a "
              f"{abs(best['avg_win']/best['avg_loss']):.1f}:1 payoff.")
        print(f"\n  That is the shape of the manual log — modest accuracy, large")
        print(f"  winners — reached without matching anyone's judgment.")
    elif best["avg"] > 0:
        print(f"\n  Positive but inside the noise band "
              f"(±{best['se']:.2f}). Not distinguishable from zero.")
    else:
        print(f"\n  Still negative with every exit tested. The entries have no")
        print(f"  edge for an exit rule to work with.")

    print(f"""
  Caveats unchanged: the universe excludes delisted names, entries assume a
  fill at the signal bar's close, and holding a 12-20% drawdown all day is
  considerably harder in practice than in a table.
""")


if __name__ == "__main__":
    main()
