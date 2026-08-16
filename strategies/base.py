"""Shared strategy plumbing.

A strategy watches symbols the scanner has flagged and decides whether the
setup it is looking for has actually appeared. It emits Signals; it never
sizes a position, places an order, or knows what the account balance is.
Sizing lives in risk/, execution lives in execution/.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from scanner.scanner import Bar


@dataclass(frozen=True)
class Signal:
    """An entry the strategy wants to take, before sizing.

    entry_price is indicative — the engine fills at the next bar's open plus
    slippage, because a strategy that could fill at the price it decided on
    is a strategy that cheats.
    """

    symbol: str
    timestamp: datetime
    entry_price: float
    stop_price: float
    target_price: float
    setup: str
    note: str = ""


@dataclass
class FiveMinuteAggregator:
    """Rolls 1-minute bars into 5-minute bars.

    Bars close on the clock, not on a counter: 09:30-09:34 is one bar
    regardless of whether every minute printed. Thinly traded names skip
    minutes constantly, and counting bars instead of watching the clock
    silently shifts their whole timeline.
    """

    pending: list[Bar] = field(default_factory=list)

    def push(self, bar: Bar) -> Bar | None:
        """Add a minute bar; return a 5-minute bar when one completes."""
        if self.pending and self.pending[0].timestamp.minute // 5 != bar.timestamp.minute // 5:
            completed = self._collapse()
            self.pending = [bar]
            return completed

        self.pending.append(bar)
        if bar.timestamp.minute % 5 == 4:
            completed = self._collapse()
            self.pending = []
            return completed
        return None

    def _collapse(self) -> Bar:
        bars = self.pending
        volume = sum(b.volume for b in bars)
        notional = sum((b.vwap or b.close) * b.volume for b in bars)
        return Bar(
            symbol=bars[0].symbol,
            timestamp=bars[-1].timestamp,
            open=bars[0].open,
            high=max(b.high for b in bars),
            low=min(b.low for b in bars),
            close=bars[-1].close,
            volume=volume,
            vwap=notional / volume if volume else bars[-1].close,
        )
