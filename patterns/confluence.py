"""The four confluences — scored, not gated.

A pattern alone is not a trade. Two confluences make an A setup, three or
more an A++. Every previous version of this bot fired on effectively one
condition, which is how it took 210 trades in twenty sessions and lost on
most of them.

The four, with the definitions the trader gave:

  DEMAND ZONE      the last red candle before a strong upward push, from its
                   high to its low, projected forward. Where the buyers who
                   caused the rally were absorbing supply.

  BOTTOM WICK      a long lower shadow: price was pushed down and bought back
                   within the bar. Buyers defending the level.

  LOW VOLUME       small candle bodies on the pullback. Sellers are not
  PULLBACK         pressing. Identified by body size rather than by a volume
                   figure, since that is how it is read on the chart.

  PSYCHOLOGICAL    quarter-dollar increments: 5.00, 5.25, 5.50, 5.75. Not
  SUPPORT          only round dollars, which makes this the weakest of the
                   four on a low-priced stock, where almost any price is near
                   one. Weighted accordingly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean


@dataclass
class DemandZone:
    low: float
    high: float
    formed_at: int

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high

    def near(self, price: float, tolerance_pct: float = 1.0) -> bool:
        pad = self.high * tolerance_pct / 100
        return self.low - pad <= price <= self.high + pad


@dataclass
class Confluences:
    demand_zone: bool = False
    bottom_wick: bool = False
    low_volume_pullback: bool = False
    psychological: bool = False
    detail: list = field(default_factory=list)

    @property
    def count(self) -> int:
        return sum([self.demand_zone, self.bottom_wick,
                    self.low_volume_pullback, self.psychological])

    @property
    def grade(self) -> str:
        n = self.count
        if n >= 3:
            return "A++"
        if n == 2:
            return "A"
        return "none"


def find_demand_zones(bars: list, min_push_pct: float = 3.0,
                      push_bars: int = 3) -> list:
    """Zones drawn from the last red candle before each strong push up.

    The push threshold is what decides how many zones exist. Too low and
    every wobble creates one, which would make the confluence meaningless
    because price is always inside some zone.
    """
    zones = []
    for i in range(1, len(bars) - push_bars):
        start = bars[i]["c"]
        ahead = bars[i:i + push_bars + 1]
        peak = max(b["h"] for b in ahead)
        if start <= 0 or (peak / start - 1) * 100 < min_push_pct:
            continue

        # Walk back to the last red candle before the push.
        j = i
        while j >= 0 and bars[j]["c"] >= bars[j]["o"]:
            j -= 1
        if j < 0:
            continue

        zone = DemandZone(low=bars[j]["l"], high=bars[j]["h"], formed_at=j)
        if not any(abs(z.low - zone.low) / max(z.low, 0.01) < 0.01
                   for z in zones):
            zones.append(zone)
    return zones


def has_bottom_wick(bar: dict, min_ratio: float = 0.5) -> bool:
    """Lower shadow at least this fraction of the bar's whole range.

    Measured against range rather than body, so a doji with a long tail
    counts — which is the shape that matters at a level.
    """
    span = bar["h"] - bar["l"]
    if span <= 0:
        return False
    body_low = min(bar["o"], bar["c"])
    return (body_low - bar["l"]) / span >= min_ratio


def is_low_volume_pullback(bars: list, index: int, window: int = 6,
                           max_body_ratio: float = 0.5) -> bool:
    """Small bodies over the recent stretch, relative to earlier bodies.

    Read from candle bodies rather than volume figures, matching how it is
    identified on the chart — and more robust, since body size does not
    depend on the volume feed being clean.
    """
    if index < window * 2:
        return False
    recent = bars[index - window:index + 1]
    earlier = bars[index - window * 2:index - window]
    if not earlier:
        return False

    def body(b):
        return abs(b["c"] - b["o"])

    recent_body = mean(body(b) for b in recent)
    earlier_body = mean(body(b) for b in earlier)
    if earlier_body <= 0:
        return False
    return recent_body / earlier_body <= max_body_ratio


def near_psychological(price: float, tolerance_pct: float = 1.0) -> bool:
    """Within tolerance of a quarter-dollar increment.

    On a $0.30 stock nearly every price is near one of these, so this is the
    weakest of the four confluences and should never carry a setup alone.
    """
    if price <= 0:
        return False
    quarter = round(price * 4) / 4
    if quarter <= 0:
        return False
    return abs(price - quarter) / price * 100 <= tolerance_pct


def evaluate(bars: list, index: int, zones: list) -> Confluences:
    """Score the four confluences at one bar."""
    bar = bars[index]
    price = bar["c"]
    c = Confluences()

    for zone in zones:
        if zone.formed_at < index and zone.near(bar["l"]):
            c.demand_zone = True
            c.detail.append(f"demand zone {zone.low:.4f}-{zone.high:.4f}")
            break

    if has_bottom_wick(bar):
        span = bar["h"] - bar["l"]
        ratio = (min(bar["o"], bar["c"]) - bar["l"]) / span if span else 0
        c.bottom_wick = True
        c.detail.append(f"bottom wick {ratio:.0%} of range")

    if is_low_volume_pullback(bars, index):
        c.low_volume_pullback = True
        c.detail.append("small bodies into the level")

    if near_psychological(price):
        c.psychological = True
        c.detail.append(f"near {round(price * 4) / 4:.2f}")

    return c
