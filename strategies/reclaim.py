"""VWAP reclaim from a downtrend, on volume expansion.

Four of the sixteen logged manual winners were this setup — GIPR, TNMG, NCPL,
WKSP — averaging +45%. It is the most mechanical of the four categories and
so the first worth testing properly.

WHY THIS IS NOT THE STRATEGY THAT WAS ALREADY REJECTED

An earlier module called strategies/vwap_reclaim.py was tested and dropped:
629 trades, 23% hit rate, -0.43R, every parameter combination negative out of
sample. That version fired on any scanner candidate closing back above VWAP
after two bars below it. Applied to names that were already up 50% on the
day, it was buying a pause in a blow-off top.

This is a different rule, and the difference is the whole point:

  A DOWNTREND FIRST. The name must have been falling — below VWAP for an
  extended stretch and well off its session high. The setup is a reversal,
  not a dip inside an uptrend.

  VOLUME EXPANSION ON THE RECLAIM. The bar that crosses back above VWAP must
  carry meaningfully more volume than the decline did. Without that it is
  drift, and drift back below VWAP is the base case.

  NOT ALREADY EXTENDED. If the name is up 50% on the day, this is not a
  reversal off a low, and the live sessions showed those are the worst
  trades regardless of shape.

Nothing here is tuned to fit anything. Every threshold below is a first
guess, deliberately round, to be tested and then argued with.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReclaimConfig:
    # --- the downtrend that must come first ---------------------------
    min_bars_below_vwap: int = 15      # a genuine decline, not a two-bar dip
    min_pct_off_high: float = 8.0      # how far it has faded from the session high

    # --- the reclaim bar ----------------------------------------------
    min_volume_ratio: float = 2.0      # this bar vs the average during the decline
    require_close_above: bool = True   # close above VWAP, not just touch it

    # --- the name itself ----------------------------------------------
    min_price: float = 0.25
    max_price: float = 20.0
    max_pct_change: float = 30.0       # not already extended for the day
    min_session_volume: float = 200_000

    # --- exits ---------------------------------------------------------
    # The manual trades averaged +47% against a 15% target, so a fixed target
    # gives away most of the move. A trailing stop keeps the position while
    # the move continues. Both are tested; the sweep decides.
    stop_pct: float = 8.0
    target_pct: float | None = None    # None means trail only
    trail_pct: float = 12.0            # give back this much from the peak
    give_up_minutes: int = 45          # out if it has not worked by then


class ReclaimState:
    """Per-symbol state for detecting the setup on a stream of bars.

    Tracks the decline so the reclaim can be measured against it: how long
    the name spent below VWAP, how far it fell from the high, and what
    volume looked like on the way down.
    """

    def __init__(self) -> None:
        self.bars_below = 0
        self.high_of_day = 0.0
        self.decline_volume: list = []
        self.armed = False           # a downtrend has been established
        self.triggered_at = None     # fired already this session

    def update(self, bar, vwap: float, cfg: ReclaimConfig) -> bool:
        """Feed one bar. Returns True on the bar that completes the setup."""
        self.high_of_day = max(self.high_of_day, bar.high)
        below = bar.close < vwap

        if below:
            self.bars_below += 1
            self.decline_volume.append(bar.volume)
            off_high = (bar.close / self.high_of_day - 1) * 100 if self.high_of_day else 0
            if (self.bars_below >= cfg.min_bars_below_vwap
                    and off_high <= -cfg.min_pct_off_high):
                self.armed = True
            return False

        # Above VWAP. Only interesting if a downtrend came first.
        if not self.armed or self.triggered_at is not None:
            self.reset_if_extended(bar, vwap)
            return False

        if cfg.require_close_above and bar.close <= vwap:
            return False

        avg_decline = (sum(self.decline_volume) / len(self.decline_volume)
                       if self.decline_volume else 0)
        if avg_decline <= 0 or bar.volume < avg_decline * cfg.min_volume_ratio:
            # Crossed back over, but nobody is buying it. Stay armed: the
            # name may reclaim properly on the next attempt.
            return False

        self.triggered_at = bar.timestamp
        return True

    def reset_if_extended(self, bar, vwap: float) -> None:
        """Once a name is well above VWAP the reversal has passed."""
        if self.armed and bar.close > vwap * 1.05:
            self.armed = False
            self.bars_below = 0
            self.decline_volume = []


def qualifies(ref, price: float, pct_change: float,
              session_volume: float, cfg: ReclaimConfig) -> str | None:
    """Universe checks. Returns a rejection reason, or None if tradeable."""
    if not (cfg.min_price <= price <= cfg.max_price):
        return f"price {price:.2f}"
    if pct_change > cfg.max_pct_change:
        return f"already up {pct_change:.0f}%"
    if session_volume < cfg.min_session_volume:
        return f"session volume {session_volume / 1e6:.2f}M"
    return None


def exit_for(bars: list, entry_idx: int, entry: float,
             cfg: ReclaimConfig) -> tuple[str, float]:
    """Walk forward from entry and return how the trade ended.

    A trailing stop rather than a fixed target, because the manual trades
    averaged +47% and a 15% target would have captured a third of that. The
    trail gives back some of the peak in exchange for staying in the ones
    that keep going.
    """
    stop = entry * (1 - cfg.stop_pct / 100)
    peak = entry

    for i, bar in enumerate(bars[entry_idx + 1:], start=1):
        low = bar["l"] if isinstance(bar, dict) else bar.low
        high = bar["h"] if isinstance(bar, dict) else bar.high
        close = bar["c"] if isinstance(bar, dict) else bar.close

        if low <= stop:
            return ("stop", (stop / entry - 1) * 100)

        if cfg.target_pct is not None:
            target = entry * (1 + cfg.target_pct / 100)
            if high >= target:
                return ("target", cfg.target_pct)

        if high > peak:
            peak = high
            trailed = peak * (1 - cfg.trail_pct / 100)
            stop = max(stop, trailed)

        if cfg.give_up_minutes and i >= cfg.give_up_minutes:
            if close < entry:          # only bail if it has not worked
                return ("gave_up", (close / entry - 1) * 100)

    last = bars[-1]
    close = last["c"] if isinstance(last, dict) else last.close
    return ("close", (close / entry - 1) * 100)
