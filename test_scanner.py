"""Tests for scanner/scanner.py"""

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from datetime import datetime, timedelta

from scanner.scanner import Bar, Mode, Scanner, Session, TickerRef

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
    "dedup": {"cooldown_minutes": 15, "reescalate_pct": 15.0},
    "sessions": {"premarket_start": "04:00", "regular_open": "09:30",
                 "regular_close": "16:00", "afterhours_end": "20:00"},
}

def ref(symbol="AAAA", exchange="NASDAQ", so=10_000_000, adv=1_000_000,
        prior_close=2.00):
    return TickerRef(symbol, exchange, so, adv, prior_close, 2.50, 0.20)

def scanner(refs=None):
    return Scanner(CFG, refs or {"AAAA": ref()})

def bar(price, volume, minute=0, hour=10, low=None, symbol="AAAA"):
    ts = datetime(2026, 3, 10, hour, minute)
    return Bar(symbol, ts, price, price, low if low is not None else price,
               price, volume)


# ------------------------------------------------------------------ universe

def test_otc_is_excluded():
    s = scanner({"AAAA": ref(exchange="OTC")})
    assert s.on_bar(bar(2.60, 500_000)) is None


def test_price_above_ceiling_excluded():
    s = scanner({"AAAA": ref(prior_close=25.00)})
    assert s.on_bar(bar(30.00, 500_000)) is None


def test_high_share_count_excluded():
    s = scanner({"AAAA": ref(so=500_000_000)})
    assert s.on_bar(bar(2.60, 500_000)) is None


def test_illiquid_name_excluded():
    s = scanner({"AAAA": ref(adv=50_000)})
    assert s.on_bar(bar(2.60, 500_000)) is None


# ----------------------------------------------------------------- gap mode

def test_gap_triggers_on_move_with_volume():
    s = scanner()
    c = s.on_bar(bar(2.60, 4_000_000))          # +30% on 4x rel volume
    assert c is not None and c.mode is Mode.GAP
    assert c.pct_change_from_prior_close == 30.0


def test_gap_needs_relative_volume():
    s = scanner()
    assert s.on_bar(bar(2.60, 150_000)) is None  # big move, thin volume


def test_gap_needs_the_move():
    s = scanner()
    assert s.on_bar(bar(2.05, 4_000_000)) is None  # volume but only +2.5%


# ------------------------------------------------------------ velocity mode

def test_velocity_triggers_on_vertical_move():
    s = scanner()
    s.on_bar(bar(2.00, 300_000, minute=0))
    c = s.on_bar(bar(2.90, 200_000, minute=1, low=2.00))   # +45% in one bar
    assert c is not None and c.mode is Mode.VELOCITY
    assert c.pct_change_in_window == 45.0


def test_velocity_fires_premarket():
    s = scanner()
    s.on_bar(bar(2.00, 300_000, hour=7, minute=0))
    c = s.on_bar(bar(2.90, 200_000, hour=7, minute=1, low=2.00))
    assert c is not None and c.session is Session.PREMARKET


def test_velocity_needs_window_volume():
    s = scanner()
    s.on_bar(bar(2.00, 300_000, minute=0))
    assert s.on_bar(bar(2.90, 5_000, minute=1, low=2.00)) is None


# --------------------------------------------------------------------- dedup

def test_repeat_within_cooldown_is_suppressed():
    s = scanner()
    assert s.on_bar(bar(2.60, 4_000_000, minute=0)) is not None
    assert s.on_bar(bar(2.62, 100_000, minute=5)) is None


def test_material_extension_reescalates():
    s = scanner()
    s.on_bar(bar(2.60, 4_000_000, minute=0))
    c = s.on_bar(bar(3.10, 100_000, minute=5))   # +19% further
    assert c is not None


def test_cooldown_expires():
    s = scanner()
    s.on_bar(bar(2.60, 4_000_000, minute=0))
    assert s.on_bar(bar(2.62, 100_000, minute=20)) is not None


# ------------------------------------------------------------------ features

def test_snapshot_carries_vwap_and_appearances():
    s = scanner()
    c = s.on_bar(bar(2.60, 4_000_000))
    assert c.vwap is not None and c.above_vwap is False   # first bar: price==vwap
    assert c.appearances_today == 1
    assert c.appearances_10d == 1


def test_appearances_accumulate_across_days():
    s = scanner()
    for day in range(3):
        ts = datetime(2026, 3, 10 + day, 10, 0)
        s.on_bar(Bar("AAAA", ts, 2.60, 2.60, 2.60, 2.60, 4_000_000))
    assert s._appearances_10d("AAAA", datetime(2026, 3, 12, 10, 0)) == 3


def test_session_state_resets_daily():
    s = scanner()
    s.on_bar(bar(2.60, 4_000_000))
    day2 = Bar("AAAA", datetime(2026, 3, 11, 10, 0), 2.10, 2.10, 2.10, 2.10, 500_000)
    s.on_bar(day2)
    assert s.state["AAAA"].session_volume == 500_000   # not cumulative


def test_closed_session_never_fires():
    s = scanner()
    assert s.on_bar(bar(2.60, 4_000_000, hour=2)) is None
