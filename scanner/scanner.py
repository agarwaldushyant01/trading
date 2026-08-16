"""Candidate scanner — the universe generator.

Replaces the Nuntio mosquito screener with something that (a) you own and
(b) can be run over historical data, which is what makes the backtest possible.

Design rule: this module never calls a broker or a data vendor. It consumes
Bar events and emits Candidate events. The replay driver and the live driver
both feed it the same objects, so backtest and live cannot drift apart.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum


class Mode(str, Enum):
    GAP = "gap"            # cumulative move from prior close
    VELOCITY = "velocity"  # rate of change inside a rolling window


class Session(str, Enum):
    PREMARKET = "premarket"
    REGULAR = "regular"
    AFTERHOURS = "afterhours"
    CLOSED = "closed"


@dataclass(frozen=True)
class Bar:
    """One minute of trading. The only input this module understands."""

    symbol: str
    timestamp: datetime          # bar close, timezone-aware ET
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float | None = None


@dataclass(frozen=True)
class TickerRef:
    """Static reference data, refreshed daily, not per bar."""

    symbol: str
    exchange: str
    shares_outstanding: float
    avg_20d_volume: float
    prior_close: float
    prior_high: float
    atr_14: float


@dataclass
class Candidate:
    """A scanner hit, with the full feature snapshot at trigger time.

    Every field here is written to Parquet. Phase 3 joins these rows to
    forward returns; anything not captured now cannot be studied later, so
    the snapshot is deliberately wider than any single strategy needs.
    """

    symbol: str
    timestamp: datetime
    mode: Mode
    session: Session

    price: float
    pct_change_from_prior_close: float
    pct_change_in_window: float
    session_volume: int
    window_volume: int
    rel_volume: float
    vwap: float | None
    above_vwap: bool
    high_of_day: float
    pct_off_high: float

    shares_outstanding: float
    avg_20d_volume: float
    atr_14: float
    prior_close: float
    prior_high: float

    appearances_10d: int = 0     # your "seen it 2-3 times" filter
    appearances_today: int = 0

    def to_row(self) -> dict:
        d = asdict(self)
        d["mode"] = self.mode.value
        d["session"] = self.session.value
        return d


def _to_minutes(clock: str) -> int:
    hours, minutes = clock.split(":")
    return int(hours) * 60 + int(minutes)


def expected_volume_fraction(ts: datetime, curve: dict[str, float]) -> float:
    """What fraction of an average day's volume has normally traded by now.

    Linear interpolation between the anchors in config. Without this,
    relative volume compares volume-so-far against a whole day's average and
    is structurally biased low at every hour except the close.
    """
    points = sorted((_to_minutes(k), v) for k, v in curve.items())
    now = ts.hour * 60 + ts.minute

    if now <= points[0][0]:
        return points[0][1]
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if now <= t1:
            span = t1 - t0
            return v0 if span == 0 else v0 + (v1 - v0) * (now - t0) / span
    return points[-1][1]


@dataclass
class _SymbolState:
    """Rolling per-symbol state. Kept small — this runs on every bar."""

    session_volume: int = 0
    high_of_day: float = 0.0
    low_of_day: float = float("inf")
    cum_pv: float = 0.0          # for VWAP
    cum_vol: int = 0
    window: deque = field(default_factory=lambda: deque(maxlen=10))
    last_alert_at: datetime | None = None
    last_alert_price: float = 0.0
    appearances_today: int = 0
    alerts_by_session: dict = field(default_factory=dict)

    @property
    def vwap(self) -> float | None:
        return self.cum_pv / self.cum_vol if self.cum_vol else None


class Scanner:
    def __init__(self, config: dict, refs: dict[str, TickerRef]) -> None:
        self.cfg = config
        self.refs = refs
        self.state: dict[str, _SymbolState] = defaultdict(_SymbolState)
        # Distinct DAYS a symbol appeared, not alert count. The first run had
        # 482 of 570 candidates in the "4+" bucket purely because the same
        # names re-fired all session, which measured scanner repetition rather
        # than the "seen it 2-3 times" filter it was meant to encode.
        self.appearance_days: dict[str, deque] = defaultdict(lambda: deque(maxlen=40))
        self._session_date = None

    # ------------------------------------------------------------- helpers

    def _session_for(self, ts: datetime) -> Session:
        s = self.cfg["sessions"]
        t = ts.time()
        parse = lambda x: time(*map(int, x.split(":")))
        if parse(s["premarket_start"]) <= t < parse(s["regular_open"]):
            return Session.PREMARKET
        if parse(s["regular_open"]) <= t < parse(s["regular_close"]):
            return Session.REGULAR
        if parse(s["regular_close"]) <= t < parse(s["afterhours_end"]):
            return Session.AFTERHOURS
        return Session.CLOSED

    def _passes_universe(self, ref: TickerRef, price: float) -> bool:
        u = self.cfg["universe"]
        return (
            ref.exchange in u["exchanges"]              # OTC excluded here
            and u["min_price"] <= price <= u["max_price"]
            and ref.shares_outstanding <= u["max_shares_outstanding"]
            and ref.avg_20d_volume >= u["min_avg_20d_volume"]
        )

    def _roll_session(self, ts: datetime) -> None:
        """Reset intraday state at the start of each new trading day."""
        if self._session_date != ts.date():
            self._session_date = ts.date()
            self.state.clear()

    def _suppressed(self, st: _SymbolState, ts: datetime, price: float,
                    session: Session) -> bool:
        """Dedup: a re-alert needs a genuinely new development.

        The earlier version released the lock once the cooldown expired, so a
        name sitting 10% up on heavy volume re-fired every 15 minutes for the
        whole session — 5 alerts per symbol per day, none of them independent
        events. Elapsed time is now necessary but not sufficient: price must
        ALSO have moved materially since the last alert.
        """
        d = self.cfg["dedup"]
        if st.last_alert_at is None:
            return False

        if ts - st.last_alert_at < timedelta(minutes=d["cooldown_minutes"]):
            return True
        # Per session, not per day. A day-wide cap is spent during premarket
        # by a gapper alerting at 04:00, 06:00 and 08:00 — which silences the
        # symbol at 09:30, the moment that matters most for these names.
        cap = d.get("max_alerts_per_session", 3)
        if st.alerts_by_session.get(session.value, 0) >= cap:
            return True
        if st.last_alert_price <= 0:
            return True

        # Absolute move, so a collapse is as much a new event as a run.
        moved = abs(price / st.last_alert_price - 1) * 100
        return moved < d["reescalate_pct"]

    def _appearances_10d(self, symbol: str, ts: datetime) -> int:
        cutoff = ts.date() - timedelta(days=14)   # ~10 trading days
        return sum(1 for day in self.appearance_days[symbol] if day >= cutoff)

    def _record_appearance(self, symbol: str, ts: datetime) -> None:
        """One entry per day, however many times the symbol alerts."""
        days = self.appearance_days[symbol]
        if not days or days[-1] != ts.date():
            days.append(ts.date())

    # ---------------------------------------------------------------- main

    def on_bar(self, bar: Bar) -> Candidate | None:
        """Feed one bar. Returns a Candidate if this bar triggers a scan hit.

        Identical code path for replay and live.
        """
        self._roll_session(bar.timestamp)

        ref = self.refs.get(bar.symbol)
        if ref is None or ref.prior_close <= 0:
            return None

        st = self.state[bar.symbol]
        st.session_volume += bar.volume
        st.high_of_day = max(st.high_of_day, bar.high)
        st.low_of_day = min(st.low_of_day, bar.low)
        st.cum_pv += (bar.vwap or bar.close) * bar.volume
        st.cum_vol += bar.volume
        st.window.append(bar)

        session = self._session_for(bar.timestamp)
        tradeable = self.cfg["sessions"].get("tradeable", ["premarket", "regular"])
        if session.value not in tradeable:
            return None
        if not self._passes_universe(ref, bar.close):
            return None

        pct_from_close = (bar.close / ref.prior_close - 1) * 100
        rel_volume = 0.0
        if ref.avg_20d_volume:
            fraction = max(
                expected_volume_fraction(bar.timestamp, self.cfg["volume_curve"]),
                0.001,                       # floor: avoid dividing by ~zero at 04:01
            )
            rel_volume = st.session_volume / (ref.avg_20d_volume * fraction)

        # --- velocity: rate of change inside a rolling window -------------
        v = self.cfg["velocity"]
        bars_in_window = max(1, v["window_seconds"] // 60)
        recent = list(st.window)[-bars_in_window:]
        window_low = min(b.low for b in recent)
        window_volume = sum(b.volume for b in recent)
        pct_in_window = (bar.close / window_low - 1) * 100 if window_low else 0.0

        hit_velocity = (
            pct_in_window >= v["min_pct_change"]
            and window_volume >= v["min_window_volume"]
            and st.session_volume >= v["min_cumulative_volume"]
        )

        # --- gap: cumulative move from prior close ------------------------
        g = self.cfg["gap"]
        hit_gap = (
            pct_from_close >= g["min_pct_change"]
            and rel_volume >= g["min_rel_volume"]
            and st.session_volume >= g["min_session_volume"]
        )

        if not (hit_velocity or hit_gap):
            return None
        if self._suppressed(st, bar.timestamp, bar.close, session):
            return None

        st.last_alert_at = bar.timestamp
        st.last_alert_price = bar.close
        st.appearances_today += 1
        st.alerts_by_session[session.value] = (
            st.alerts_by_session.get(session.value, 0) + 1
        )
        self._record_appearance(bar.symbol, bar.timestamp)

        vwap = st.vwap
        return Candidate(
            symbol=bar.symbol,
            timestamp=bar.timestamp,
            mode=Mode.VELOCITY if hit_velocity else Mode.GAP,
            session=session,
            price=bar.close,
            pct_change_from_prior_close=round(pct_from_close, 2),
            pct_change_in_window=round(pct_in_window, 2),
            session_volume=st.session_volume,
            window_volume=window_volume,
            rel_volume=round(rel_volume, 2),
            vwap=round(vwap, 4) if vwap else None,
            above_vwap=bool(vwap and bar.close > vwap),
            high_of_day=st.high_of_day,
            pct_off_high=round((bar.close / st.high_of_day - 1) * 100, 2)
            if st.high_of_day
            else 0.0,
            shares_outstanding=ref.shares_outstanding,
            avg_20d_volume=ref.avg_20d_volume,
            atr_14=ref.atr_14,
            prior_close=ref.prior_close,
            prior_high=ref.prior_high,
            appearances_10d=self._appearances_10d(bar.symbol, bar.timestamp),
            appearances_today=st.appearances_today,
        )
