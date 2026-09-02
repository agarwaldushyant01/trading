"""Patterns, scored by confluence and confirmed by a close.

Detects only what the trader actually trades:

  BREAKOUT   falling wedge, bullish pennant, ascending triangle
  REVERSAL   higher lows off a base after a selloff

And refuses two shapes that look like the above and are not:

  DOUBLE TOP    flat resistance with two taps and no rising lows. The
                ascending triangle's failure case, and identical to it until
                you check whether the lows are climbing.

  RISING WEDGE  support rising faster than resistance. Reads as strength on
                every momentum measure and is a top forming.

Three rules apply to every detection, each of which would have prevented a
category of loss in earlier versions:

  A CLOSE, NOT A WICK. Price piercing a level intraday and being sold back is
  a fakeout. Only a body closing beyond the line counts. Earlier versions
  triggered on touch, so every fakeout became an entry and the stop fired on
  the reversal — which is exactly the shape of the losing trade log.

  TWO CONFLUENCES MINIMUM. An A setup. Three or more is A++.

  TREND CONTEXT. A breakout against a falling higher-timeframe trend is a
  bull trap: the candles close above resistance and it still fails, because
  the larger direction is down. Reversal setups are the opposite — a prior
  downtrend is required, not disqualifying.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from patterns.confluence import Confluences, evaluate, find_demand_zones
from patterns.geometry import (Trendline, find_swings, find_trendlines,
                               horizontal_level)


def wedge_pair(upper: Trendline, lower: Trendline, bars: list,
               index: int) -> str | None:
    """Classify a pair of trendlines as a wedge, or reject it.

    A wedge is TWO CONVERGING LINES. The first version of this checked only
    the upper line and called any descending resistance a falling wedge —
    which is a downtrend-line detector, not a wedge detector. It fired on
    every trade in validation, winners and losers alike, because a declining
    resistance line with three taps occurs several times a session on almost
    anything.

    Both conditions matter:

      CONVERGENCE  the vertical gap between the lines must be contracting.
                   That contraction IS the pattern — sellers achieving less
                   on each attempt while buyers absorb more.

      RELATIVE     falling wedge: upper falls faster than lower.
      SLOPE        rising wedge: lower rises faster than upper.
                   Equal slopes are a channel, which means nothing.
    """
    start = max(upper.touches[0], lower.touches[0])
    if index - start < 8:
        return None

    width_start = upper.price_at(start) - lower.price_at(start)
    width_now = upper.price_at(index) - lower.price_at(index)
    if width_start <= 0 or width_now <= 0:
        return None

    # Must have narrowed by at least a third to count as converging.
    if width_now / width_start > 0.67:
        return None

    up, low = upper.slope, lower.slope

    if up < 0 and low < 0 and abs(up) > abs(low) * 1.5:
        return "falling_wedge"
    if up > 0 and low > 0 and low > up * 1.5:
        return "rising_wedge"
    return None


@dataclass
class Setup:
    kind: str                  # falling_wedge | pennant | ascending_triangle
                               # | reversal
    index: int                 # bar the entry is confirmed on
    entry: float
    stop: float
    target: float | None
    confluences: Confluences
    note: str = ""
    rejected: str = ""         # why a near-miss was refused
    detail: dict = field(default_factory=dict)

    @property
    def grade(self) -> str:
        return self.confluences.grade

    @property
    def risk_reward(self) -> float | None:
        if self.target is None or self.entry <= self.stop:
            return None
        return (self.target - self.entry) / (self.entry - self.stop)


def closes_above(bars: list, index: int, level: float) -> bool:
    """A body closing above the level — not a wick through it."""
    bar = bars[index]
    return bar["c"] > level and bar["c"] > bar["o"]


def is_engulfing(bars: list, index: int) -> bool:
    """A green candle swallowing the red one before it.

    Sellers tried and were overwhelmed — the confirmation the trader uses at
    a retest.
    """
    if index < 1:
        return False
    prev, cur = bars[index - 1], bars[index]
    return (prev["c"] < prev["o"] and cur["c"] > cur["o"]
            and cur["c"] >= prev["o"] and cur["o"] <= prev["c"])


def retest_entry(bars: list, index: int, level: float,
                 broke_at: int, tolerance_pct: float = 3.0) -> bool:
    """Price returned to a broken level and held it.

    THE ENTRY THE TRADER ACTUALLY TAKES. Earlier versions triggered on the
    breakout candle itself; on GIPR that fires at 12:30 while the real entry
    was 15:05, after the move ran to 0.669 and pulled back to 0.516.

    Broken resistance becomes support. The trade is the hold, not the break —
    which gives a better price and a defined invalidation, at the cost of
    missing breaks that never come back.

    Requires three things: price has returned to the level, the bar is green,
    and it either engulfs the previous candle or shows a lower wick. A small
    green followed by a large red is a failed retest, not an entry.
    """
    if index <= broke_at + 1:
        return False

    bar = bars[index]
    near = abs(bar["l"] - level) / level * 100 <= tolerance_pct
    if not near or bar["c"] <= bar["o"]:
        return False

    if is_engulfing(bars, index):
        return True

    span = bar["h"] - bar["l"]
    if span <= 0:
        return False
    return (min(bar["o"], bar["c"]) - bar["l"]) / span >= 0.4


def higher_lows(bars: list, swings: list, start: int,
                min_count: int = 3, allow_breaks: int = 1) -> bool:
    """Successive swing lows climbing, tolerating a little noise.

    Separates an ascending triangle from a double top, and is the whole of
    the reversal setup. Demanding a strictly monotonic sequence rejects
    almost every real base — one lower low inside an otherwise rising series
    is normal, so a small number of breaks is allowed.
    """
    lows = [s for s in swings if not s.is_high and s.index >= start]
    if len(lows) < min_count:
        return False
    breaks = sum(1 for i in range(1, len(lows))
                 if lows[i].price <= lows[i - 1].price * 0.995)
    return breaks <= allow_breaks and lows[-1].price > lows[0].price


def trend_is_down(daily: list, lookback: int = 10) -> bool:
    """Higher-timeframe direction, from daily bars."""
    if len(daily) < lookback:
        return False
    window = daily[-lookback:]
    first = sum(b["c"] for b in window[:3]) / 3
    last = sum(b["c"] for b in window[-3:]) / 3
    return last < first * 0.97


def nearest_resistance_above(price: float, levels: list) -> float | None:
    above = [l for l in levels if l > price * 1.005]
    return min(above) if above else None


def bar_time(bar: dict) -> tuple[int, int]:
    """Hour and minute from a bar's ISO timestamp."""
    stamp = bar.get("t", "")
    try:
        return (int(stamp[11:13]), int(stamp[14:16]))
    except (ValueError, IndexError):
        return (-1, -1)


def in_trading_window(bar: dict, latest_hour: int = 16,
                      latest_minute: int = 0) -> bool:
    """Optional time filter, off by default.

    A 04:00-11:00 window was tried on the assumption that entries cluster
    early. The trader's GIPR entry was at 15:05, which the window excluded —
    the assumption was mine, not theirs, and it cost five winners in
    validation. Kept only for experiments.

    Added after validation showed the detector finding patterns at 13:25 and
    15:50 while the trader's own entries cluster in premarket and the first
    hour. The shape recurs all day; the moment does not. Two of the four
    manual reclaim trades were described as "high volume in the premarket" —
    which was a statement about timing that got encoded only as volume.
    """
    hour, minute = bar_time(bar)
    if hour < 0:
        return True                     # no timestamp: do not filter
    if hour < 4:
        return False
    return (hour, minute) <= (latest_hour, latest_minute)


def had_premarket_volume(bars: list, min_share: float = 0.10) -> bool:
    """Was the name already in play before the open?

    Measured as premarket volume against the session total, so it does not
    need a per-symbol baseline. A name that traded nothing before 09:30 was
    not on anyone's screen, whatever it does later.
    """
    pre = reg = 0.0
    for b in bars:
        hour, minute = bar_time(b)
        if hour < 0:
            continue
        if (hour, minute) < (9, 30):
            pre += b["v"]
        else:
            reg += b["v"]
    total = pre + reg
    return total > 0 and pre / total >= min_share


def detect(bars: list, daily: list | None = None,
           levels: list | None = None,
           min_confluences: int = 2,
           window_only: bool = False,
           require_premarket: bool = False) -> list:
    """Every qualifying setup in the session, in order.

    Returns near-misses too, marked with why they were refused — those are
    more informative than the hits when tuning, because they show whether the
    rule is too tight or looking at the wrong thing.
    """
    warmup = min(25, max(10, len(bars) // 3))
    if len(bars) < warmup + 5:
        return []

    if require_premarket and not had_premarket_volume(bars):
        return [Setup("none", 0, bars[-1]["c"], 0, None, Confluences(),
                      rejected="no premarket volume")]

    swings = find_swings(bars)
    zones = find_demand_zones(bars)
    levels = levels or []
    downtrend = trend_is_down(daily) if daily else False

    setups = []
    broken = []                  # (level, index) for each confirmed breakout
    upper_lines = find_trendlines(bars, swings, is_upper=True)
    lower_lines = find_trendlines(bars, swings, is_upper=False)
    # 3% rather than 2%: on GIPR the highs at 0.3390 and 0.3300 are one
    # level to the eye and 2.65% apart, so a tighter tolerance found no flat
    # level at all and the ascending triangle went undetected.
    flat = horizontal_level(bars, swings, is_high=True, tolerance_pct=3.0)

    for i in range(warmup, len(bars)):
        bar = bars[i]

        if window_only and not in_trading_window(bar):
            continue

        # ---- exclusions first ------------------------------------------
        # A retest of something already broken takes priority: it is the
        # entry the trader takes, and it comes after the breakout the earlier
        # versions were firing on.
        for level, broke_at in broken:
            if retest_entry(bars, i, level, broke_at):
                conf = evaluate(bars, i, zones)
                if conf.count < min_confluences:
                    setups.append(Setup("retest", i, bar["c"], 0, None, conf,
                                        rejected=f"retest, only "
                                                 f"{conf.count} confluence(s)"))
                    break
                stop = min(b["l"] for b in bars[max(0, i - 3):i + 1]) * 0.99
                target = nearest_resistance_above(bar["c"], levels)
                setups.append(Setup(
                    "retest", i, bar["c"], stop,
                    target * 0.98 if target else None, conf,
                    note=f"retest of {level:.4f} broken at "
                         f"{bars[broke_at]['t'][11:16] if 't' in bars[broke_at] else broke_at}"))
                break

        blocked = None
        if flat and _is_double_top(bars, swings, flat, i):
            blocked = "double top forming"
        elif _is_rising_wedge(bars, swings, i):
            blocked = "rising wedge"

        # ---- breakout shapes -------------------------------------------
        hit = None

        # The flat level as a breakout candidate in its own right. Without
        # this the ascending triangle is unreachable: horizontal_level()
        # found GIPR's level at 0.334 correctly, but nothing ever tried to
        # break out through it.
        if flat:
            flat_level, flat_touches = flat
            if (flat_touches[0] < i
                    and closes_above(bars, i, flat_level)
                    and higher_lows(bars, swings, flat_touches[0])):
                recent_vol = [b["v"] for b in bars[max(0, i - 10):i]]
                avg_vol = sum(recent_vol) / len(recent_vol) if recent_vol else 0
                if avg_vol <= 0 or bars[i]["v"] >= avg_vol * 1.5:
                    hit = ("ascending_triangle",
                           Trendline(0.0, flat_level, flat_touches, True),
                           flat_level)

        if hit is None:
          for line in upper_lines:
              if not (line.touches[0] < i <= line.touches[-1] + 20):
                  continue
              level = line.price_at(i)
              if level <= 0 or not closes_above(bars, i, level):
                  continue

              # Volume must expand on the break. A breakout on thin volume is
              # drift, and the trader's own rule is that a level tested without
              # volume refuses to break.
              recent_vol = [b["v"] for b in bars[max(0, i - 10):i]]
              avg_vol = sum(recent_vol) / len(recent_vol) if recent_vol else 0
              if avg_vol > 0 and bars[i]["v"] < avg_vol * 1.5:
                  continue

              lows_rising = higher_lows(bars, swings, line.touches[0])

              if line.is_flat and lows_rising:
                  kind = "ascending_triangle"
              elif line.is_flat:
                  kind = "pennant"
              elif line.slope < 0:
                  # A falling upper line is only a wedge if a lower line
                  # converges with it. Without that it is just a downtrend.
                  kind = None
                  for low_line in lower_lines:
                      verdict = wedge_pair(line, low_line, bars, i)
                      if verdict == "falling_wedge":
                          kind = "falling_wedge"
                          break
                  if kind is None:
                      continue
              else:
                  continue
              hit = (kind, line, level)
              break

        if hit is None:
            continue

        kind, line, level = hit

        # Record the exclusion only where a setup would otherwise have
        # fired — a veto with nothing to veto is noise in the log.
        if blocked:
            setups.append(Setup(kind, i, bar["c"], 0, None, Confluences(),
                                rejected=blocked))
            continue

        # A breakout against a falling daily trend is a bull trap.
        if downtrend and kind in ("pennant", "ascending_triangle"):
            setups.append(Setup(kind, i, bar["c"], 0, None, Confluences(),
                                rejected="breakout against a falling trend"))
            continue

        conf = evaluate(bars, i, zones)
        if conf.count < min_confluences:
            setups.append(Setup(kind, i, bar["c"], 0, None, conf,
                                rejected=f"only {conf.count} confluence(s)"))
            continue

        # Stop below the structure that justified the entry, not a
        # percentage: the low of the pullback into the level.
        recent_low = min(b["l"] for b in bars[max(0, i - 10):i + 1])
        stop = recent_low * 0.995

        target = nearest_resistance_above(bar["c"], levels)
        if target:
            target *= 0.98                  # out before the wall, not at it

        broken.append((level, i))
        setups.append(Setup(kind, i, bar["c"], stop, target, conf,
                            note=f"{len(line.touches)} taps, "
                                 f"line at {level:.4f}",
                            detail={"line_slope": line.slope,
                                    "touches": line.touches}))

    return setups


def _is_double_top(bars: list, swings: list, flat, index: int) -> bool:
    """A double top, and only where price is actually testing the level.

    The first version applied this session-wide: it found the strongest flat
    level anywhere in the day and then vetoed every bar whose lows were not
    strictly rising, whether or not price was near that level. In validation
    it blocked 10 of 13 missed winners — the exclusion was doing all the work
    and the patterns were never evaluated.

    Two corrections: price must be AT the level for the shape to mean
    anything, and the rising-lows test tolerates one lower low, since
    demanding a perfectly monotonic sequence rejects almost every real base.
    """
    level, touches = flat
    prior = [t for t in touches if t < index]
    if len(prior) < 2:
        return False

    price = bars[index]["c"]
    if price > level * 1.02:
        return False                        # already broken through
    if abs(price - level) / level > 0.03:
        return False                        # not testing the level

    return not higher_lows(bars, swings, prior[0], min_count=2)


def _is_rising_wedge(bars: list, swings: list, index: int) -> bool:
    """Support climbing faster than resistance — a top, not a base."""
    window = [s for s in swings if index - 40 <= s.index <= index]
    highs = [s for s in window if s.is_high]
    lows = [s for s in window if not s.is_high]
    if len(highs) < 3 or len(lows) < 3:
        return False

    def slope(points):
        span = points[-1].index - points[0].index
        return (points[-1].price - points[0].price) / span if span else 0

    up_slope, low_slope = slope(highs), slope(lows)
    return up_slope > 0 and low_slope > up_slope * 1.5
