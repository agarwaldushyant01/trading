"""Tests for scanner/scanner.py"""

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from datetime import datetime, timedelta

from scanner.scanner import (
    Bar, Mode, Scanner, Session, TickerRef, expected_volume_fraction,
)

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
    "volume_curve": {"04:00": 0.002, "08:00": 0.015, "09:30": 0.030,
                     "10:00": 0.160, "12:00": 0.350, "16:00": 1.000,
                     "20:00": 1.020},
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
    """Past the cooldown AND materially higher: a genuine new event."""
    s = scanner()
    s.on_bar(bar(2.60, 4_000_000, minute=0))
    assert s.on_bar(bar(3.10, 100_000, minute=20)) is not None


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


# ------------------------------------------------------- time-of-day volume

CURVE = CFG["volume_curve"]


def at(hour, minute=0):
    return datetime(2026, 3, 10, hour, minute)


def test_curve_interpolates_between_anchors():
    """10:00 is 0.16, 12:00 is 0.35 — 11:00 sits halfway."""
    assert round(expected_volume_fraction(at(11), CURVE), 4) == 0.255


def test_curve_returns_anchor_values_exactly():
    assert expected_volume_fraction(at(10), CURVE) == 0.160


def test_curve_clamps_before_the_first_anchor():
    assert expected_volume_fraction(at(1), CURVE) == 0.002


def test_curve_clamps_after_the_last_anchor():
    assert expected_volume_fraction(at(23), CURVE) == 1.020


def test_relative_volume_is_scaled_to_time_of_day():
    """800k by 10:00 against a 1M daily average is 5x normal, not 0.8x.

    Only 16% of a day's volume normally trades by 10:00, so the denominator
    is 160k, not 1M.
    """
    s = scanner()
    c = s.on_bar(bar(2.60, 800_000, hour=10))
    assert c is not None
    assert 4.9 < c.rel_volume < 5.1


def test_early_volume_now_clears_the_threshold():
    """The bias this fixes: a genuine morning surge used to read as nothing."""
    s = scanner()
    c = s.on_bar(bar(2.60, 600_000, hour=10))
    assert c is not None and c.rel_volume >= CFG["gap"]["min_rel_volume"]


# --------------------------------------------------------- session filtering

def test_afterhours_is_excluded():
    """A third of the first run's candidates were after-hours, untraded."""
    s = scanner()
    assert s.on_bar(bar(2.60, 4_000_000, hour=17)) is None


def test_regular_session_still_fires():
    s = scanner()
    assert s.on_bar(bar(2.60, 4_000_000, hour=14)) is not None


# ------------------------------------------------------- appearances by day

def test_repeat_alerts_same_day_count_once():
    """Twenty alerts on one name in one session is one appearance, not twenty."""
    s = scanner()
    for minute in (0, 20, 40):
        s.on_bar(bar(2.60 + minute / 100, 4_000_000, minute=minute))
    assert s._appearances_10d("AAAA", datetime(2026, 3, 10, 11, 0)) == 1


def test_appearances_count_distinct_days():
    s = scanner()
    for day in range(3):
        ts = datetime(2026, 3, 10 + day, 10, 0)
        s.on_bar(Bar("AAAA", ts, 2.60, 2.60, 2.60, 2.60, 4_000_000))
        s.on_bar(Bar("AAAA", ts.replace(minute=30), 3.10, 3.10, 3.10, 3.10, 900_000))
    assert s._appearances_10d("AAAA", datetime(2026, 3, 12, 11, 0)) == 3


def test_appearances_age_out():
    s = scanner()
    s.on_bar(Bar("AAAA", datetime(2026, 1, 5, 10, 0), 2.6, 2.6, 2.6, 2.6, 4_000_000))
    assert s._appearances_10d("AAAA", datetime(2026, 3, 10, 10, 0)) == 0


# ------------------------------------------------------- dedup after cooldown

def test_elapsed_time_alone_does_not_retrigger():
    """The leak that turned 270 events into 1,365: a name parked 30% up
    re-fired every cooldown period all session."""
    s = scanner()
    assert s.on_bar(bar(2.60, 4_000_000, minute=0)) is not None
    assert s.on_bar(bar(2.62, 500_000, minute=20)) is None
    assert s.on_bar(bar(2.64, 500_000, minute=45)) is None


def test_material_move_after_cooldown_does_retrigger():
    s = scanner()
    s.on_bar(bar(2.60, 4_000_000, minute=0))
    assert s.on_bar(bar(3.10, 500_000, minute=20)) is not None


def test_a_collapse_also_counts_as_a_new_event():
    """Absolute move: a name giving back 20% is news too."""
    s = scanner()
    s.on_bar(bar(3.20, 4_000_000, minute=0))          # +60% over prior close
    assert s.on_bar(bar(2.60, 500_000, minute=20)) is not None


def test_session_alert_ceiling_applies():
    s = scanner()
    prices = [2.60, 3.10, 3.70, 4.40, 5.20]            # each ~+19%
    fired = [s.on_bar(bar(p, 4_000_000, hour=10 + i))
             for i, p in enumerate(prices)]
    assert sum(1 for c in fired if c) == 3


def test_premarket_alerts_do_not_exhaust_the_regular_session():
    """A gapper firing three times before 09:30 must still be able to alert
    at the open — that is the moment these names actually trade."""
    s = scanner()
    for i, price in enumerate([2.60, 3.10, 3.70]):
        assert s.on_bar(bar(price, 4_000_000, hour=5 + i)) is not None
    assert s.on_bar(bar(4.40, 4_000_000, hour=6)) is None      # premarket capped
    assert s.on_bar(bar(4.40, 4_000_000, hour=10)) is not None  # open is fresh
