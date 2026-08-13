"""Tests for data/reference.py — the pure computation only.

Network calls are not tested here; they need credentials. What is tested is
the maths, which is where a silent error would do the most damage.
"""

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from data.reference import ATR_PERIOD, atr, compute_ref, true_range


def bars(n, high=2.2, low=1.8, close=2.0, volume=1_000_000):
    return [{"high": high, "low": low, "close": close, "volume": volume}
            for _ in range(n)]


# ------------------------------------------------------------- true range

def test_true_range_uses_bar_range_without_prior_close():
    assert round(true_range(2.2, 1.8, None), 4) == 0.4


def test_true_range_captures_a_gap_up():
    """Gapped from 1.50 to a 2.0-2.2 range: TR spans the gap, not just the bar."""
    assert round(true_range(2.2, 2.0, 1.50), 4) == 0.70


def test_true_range_captures_a_gap_down():
    assert round(true_range(1.6, 1.4, 2.00), 4) == 0.60


# -------------------------------------------------------------------- atr

def test_atr_on_flat_bars_equals_the_bar_range():
    assert round(atr(bars(20)), 4) == 0.4


def test_atr_uses_only_the_last_period_bars():
    """Old volatility should not linger once the stock calms down."""
    noisy = [{"high": 5.0, "low": 1.0, "close": 2.0, "volume": 1}] * 30
    calm = [{"high": 2.05, "low": 1.95, "close": 2.0, "volume": 1}] * ATR_PERIOD
    assert round(atr(noisy + calm), 4) == 0.1


def test_atr_of_a_single_bar_is_zero():
    assert atr(bars(1)) == 0.0


# ------------------------------------------------------------- compute_ref

def test_ref_is_none_without_enough_history():
    assert compute_ref("AAAA", "NASDAQ", bars(5), 8e6) is None


def test_ref_carries_prior_session_values():
    history = bars(20)
    history[-1] = {"high": 3.0, "low": 2.5, "close": 2.8, "volume": 5_000_000}
    ref = compute_ref("AAAA", "NASDAQ", history, 8e6)
    assert ref.prior_close == 2.8
    assert ref.prior_high == 3.0


def test_avg_volume_uses_twenty_sessions():
    history = bars(40, volume=100_000)
    history[-20:] = bars(20, volume=1_000_000)
    ref = compute_ref("AAAA", "NASDAQ", history, 8e6)
    assert ref.avg_20d_volume == 1_000_000     # old quiet volume excluded


def test_ref_preserves_exchange_and_share_count():
    ref = compute_ref("AAAA", "NYSE", bars(20), 8_000_000)
    assert ref.exchange == "NYSE"
    assert ref.shares_outstanding == 8_000_000
