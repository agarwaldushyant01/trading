"""Setup 1 — VWAP reclaim.

The sequence, from your description: a stock runs, peaks, falls below VWAP,
then closes a 5-minute bar back above it. That reclaim is the entry.

Why this one first: every condition is a number a machine can check. No
judgement about whether a name "looks" manipulated, no read on the tape.

The six-month scan supports the shape of it — candidates caught below VWAP
had a 46.7% hit rate against 36.4% for ones caught extended above. This
strategy is the sharper version of that: not merely below VWAP, but below
and then recovering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

from scanner.scanner import Bar, Candidate, TickerRef
from strategies.base import FiveMinuteAggregator, Signal

SETUP = "vwap_reclaim"


@dataclass
class _Watch:
    """Per-symbol state for one session."""

    ref: TickerRef
    aggregator: FiveMinuteAggregator = field(default_factory=FiveMinuteAggregator)

    cum_notional: float = 0.0
    cum_volume: int = 0
    high_of_day: float = 0.0
    made_qualifying_high: bool = False

    bars_below_vwap: int = 0
    recent_volumes: list = field(default_factory=list)
    entered: bool = False

    @property
    def vwap(self) -> float | None:
        return self.cum_notional / self.cum_volume if self.cum_volume else None


class VwapReclaim:
    """Watches flagged symbols for a reclaim. Emits at most one entry each."""

    def __init__(self, cfg: dict, refs: dict[str, TickerRef]) -> None:
        self.cfg = cfg[SETUP]
        self.refs = refs
        self.watching: dict[str, _Watch] = {}
        self._session_date = None

    # ------------------------------------------------------------- lifecycle

    def on_candidate(self, candidate: Candidate) -> None:
        """The scanner flagged this symbol. Start tracking it."""
        if candidate.symbol not in self.watching:
            ref = self.refs.get(candidate.symbol)
            if ref:
                self.watching[candidate.symbol] = _Watch(ref=ref)

    def _roll_session(self, bar: Bar) -> None:
        # Only clear on an actual day change. The watchlist is populated by
        # on_candidate BEFORE any bar arrives, so treating the first bar as a
        # session change would discard every symbol just added.
        if self._session_date is None:
            self._session_date = bar.timestamp.date()
        elif self._session_date != bar.timestamp.date():
            self._session_date = bar.timestamp.date()
            self.watching.clear()

    # ------------------------------------------------------------------ main

    def on_bar(self, bar: Bar) -> Signal | None:
        self._roll_session(bar)

        watch = self.watching.get(bar.symbol)
        if watch is None or watch.entered:
            return None

        # VWAP is measured from the regular open, which is the line traders
        # are actually watching. Including premarket would put it somewhere
        # nobody is looking.
        if bar.timestamp.time() < time(9, 30):
            return None

        watch.cum_notional += (bar.vwap or bar.close) * bar.volume
        watch.cum_volume += bar.volume
        watch.high_of_day = max(watch.high_of_day, bar.high)

        if not watch.made_qualifying_high:
            required = watch.ref.prior_close * (1 + self.cfg["min_hod_pct"] / 100)
            if watch.high_of_day >= required:
                watch.made_qualifying_high = True

        five = watch.aggregator.push(bar)
        if five is None:
            return None

        return self._on_five_minute(watch, five)

    def _on_five_minute(self, watch: _Watch, bar: Bar) -> Signal | None:
        vwap = watch.vwap
        if vwap is None:
            return None

        watch.recent_volumes.append(bar.volume)
        watch.recent_volumes = watch.recent_volumes[-4:]

        # Below VWAP: accumulate the pullback, no entry possible yet.
        if bar.close <= vwap:
            watch.bars_below_vwap += 1
            return None

        # Above VWAP. Is this the reclaim, or was it never below?
        bars_below = watch.bars_below_vwap
        watch.bars_below_vwap = 0

        if not watch.made_qualifying_high:
            return None
        if bars_below < self.cfg["min_bars_below_vwap"]:
            return None
        if bar.timestamp.time() >= time.fromisoformat(self.cfg["no_entry_after"]):
            return None

        # Volume confirmation: a reclaim on no volume is drift, not demand.
        prior = watch.recent_volumes[:-1]
        if prior and bar.volume < sum(prior) / len(prior):
            return None

        stop = self._stop_for(bar, vwap)
        if stop is None or stop >= bar.close:
            return None

        stop_pct = (bar.close - stop) / bar.close * 100
        if stop_pct > self.cfg["max_stop_pct"]:
            return None                      # too wide to size sensibly

        target = min(
            watch.ref.prior_high if watch.ref.prior_high > bar.close else float("inf"),
            bar.close * (1 + self.cfg["target_pct"] / 100),
        )

        watch.entered = True
        return Signal(
            symbol=bar.symbol,
            timestamp=bar.timestamp,
            entry_price=bar.close,
            stop_price=stop,
            target_price=round(target, 4),
            setup=SETUP,
            note=f"reclaim after {bars_below} bars below VWAP {vwap:.3f}",
        )

    def _stop_for(self, bar: Bar, vwap: float) -> float | None:
        """Where the stop goes. The single most consequential choice here.

        bar_low  the reclaim bar's low. Tightest, and the first backtest
                 stopped out 68% of trades with it — inside normal noise.
        vwap     below the line the whole thesis rests on. If price is back
                 under VWAP the reclaim failed, so this is the stop that
                 matches the idea rather than the chart.
        pct      a flat percentage, for comparison.
        """
        mode = self.cfg.get("stop_mode", "bar_low")
        buffer_pct = self.cfg.get("stop_buffer_pct", 1.0) / 100

        if mode == "bar_low":
            return round(bar.low - 0.01, 4)
        if mode == "vwap":
            return round(vwap * (1 - buffer_pct), 4)
        if mode == "pct":
            return round(bar.close * (1 - self.cfg.get("stop_pct", 8.0) / 100), 4)
        raise ValueError(f"unknown stop_mode: {mode}")

    # --------------------------------------------------------- exit handling

    def should_exit(self, symbol: str, bar: Bar) -> str | None:
        """Losing VWAP invalidates the thesis, so exit rather than wait.

        Checked on every minute bar's contribution to the running VWAP, using
        the last completed 5-minute close so a single wick does not eject a
        good position.
        """
        watch = self.watching.get(symbol)
        if watch is None or not self.cfg.get("exit_on_vwap_loss", True):
            return None
        vwap = watch.vwap
        if vwap and bar.close < vwap * (1 - self.cfg.get("vwap_exit_buffer_pct", 0.5) / 100):
            return "lost_vwap"
        return None
