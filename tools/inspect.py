"""What does the detector actually see?

    python -m tools.inspect GIPR 2026-08-24
    python -m tools.inspect GIPR 2026-08-24 --around 10:30

Five rounds of parameter changes moved agreement between 41% and 53% and
taught us nothing. This stops guessing: it prints every swing point, every
trendline, every level and every confluence the machine found on one
symbol-day, so the trader can look at the same chart and say which part is
wrong.

That is a different kind of debugging. A validation score says the rule
disagrees with you; this says WHY, in terms you can check against what you
remember seeing.

The most useful output is usually the swing list. If the pivots are wrong,
every line built on them is wrong, and no threshold anywhere else will fix
it.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from patterns.confluence import evaluate, find_demand_zones
from patterns.detect import (bar_time, detect, had_premarket_volume,
                             higher_lows, in_trading_window)
from patterns.geometry import find_swings, find_trendlines, horizontal_level

ET = ZoneInfo("America/New_York")


def load_bars(symbol: str, day: date) -> tuple[list, list]:
    """Reuse whatever tools.validate already cached."""
    cache = pathlib.Path("data/bars/validate")
    five = cache / f"{symbol}-{day.isoformat()}-5m.json"
    daily = cache / f"{symbol}-{day.isoformat()}-1d.json"

    if five.exists():
        bars = json.loads(five.read_text())
        dailies = json.loads(daily.read_text()) if daily.exists() else []
        return bars, dailies

    sys.path.insert(0, ".")
    from alpaca.data.historical import StockHistoricalDataClient

    from data.reference import load_credentials
    from tools.validate import daily_bars, five_minute_bars

    key, secret = load_credentials()
    client = StockHistoricalDataClient(key, secret)
    return (five_minute_bars(client, symbol, day),
            daily_bars(client, symbol, day))


def show_bars(bars: list, around: str | None) -> None:
    if not around:
        return
    hh, mm = int(around[:2]), int(around[3:5])
    print(f"\n  BARS AROUND {around}\n")
    print(f"  {'time':<7}{'open':>9}{'high':>9}{'low':>9}{'close':>9}"
          f"{'vol':>10}  {'body':>6}{'lower wick':>12}")
    for b in bars:
        h, m = bar_time(b)
        if abs((h * 60 + m) - (hh * 60 + mm)) > 40:
            continue
        span = b["h"] - b["l"]
        body = abs(b["c"] - b["o"])
        wick = (min(b["o"], b["c"]) - b["l"]) / span if span else 0
        print(f"  {b['t'][11:16]:<7}{b['o']:>9.4f}{b['h']:>9.4f}"
              f"{b['l']:>9.4f}{b['c']:>9.4f}{b['v']:>10,.0f}"
              f"{body:>6.3f}{wick:>11.0%}")


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("symbol")
    p.add_argument("date")
    p.add_argument("--around", default=None,
                   help="HH:MM — print the bars around this time")
    args = p.parse_args()

    day = date.fromisoformat(args.date)
    bars, daily = load_bars(args.symbol.upper(), day)

    if not bars:
        print("No bars.")
        return

    print(f"\n{'=' * 72}")
    print(f"  {args.symbol.upper()}  {day}  —  {len(bars)} five-minute bars")
    print(f"{'=' * 72}")

    first, last = bars[0]["t"][11:16], bars[-1]["t"][11:16]
    session_high = max(b["h"] for b in bars)
    session_low = min(b["l"] for b in bars)
    print(f"\n  {first} to {last}   range {session_low:.4f} - {session_high:.4f}")

    pre = sum(b["v"] for b in bars if bar_time(b) < (9, 30))
    total = sum(b["v"] for b in bars)
    print(f"  premarket volume {pre:,.0f} of {total:,.0f} "
          f"({pre / total * 100 if total else 0:.0f}%)  "
          f"-> gate {'PASSES' if had_premarket_volume(bars) else 'BLOCKS'}")

    # --- swings -------------------------------------------------------
    swings = find_swings(bars)
    highs = [s for s in swings if s.is_high]
    lows = [s for s in swings if not s.is_high]
    print(f"\n  SWING POINTS — {len(highs)} highs, {len(lows)} lows")
    print(f"  If these are wrong, everything built on them is wrong.\n")
    for s in swings:
        print(f"    {bars[s.index]['t'][11:16]}  bar {s.index:>3}  "
              f"{'HIGH' if s.is_high else 'low ':<4}  {s.price:.4f}")

    # --- lines --------------------------------------------------------
    for is_upper, label in ((True, "RESISTANCE"), (False, "SUPPORT")):
        lines = find_trendlines(bars, swings, is_upper=is_upper)
        print(f"\n  {label} TRENDLINES (3+ taps): {len(lines)}")
        for line in lines[:4]:
            times = [bars[t]["t"][11:16] for t in line.touches]
            print(f"    {len(line.touches)} taps at {', '.join(times)}   "
                  f"slope {line.slope:+.5f}/bar   flat={line.is_flat}")
            print(f"      price at first tap {line.price_at(line.touches[0]):.4f}"
                  f", at last {line.price_at(line.touches[-1]):.4f}")

    flat = horizontal_level(bars, swings, is_high=True)
    if flat:
        level, touches = flat
        print(f"\n  FLAT LEVEL: {level:.4f} touched at "
              f"{', '.join(bars[t]['t'][11:16] for t in touches)}")
        print(f"    higher lows after first touch: "
              f"{higher_lows(bars, swings, touches[0])}")
    else:
        print(f"\n  FLAT LEVEL: none found")

    # --- zones --------------------------------------------------------
    zones = find_demand_zones(bars)
    print(f"\n  DEMAND ZONES: {len(zones)}")
    for z in zones[:6]:
        print(f"    {z.low:.4f}-{z.high:.4f}  from the red candle at "
              f"{bars[z.formed_at]['t'][11:16]}")

    # --- what it decided ----------------------------------------------
    print(f"\n  DECISIONS\n")
    setups = detect(bars, daily=daily, levels=[])
    if not setups:
        print(f"    Nothing at all — no bar produced even a rejected setup.")
    for s in setups[:14]:
        when = bars[s.index]["t"][11:16] if s.index < len(bars) else "-"
        if s.rejected:
            print(f"    {when}  refused: {s.rejected}")
        else:
            print(f"    {when}  TAKE {s.kind} {s.grade} @ {s.entry:.4f}  "
                  f"stop {s.stop:.4f}")
            for d in s.confluences.detail:
                print(f"           - {d}")

    # --- confluences through the window --------------------------------
    print(f"\n  CONFLUENCES, bar by bar (window only)\n")
    shown = 0
    for i, b in enumerate(bars):
        if not in_trading_window(b) or i < 10:
            continue
        c = evaluate(bars, i, zones)
        if c.count >= 1:
            print(f"    {b['t'][11:16]}  {c.count} ({c.grade:<4}) "
                  f"{'; '.join(c.detail)}")
            shown += 1
        if shown >= 20:
            print(f"    ... (truncated)")
            break
    if shown == 0:
        print(f"    None — no bar in the window had a single confluence.")

    show_bars(bars, args.around)
    print()


if __name__ == "__main__":
    main()
