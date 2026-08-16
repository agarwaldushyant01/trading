"""Backtest engine — signals in, completed trades out.

Holds the rules that are the same for every strategy: how a fill happens,
when a stop is honoured, what a time stop means, and what the trade cost.

Two conventions here decide whether the results mean anything:

  Fills happen at the NEXT bar's open, never at the price the signal was
  computed from. A backtest that fills at the decision price is reading the
  close of a bar and buying inside it, which is not available in life.

  When a bar's range contains both the stop and the target, the STOP is
  assumed to hit first. One-minute bars cannot tell you the order, and
  assuming the good outcome is how a losing system looks profitable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from risk.sizing import RiskManager
from scanner.scanner import Bar
from strategies.base import Signal


@dataclass
class Trade:
    symbol: str
    setup: str
    entered_at: datetime
    entry_price: float
    shares: int
    stop_price: float
    target_price: float
    exited_at: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return round((self.exit_price - self.entry_price) * self.shares, 2)

    @property
    def pnl_pct(self) -> float:
        if self.exit_price is None:
            return 0.0
        return round((self.exit_price / self.entry_price - 1) * 100, 3)

    def to_row(self) -> dict:
        return {
            "symbol": self.symbol,
            "setup": self.setup,
            "entered_at": self.entered_at,
            "entry_price": self.entry_price,
            "shares": self.shares,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "exited_at": self.exited_at,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
            "r_multiple": round(
                (self.exit_price - self.entry_price)
                / (self.entry_price - self.stop_price), 2
            ) if self.exit_price and self.entry_price > self.stop_price else None,
        }


class Engine:
    def __init__(self, risk: RiskManager, cfg: dict) -> None:
        self.risk = risk
        self.cfg = cfg
        self.slippage = cfg.get("slippage_pct", 0.5) / 100
        self.hard_exit = time.fromisoformat(cfg.get("hard_exit_time", "15:50"))

        self.pending: list[Signal] = []
        self.open: dict[str, Trade] = {}
        self.closed: list[Trade] = []

    # ------------------------------------------------------------- entries

    def submit(self, signal: Signal) -> None:
        if signal.symbol not in self.open:
            self.pending.append(signal)

    def _try_fill(self, bar: Bar) -> None:
        for signal in [s for s in self.pending if s.symbol == bar.symbol]:
            self.pending.remove(signal)

            fill = round(bar.open * (1 + self.slippage), 4)
            if fill <= signal.stop_price:
                continue                      # gapped through the stop overnight

            sized = self.risk.size(
                entry_price=fill,
                stop_price=signal.stop_price,
                atr=0.0,
                avg_20d_volume=self.cfg.get("assumed_adv", 1_000_000),
            )
            if not sized.allowed:
                continue

            self.risk.record_fill()
            self.open[bar.symbol] = Trade(
                symbol=bar.symbol,
                setup=signal.setup,
                entered_at=bar.timestamp,
                entry_price=fill,
                shares=sized.shares,
                stop_price=sized.stop_price,
                target_price=signal.target_price,
            )

    # -------------------------------------------------------------- exits

    def _close(self, trade: Trade, ts: datetime, price: float, reason: str) -> None:
        trade.exited_at = ts
        trade.exit_price = round(price * (1 - self.slippage), 4)
        trade.exit_reason = reason
        self.risk.record_close(trade.pnl)
        self.closed.append(trade)
        self.open.pop(trade.symbol, None)

    def _check_exits(self, bar: Bar) -> None:
        trade = self.open.get(bar.symbol)
        if trade is None or bar.timestamp <= trade.entered_at:
            return

        # Stop before target when a single bar spans both. Pessimistic by
        # design: the bar cannot tell us which came first.
        if bar.low <= trade.stop_price:
            self._close(trade, bar.timestamp, trade.stop_price, "stop")
        elif bar.high >= trade.target_price:
            self._close(trade, bar.timestamp, trade.target_price, "target")
        elif bar.timestamp.time() >= self.hard_exit:
            self._close(trade, bar.timestamp, bar.close, "time_stop")

    def exit_now(self, symbol: str, bar: Bar, reason: str) -> None:
        """Strategy-driven exit, e.g. losing VWAP after a reclaim."""
        trade = self.open.get(symbol)
        if trade and bar.timestamp > trade.entered_at:
            self._close(trade, bar.timestamp, bar.close, reason)

    # --------------------------------------------------------------- main

    def on_bar(self, bar: Bar) -> None:
        self._check_exits(bar)
        self._try_fill(bar)

    def end_session(self, last_bars: dict[str, Bar]) -> None:
        """Flatten anything still open. Nothing carries overnight in v1."""
        for symbol, trade in list(self.open.items()):
            bar = last_bars.get(symbol)
            if bar:
                self._close(trade, bar.timestamp, bar.close, "session_end")
        self.pending.clear()
        self.risk.end_session()


def summarize(trades: list[Trade]) -> dict:
    """Headline numbers. Expectancy in R is the one that matters —
    it says what one unit of risk returns, independent of position size."""
    if not trades:
        return {"trades": 0}

    rows = [t.to_row() for t in trades]
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] <= 0]
    r_values = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]

    # Standard error of the mean R. Without it, a small sample reads like a
    # verdict: -0.12 over 100 trades and -0.12 over 1,000 look identical on
    # the page, but only one of them is a result.
    stderr_r = None
    if len(r_values) > 1:
        mean_r = sum(r_values) / len(r_values)
        variance = sum((r - mean_r) ** 2 for r in r_values) / (len(r_values) - 1)
        stderr_r = round((variance / len(r_values)) ** 0.5, 3)

    return {
        "trades": len(rows),
        "hit_rate": round(len(wins) / len(rows) * 100, 1),
        "stderr_r": stderr_r,
        "total_pnl": round(sum(r["pnl"] for r in rows), 2),
        "avg_win": round(sum(r["pnl"] for r in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(r["pnl"] for r in losses) / len(losses), 2)
        if losses else 0.0,
        "expectancy": round(sum(r["pnl"] for r in rows) / len(rows), 2),
        "expectancy_r": round(sum(r_values) / len(r_values), 3) if r_values else None,
        "best": round(max(r["pnl"] for r in rows), 2),
        "worst": round(min(r["pnl"] for r in rows), 2),
    }
