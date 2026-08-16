"""Tests for drivers/replay.py — pre-screening, forward metrics, ordering."""

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from datetime import datetime
from zoneinfo import ZoneInfo

from drivers.replay import ET, forward_metrics, prescreen, replay
from scanner.scanner import Bar, Scanner, TickerRef

CFG = {
    "universe": {
        "exchanges": ["NASDAQ", "NYSE", "AMEX"],
        "min_price": 0.50, "max_price": 20.00,
        "max_shares_outstanding": 30_000_000,
        "min_avg_20d_volume": 200_000,
    },
    "gap": {"min_pct_change": 10.0, "min_rel_volume": 3.0,
            "min_session_volume": 100_000},
    "velocity": {"window_seconds": 60, "min_pct_change": 30.0,
                 "min_window_volume": 50_000, "min_cumulative_volume": 200_000},
    "dedup": {"cooldown_minutes": 15, "reescalate_pct": 15.0,
              "max_alerts_per_session": 3},
    "sessions": {"premarket_start": "04:00", "regular_open": "09:30",
                 "regular_close": "16:00", "afterhours_end": "20:00",
                 "tradeable": ["premarket", "regular"]},
    "volume_curve": {"04:00": 0.002, "09:30": 0.030, "10:00": 0.160,
                     "16:00": 1.000, "20:00": 1.020},
}

REF = TickerRef("AAAA", "NASDAQ", 8_000_000, 1_000_000, 2.00, 2.50, 0.20)


def daily(high=2.60, low=1.95, close=2.50, volume=3_000_000):
    return {"high": high, "low": low, "close": close, "volume": volume}


def bar(price, minute, symbol="AAAA", low=None, high=None, volume=500_000):
    ts = datetime(2026, 3, 10, 9, 30, tzinfo=ET).replace(minute=0) \
         .replace(hour=9, minute=30) + __import__("datetime").timedelta(minutes=minute)
    return Bar(symbol, ts, price, high or price, low or price, price, volume)


# ------------------------------------------------------------- pre-screen

def test_quiet_day_is_screened_out():
    """A stock whose whole day ranged 3% cannot have fired anything."""
    assert prescreen(daily(high=2.05, low=1.99, close=2.02), REF, CFG) is False


def test_gap_day_passes():
    assert prescreen(daily(high=2.60, low=2.10), REF, CFG) is True


def test_intraday_range_passes_even_without_a_gap():
    """Opened flat, then ran 40% off the low and gave it back."""
    assert prescreen(daily(high=2.10, low=1.45, close=1.50), REF, CFG) is True


def test_price_above_the_band_is_screened_out():
    ref = TickerRef("BIGC", "NYSE", 8e6, 1e6, 50.00, 55.0, 1.0)
    assert prescreen(daily(high=80, low=55, close=70), ref, CFG) is False


def test_volume_floor_applies():
    assert prescreen(daily(volume=50_000), REF, CFG) is False


def test_zero_prior_close_is_screened_out():
    ref = TickerRef("NEWW", "NASDAQ", 8e6, 1e6, 0.0, 0.0, 0.0)
    assert prescreen(daily(), ref, CFG) is False


# --------------------------------------------------------- forward metrics

def forward(prices):
    return [bar(p, i + 1, low=p * 0.98, high=p * 1.02) for i, p in enumerate(prices)]


def test_forward_returns_at_each_horizon():
    metrics = forward_metrics(2.00, forward([2.10] * 60))
    assert metrics["fwd_5m_pct"] == 5.0
    assert metrics["fwd_60m_pct"] == 5.0


def test_missing_horizon_is_none_not_zero():
    """A trigger at 15:58 has no 60-minute forward return. That is not flat."""
    metrics = forward_metrics(2.00, forward([2.10] * 10))
    assert metrics["fwd_5m_pct"] == 5.0
    assert metrics["fwd_15m_pct"] is None
    assert metrics["fwd_60m_pct"] is None


def test_mae_captures_the_worst_drawdown():
    """Dipped to 1.70 before recovering: MAE is -15%, not the +5% close."""
    prices = [2.00, 1.80, 2.00, 2.10]
    bars = [bar(p, i + 1, low=p * 0.965, high=p) for i, p in enumerate(prices)]
    metrics = forward_metrics(2.00, bars)
    assert metrics["mae_pct"] == -13.15
    assert metrics["fwd_5m_pct"] is None


def test_mfe_captures_the_best_excursion():
    prices = [2.00, 2.60, 2.10]
    bars = [bar(p, i + 1, low=p, high=p * 1.05) for i, p in enumerate(prices)]
    assert forward_metrics(2.00, bars)["mfe_pct"] == 36.5


def test_no_forward_bars_yields_nulls():
    metrics = forward_metrics(2.00, [])
    assert metrics["mae_pct"] is None
    assert metrics["bars_available"] == 0


def test_bars_available_flags_truncated_windows():
    """Lets the analysis exclude end-of-day triggers rather than mis-average."""
    assert forward_metrics(2.00, forward([2.0] * 12))["bars_available"] == 12


# ------------------------------------------------------------------ replay

def test_replay_interleaves_symbols_by_time():
    """Two symbols must be fed in clock order, not one symbol then the other."""
    refs = {
        "AAAA": REF,
        "BBBB": TickerRef("BBBB", "NASDAQ", 5_000_000, 1_000_000, 1.00, 1.20, 0.10),
    }
    bars = {
        "AAAA": [bar(2.00, 0, volume=400_000), bar(2.90, 1, low=2.00, volume=400_000)],
        "BBBB": [bar(1.00, 0, "BBBB", volume=400_000),
                 bar(1.45, 1, "BBBB", low=1.00, volume=400_000)],
    }
    rows = replay(bars, Scanner(CFG, refs))
    assert {r["symbol"] for r in rows} == {"AAAA", "BBBB"}


def test_replay_attaches_forward_returns_to_each_hit():
    refs = {"AAAA": REF}
    bars = {"AAAA": [
        bar(2.00, 0, volume=400_000),
        bar(2.90, 1, low=2.00, volume=400_000),      # velocity trigger
        bar(3.20, 2, volume=100_000),
        bar(3.50, 3, volume=100_000),                # inside cooldown: no re-fire
    ]}
    rows = replay(bars, Scanner(CFG, refs))
    assert len(rows) == 1
    assert rows[0]["mode"] == "velocity"
    assert rows[0]["mfe_pct"] is not None
    assert rows[0]["bars_available"] == 2             # only two bars follow


def test_replay_returns_nothing_when_no_bars():
    assert replay({}, Scanner(CFG, {"AAAA": REF})) == []


def test_prescreen_drops_high_share_counts():
    """Static filters belong here, before minute bars are fetched."""
    ref = TickerRef("BIGC", "NASDAQ", 900_000_000, 1_000_000, 2.00, 2.5, 0.2)
    assert prescreen(daily(), ref, CFG) is False


def test_prescreen_drops_illiquid_names():
    ref = TickerRef("THIN", "NASDAQ", 8_000_000, 50_000, 2.00, 2.5, 0.2)
    assert prescreen(daily(), ref, CFG) is False
