"""Setup 2 — bounce on a former runner.

Your description: a stock ran hard days or weeks ago, has been falling since,
and you try to catch the reversal rather than the fall.

Two things make this different from the VWAP reclaim that failed. It requires
a specific prior history — the stock must actually have run and actually have
come down — which is a much narrower universe than "anything the scanner
flagged today". And it enters on a reversal bar rather than a line cross,
which is closer to what the scan data favoured: candidates caught after a
pullback beat ones caught extended.

The daily context comes from data/daily_history, which answers strictly from
bars dated before the session being traded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

from data.daily_history import DailyContext, History
from scanner.scanner import Bar, Candidate, TickerRef
from strategies.base import FiveMinuteAggregator, Signal

SETUP = "bounce"


def ema(previous: float | None, value: float, period: int) -> float:
    k = 2 / (period + 1)
    return value if previous is None else value * k + previous * (1 - k)


@dataclass
class _Watch:
    ref: TickerRef
    context: DailyContext
    aggregator: FiveMinuteAggregator = field(default_factory=FiveMinuteAggregator)

    ema9: float | None = None
    prior_bar: Bar | None = None
    recent_volumes: list = field(default_factory=list)
    session_low: float = float("inf")
    entered: bool = False


class Bounce:
    def __init__(self, cfg: dict, refs: dict[str, TickerRef],
                 history: History | None = None) -> None:
        self.cfg = cfg[SETUP]
        self.refs = refs
        self.history = history
        self.watching: dict[str, _Watch] = {}
        self.rejected: set[str] = set()
        self._session_date = None

    # ------------------------------------------------------------- lifecycle

    def on_candidate(self, candidate: Candidate) -> None:
        symbol = candidate.symbol
        if symbol in self.watching or symbol in self.rejected:
            return

        ref = self.refs.get(symbol)
        if ref is None or self.history is None:
            return

        context = self.history.context(
            symbol,
            candidate.timestamp.date(),
            lookback=self.cfg["lookback_sessions"],
            runner_move_pct=self.cfg["runner_move_pct"],
        )

        # The setup is defined by history, not by today's move. A name that
        # never ran, or that has not come down far enough, is a different
        # trade regardless of what it is doing right now.
        qualifies = (
            context.had_runner
            and context.pct_off_runner_high <= -self.cfg["min_decline_pct"]
            and context.lower_low_streak >= self.cfg["min_lower_low_sessions"]
        )
        if not qualifies:
            self.rejected.add(symbol)
            return

        self.watching[symbol] = _Watch(ref=ref, context=context)

    def _roll_session(self, bar: Bar) -> None:
        if self._session_date is None:
            self._session_date = bar.timestamp.date()
        elif self._session_date != bar.timestamp.date():
            self._session_date = bar.timestamp.date()
            self.watching.clear()
            self.rejected.clear()

    # ------------------------------------------------------------------ main

    def on_bar(self, bar: Bar) -> Signal | None:
        self._roll_session(bar)

        watch = self.watching.get(bar.symbol)
        if watch is None or watch.entered:
            return None
        if bar.timestamp.time() < time(9, 30):
            return None

        watch.session_low = min(watch.session_low, bar.low)

        five = watch.aggregator.push(bar)
        if five is None:
            return None
        return self._on_five_minute(watch, five)

    def _on_five_minute(self, watch: _Watch, bar: Bar) -> Signal | None:
        watch.ema9 = ema(watch.ema9, bar.close, 9)
        watch.recent_volumes.append(bar.volume)
        watch.recent_volumes = watch.recent_volumes[-7:]

        prior, watch.prior_bar = watch.prior_bar, bar
        if prior is None or watch.ema9 is None:
            return None
        if len(watch.recent_volumes) < 4:
            return None                      # not enough baseline to judge volume

        # Reversal bar: takes out the prior bar's high, closes above the
        # 9-EMA, and does it on volume. All three, or it is drift.
        if bar.close <= prior.high:
            return None
        if bar.close <= watch.ema9:
            return None

        baseline = watch.recent_volumes[:-1]
        if bar.volume < self.cfg["volume_multiple"] * (sum(baseline) / len(baseline)):
            return None

        if bar.timestamp.time() >= time.fromisoformat(self.cfg["no_entry_after"]):
            return None

        stop = self._stop_for(bar, watch)
        if stop >= bar.close:
            return None
        if (bar.close - stop) / bar.close * 100 > self.cfg["max_stop_pct"]:
            return None

        watch.entered = True
        return Signal(
            symbol=bar.symbol,
            timestamp=bar.timestamp,
            entry_price=bar.close,
            stop_price=stop,
            target_price=round(bar.close * (1 + self.cfg["target_pct"] / 100), 4),
            setup=SETUP,
            note=(f"{watch.context.pct_off_runner_high:.0f}% off runner high, "
                  f"{watch.context.lower_low_streak} lower lows"),
        )

    def _stop_for(self, bar: Bar, watch: _Watch) -> float:
        """The VWAP reclaim failed largely on stop placement, so this exposes
        the choice from the start rather than hard-coding one guess.
        """
        mode = self.cfg.get("stop_mode", "bar_low")
        if mode == "bar_low":
            return round(bar.low - 0.01, 4)
        if mode == "session_low":
            return round(watch.session_low - 0.01, 4)
        if mode == "pct":
            return round(bar.close * (1 - self.cfg.get("stop_pct", 7.0) / 100), 4)
        raise ValueError(f"unknown stop_mode: {mode}")

    def should_exit(self, symbol: str, bar: Bar) -> str | None:
        """No thesis-invalidation exit. The VWAP reclaim's equivalent rule
        made every parameter set worse, so this one earns its place by test
        rather than by sounding disciplined."""
        return None
