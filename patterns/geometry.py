"""Swing points and trendlines — the geometry everything else rests on.

Two rules govern this file, both from the trader rather than from me:

  THREE TAPS. A trendline is not a line until price has touched it three
  times. Any two points define a line; the third is what makes it real,
  because price came back and respected something that already existed.
  Without this a detector hallucinates structure everywhere — which is
  exactly how earlier attempts produced 210 signals in twenty sessions.

  FLAT MEANS FLAT. For the horizontal resistance in a pennant or ascending
  triangle, the highs must sit at nearly the same price. A ceiling allowed to
  drift becomes a symmetrical triangle, which carries no directional meaning
  and would quietly pollute every result.

Touch tolerance is about 2% of price: on a $4.00 level, a high of $3.92
counts. That is looser than it looks, and deliberately so — real levels are
zones, not lines, and demanding exact touches finds nothing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Swing:
    index: int
    price: float
    is_high: bool


@dataclass
class Trendline:
    slope: float            # price change per bar
    intercept: float        # price at index 0
    touches: list           # indices where price met the line
    is_upper: bool

    def price_at(self, index: int) -> float:
        return self.intercept + self.slope * index

    @property
    def is_flat(self) -> bool:
        """Near-horizontal over the span of its touches.

        Judged against the line's own price rather than an absolute slope, so
        the same test works on a $0.30 stock and a $16 one.
        """
        if len(self.touches) < 2:
            return False
        span = self.touches[-1] - self.touches[0]
        if span <= 0:
            return False
        drift = abs(self.slope * span)
        return drift / max(self.price_at(self.touches[0]), 0.01) < 0.015


def find_swings(bars: list, lookback: int = 2) -> list:
    """Local highs and lows, each higher or lower than its neighbours.

    A small lookback keeps minor pivots that matter on a 5-minute chart; a
    large one only finds the obvious turns and misses the taps that confirm a
    line.
    """
    swings = []
    for i in range(lookback, len(bars) - lookback):
        window = bars[i - lookback:i + lookback + 1]
        others = window[:lookback] + window[lookback + 1:]
        high = bars[i]["h"]
        low = bars[i]["l"]

        # Strictly above at least one neighbour, not merely equal to all of
        # them. Without this a flat stretch registers as a swing at every
        # bar, and those phantom pivots then dominate any level clustering —
        # a run of identical highs looked like an eleven-touch level in
        # testing when the real level was elsewhere.
        if (all(high >= b["h"] for b in others)
                and any(high > b["h"] for b in others)):
            swings.append(Swing(i, high, True))
        elif (all(low <= b["l"] for b in others)
                and any(low < b["l"] for b in others)):
            swings.append(Swing(i, low, False))
    return swings


def fit_line(a: Swing, b: Swing) -> tuple[float, float] | None:
    if b.index == a.index:
        return None
    slope = (b.price - a.price) / (b.index - a.index)
    intercept = a.price - slope * a.index
    return (slope, intercept)


def count_touches(bars: list, slope: float, intercept: float, is_upper: bool,
                  start: int, end: int, tolerance_pct: float = 2.0) -> list:
    """Bars whose high (or low) comes within tolerance of the line.

    Only counts touches from the correct side: an upper line is touched by
    highs, a lower line by lows. Consecutive bars are collapsed to one touch,
    since price sitting against a line for three minutes is one visit, not
    three.
    """
    touches = []
    for i in range(start, min(end + 1, len(bars))):
        line = intercept + slope * i
        if line <= 0:
            continue
        price = bars[i]["h"] if is_upper else bars[i]["l"]
        if abs(price - line) / line * 100 <= tolerance_pct:
            if touches and i - touches[-1] <= 1:
                touches[-1] = i          # same visit, extend it
            else:
                touches.append(i)
    return touches


def find_trendlines(bars: list, swings: list, is_upper: bool,
                    min_touches: int = 3,
                    tolerance_pct: float = 2.0) -> list:
    """Every line meeting the three-tap rule, best first.

    Candidate lines come from pairs of swing points; a line survives only if
    a third touch exists. Lines that price has already violated are dropped —
    a resistance line that price closed above is no longer resistance.
    """
    points = [s for s in swings if s.is_high == is_upper]
    if len(points) < 2:
        return []

    found = []
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            fit = fit_line(points[i], points[j])
            if fit is None:
                continue
            slope, intercept = fit

            touches = count_touches(bars, slope, intercept, is_upper,
                                    points[i].index, len(bars) - 1,
                                    tolerance_pct)
            if len(touches) < min_touches:
                continue

            # Reject a line price broke through and left behind DURING its
            # formation. Only bars between the first and last touch count:
            # checking to the end of the data would invalidate every line at
            # the moment it breaks, which is precisely the event being
            # detected.
            violated = False
            for k in range(touches[0], touches[-1] + 1):
                line = intercept + slope * k
                if is_upper and bars[k]["c"] > line * 1.02:
                    violated = True
                    break
                if not is_upper and bars[k]["c"] < line * 0.98:
                    violated = True
                    break
            if violated:
                continue

            found.append(Trendline(slope, intercept, touches, is_upper))

    # Prefer more touches, then the longer span — both mean a line more
    # traders will have drawn.
    found.sort(key=lambda t: (len(t.touches), t.touches[-1] - t.touches[0]),
               reverse=True)

    # Drop near-duplicates: many pairs of swings fit almost the same line.
    unique = []
    for line in found:
        if not any(abs(line.slope - u.slope) < 1e-4
                   and abs(line.intercept - u.intercept) /
                   max(u.intercept, 0.01) < 0.02 for u in unique):
            unique.append(line)
    return unique


def horizontal_level(bars: list, swings: list, is_high: bool,
                     min_touches: int = 3,
                     tolerance_pct: float = 2.0) -> tuple[float, list] | None:
    """The strongest flat level — the ceiling of a pennant or triangle.

    Distinct from a flat trendline: this clusters swing prices directly
    rather than fitting a line, which is closer to how the level is drawn by
    hand.
    """
    points = [s for s in swings if s.is_high == is_high]
    if len(points) < min_touches:
        return None

    best = None
    for anchor in points:
        cluster = [p for p in points
                   if abs(p.price - anchor.price) / anchor.price * 100
                   <= tolerance_pct]
        if len(cluster) < min_touches:
            continue
        level = sum(p.price for p in cluster) / len(cluster)
        indices = sorted(p.index for p in cluster)
        span = indices[-1] - indices[0]
        score = (len(cluster), span)
        if best is None or score > best[0]:
            best = (score, level, indices)

    return (best[1], best[2]) if best else None
