"""Tests for alerts/notify.py and drivers/live.py"""

import sys, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from datetime import datetime
from zoneinfo import ZoneInfo

from alerts.notify import Notifier, format_candidate
from scanner.scanner import Candidate, Mode, Session, TickerRef

REF = TickerRef("AAAA", "NASDAQ", 8_000_000, 1_200_000, 2.00, 2.50, 0.20)
ET = ZoneInfo("America/New_York")


def candidate(**overrides):
    fields = dict(
        symbol="AAAA",
        timestamp=datetime(2026, 8, 17, 9, 45, tzinfo=ET),
        mode=Mode.GAP,
        session=Session.REGULAR,
        price=2.60,
        pct_change_from_prior_close=30.0,
        pct_change_in_window=4.0,
        session_volume=3_000_000,
        window_volume=200_000,
        rel_volume=5.2,
        vwap=2.45,
        above_vwap=True,
        high_of_day=2.80,
        pct_off_high=-7.1,
        shares_outstanding=8_000_000,
        avg_20d_volume=1_200_000,
        atr_14=0.20,
        prior_close=2.00,
        prior_high=2.50,
        appearances_10d=1,
        appearances_today=1,
    )
    fields.update(overrides)
    return Candidate(**fields)


# ---------------------------------------------------------------- formatting

def test_title_carries_symbol_move_and_mode():
    title, _ = format_candidate(candidate(), REF)
    assert "AAAA" in title and "+30%" in title and "GAP" in title


def test_body_reports_vwap_side():
    _, body = format_candidate(candidate(above_vwap=False), REF)
    assert "below VWAP" in body


def test_premarket_is_labelled():
    _, body = format_candidate(candidate(session=Session.PREMARKET), REF)
    assert "PREMKT" in body


def test_repeat_appearances_are_surfaced():
    _, body = format_candidate(candidate(appearances_10d=3), REF)
    assert "seen 3x" in body


def test_single_appearance_is_not_mentioned():
    """Noise on the lock screen — only surface it when it means something."""
    _, body = format_candidate(candidate(appearances_10d=1), REF)
    assert "seen" not in body


def test_float_is_shown_in_millions():
    _, body = format_candidate(candidate(), REF)
    assert "8.0M" in body


def test_size_appears_only_when_the_trade_is_allowed():
    class Sizing:
        allowed, shares, notional, stop_price = True, 1000, 2600.0, 2.34
    _, with_size = format_candidate(candidate(), REF, Sizing())
    assert "ref size" in with_size

    class Rejected:
        allowed, shares, notional, stop_price = False, 0, 0.0, 0.0
    _, without = format_candidate(candidate(), REF, Rejected())
    assert "ref size" not in without


# ------------------------------------------------------------------ delivery

def test_console_channel_needs_no_network():
    assert Notifier({"channel": "console"}).send("t", "b") is True


def test_delivery_failure_does_not_raise():
    """A dropped notification must never stop the scanner."""
    n = Notifier({"channel": "webhook", "webhook_url": "http://127.0.0.1:1",
                  "timeout_seconds": 1})
    assert n.send("t", "b") is False


def test_ntfy_without_a_topic_falls_back_to_console():
    assert Notifier({"channel": "ntfy", "ntfy_topic": ""}).send("t", "b") is True
