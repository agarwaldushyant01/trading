"""Position sizing and account-level risk gates.

Single source of truth for how big a trade is and whether it is allowed at all.
No strategy computes its own share count.

Config lives in config/risk.yaml; the dataclass below mirrors it so the defaults
are visible and testable without a file.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class Reject(str, Enum):
    """Why a trade was not taken. Logged on every rejection."""

    OK = "ok"
    DAILY_LOSS_LIMIT = "daily_loss_limit_hit"
    INSUFFICIENT_BUDGET = "risk_exceeds_remaining_daily_budget"
    MAX_POSITIONS = "max_concurrent_positions"
    STOP_TOO_WIDE = "stop_further_than_max_stop_pct"
    STOP_ABOVE_ENTRY = "stop_not_below_entry"
    ZERO_SHARES = "size_rounded_to_zero"
    CONSECUTIVE_LOSING_DAYS = "consecutive_losing_days_halt"


@dataclass(frozen=True)
class RiskConfig:
    equity: float = 50_000.0
    risk_per_trade_pct: float = 0.005      # 0.5%  -> $250
    daily_loss_limit_pct: float = 0.02     # 2%    -> $1,000
    max_concurrent_positions: int = 3
    max_position_pct: float = 0.10         # 10% of equity in one name
    max_pct_of_adv: float = 0.01           # 1% of 20-day average volume
    max_stop_pct: float = 0.20             # skip if stop is >20% from entry
    min_stop_atr_mult: float = 0.5         # widen stops tighter than 0.5 ATR
    max_consecutive_losing_days: int = 3


@dataclass(frozen=True)
class Sizing:
    """Result of a sizing request. shares == 0 means do not trade."""

    shares: int
    stop_price: float
    risk_dollars: float
    notional: float
    reject: Reject
    binding_cap: str | None = None   # which cap set the size, for diagnostics

    @property
    def allowed(self) -> bool:
        return self.shares > 0 and self.reject is Reject.OK


class RiskManager:
    """Tracks intraday state and answers 'how many shares, if any'.

    Deliberately has no broker or market-data dependency so it can be unit
    tested and driven identically by the replay and live drivers.
    """

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.cfg = config or RiskConfig()
        self.realized_pnl_today = 0.0
        self.open_positions = 0
        self.consecutive_losing_days = 0
        self.halted = False

    # ---------------------------------------------------------------- state

    def start_session(self, equity: float | None = None) -> None:
        """Call once at the start of each session."""
        if equity is not None:
            self.cfg = RiskConfig(**{**self.cfg.__dict__, "equity": equity})
        self.realized_pnl_today = 0.0
        self.halted = (
            self.consecutive_losing_days >= self.cfg.max_consecutive_losing_days
        )

    def end_session(self) -> None:
        if self.realized_pnl_today < 0:
            self.consecutive_losing_days += 1
        else:
            self.consecutive_losing_days = 0

    def record_fill(self) -> None:
        self.open_positions += 1

    def record_close(self, realized_pnl: float) -> None:
        self.open_positions = max(0, self.open_positions - 1)
        self.realized_pnl_today += realized_pnl

    @property
    def daily_loss_limit(self) -> float:
        return self.cfg.equity * self.cfg.daily_loss_limit_pct

    @property
    def daily_budget_left(self) -> float:
        """Risk dollars still available today. Only losses consume it."""
        used = max(0.0, -self.realized_pnl_today)
        return max(0.0, self.daily_loss_limit - used)

    @property
    def should_flatten(self) -> bool:
        """True when the day is over: close everything, take no new trades."""
        return self.halted or self.daily_budget_left <= 0

    # --------------------------------------------------------------- sizing

    def size(
        self,
        entry_price: float,
        stop_price: float,
        atr: float,
        avg_20d_volume: float,
        risk_multiplier: float = 1.0,
    ) -> Sizing:
        """Return the share count for a candidate trade.

        risk_multiplier scales the per-trade risk: 1.0 normal, 0.5 for
        overnight holds, 0.25 for Setup 3 base pre-buys.
        """
        nothing = lambda why: Sizing(0, stop_price, 0.0, 0.0, why)

        if self.halted:
            return nothing(Reject.CONSECUTIVE_LOSING_DAYS)
        if self.daily_budget_left <= 0:
            return nothing(Reject.DAILY_LOSS_LIMIT)
        if self.open_positions >= self.cfg.max_concurrent_positions:
            return nothing(Reject.MAX_POSITIONS)
        if stop_price >= entry_price:
            return nothing(Reject.STOP_ABOVE_ENTRY)

        # Widen a stop that sits inside the stock's own noise. A 5% stop on a
        # name that swings 12% a day is not a stop, it is a coin flip.
        min_distance = self.cfg.min_stop_atr_mult * atr
        stop_distance = entry_price - stop_price
        if atr > 0 and stop_distance < min_distance:
            stop_distance = min_distance
            stop_price = entry_price - stop_distance

        if stop_distance > entry_price * self.cfg.max_stop_pct:
            return nothing(Reject.STOP_TOO_WIDE)

        risk_dollars = self.cfg.equity * self.cfg.risk_per_trade_pct
        risk_dollars *= risk_multiplier

        # Reject rather than silently downsize: a trade taken at a fraction of
        # its intended size has a different risk/reward than the one tested.
        if risk_dollars > self.daily_budget_left:
            return nothing(Reject.INSUFFICIENT_BUDGET)

        shares = math.floor(risk_dollars / stop_distance)
        binding = "risk"

        concentration_cap = math.floor(
            self.cfg.equity * self.cfg.max_position_pct / entry_price
        )
        if concentration_cap < shares:
            shares, binding = concentration_cap, "concentration"

        liquidity_cap = math.floor(avg_20d_volume * self.cfg.max_pct_of_adv)
        if liquidity_cap < shares:
            shares, binding = liquidity_cap, "liquidity"

        if shares <= 0:
            return nothing(Reject.ZERO_SHARES)

        return Sizing(
            shares=shares,
            stop_price=round(stop_price, 4),
            risk_dollars=round(shares * stop_distance, 2),
            notional=round(shares * entry_price, 2),
            reject=Reject.OK,
            binding_cap=binding,
        )
