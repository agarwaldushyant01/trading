"""Tests for data/daily_history.py and strategies/bounce.py"""

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from datetime import date, datetime, timedelta

from data.daily_history import DailyBar, History, build_context
from scanner.scanner import Bar, TickerRef
from strategies.bounce import Bounce, ema

REF = TickerRef("AAAA", "NASDAQ", 8_000_000, 1_000_000, 1.00, 1.20, 0.10)

CFG = {
    "bounce": {
        "lookback_sessions": 30,
        "runner_move_pct": 50.0,
        "min_decline_pct": 40.0,
        "min_lower_low_sessions": 2,
        "volume_multiple": 2.0,
        "no_entry_after": "15:00",
        "stop_mode": "pct",
        "stop_pct": 7.0,
        "max_stop_pct": 12.0,
        "target_pct": 25.0,
    }
}


def daily(day_offset, high, low, close, volume=1_000_000):
    return DailyBar(date(2026, 2, 1) + timedelta(days=day_offset),
                    high, low, close, volume)


def runner_then_decline():
    """One 100% spike day, then a long grind down."""
    bars = [daily(i, 1.05, 0.95, 1.00) for i in range(5)]
    bars.append(daily(5, 2.00, 1.00, 1.90))            # the runner day
    for i in range(6, 20):                              # lower low each session
        price = 1.90 - (i - 5) * 0.09
        bars.append(daily(i, price + 0.05, price - 0.05, price))
    return bars


# ------------------------------------------------------------------ context

def test_runner_day_is_detected():
    ctx = build_context(runner_then_decline(), date(2026, 2, 25))
    assert ctx.had_runner is True
    assert ctx.runner_high == 2.00


def test_decline_from_the_peak_is_measured():
    ctx = build_context(runner_then_decline(), date(2026, 2, 25))
    assert ctx.pct_off_runner_high < -40


def test_lower_low_streak_counts_consecutive_sessions():
    ctx = build_context(runner_then_decline(), date(2026, 2, 25))
    assert ctx.lower_low_streak >= 5


def test_streak_breaks_on_a_higher_low():
    bars = runner_then_decline()
    bars.append(daily(20, 1.00, 0.90, 0.98))          # higher low, reversal
    ctx = build_context(bars, date(2026, 2, 26))
    assert ctx.lower_low_streak == 0


def test_quiet_stock_is_not_a_runner():
    bars = [daily(i, 1.02, 0.98, 1.00) for i in range(20)]
    assert build_context(bars, date(2026, 2, 25)).had_runner is False


def test_context_ignores_bars_on_and_after_the_date():
    """The look-ahead guard. A spike today must not qualify today."""
    bars = [daily(i, 1.02, 0.98, 1.00) for i in range(10)]
    bars.append(daily(10, 5.00, 1.00, 4.80))          # huge move ON the date
    as_of = bars[-1].day
    assert build_context(bars, as_of).had_runner is False
    assert build_context(bars, as_of + timedelta(days=1)).had_runner is True


def test_thin_history_returns_a_safe_default():
    assert build_context([daily(0, 1, 1, 1)], date(2026, 2, 25)).had_runner is False


# ---------------------------------------------------------------------- ema

def test_ema_seeds_with_the_first_value():
    assert ema(None, 2.0, 9) == 2.0


def test_ema_moves_toward_new_values():
    value = 2.0
    for _ in range(20):
        value = ema(value, 3.0, 9)
    assert 2.9 < value < 3.0


# ----------------------------------------------------------------- strategy

class FakeCandidate:
    def __init__(self, symbol="AAAA", day=date(2026, 2, 25)):
        self.symbol = symbol
        self.timestamp = datetime.combine(day, datetime.min.time()).replace(hour=10)


def history(bars=None):
    return History({"AAAA": bars if bars is not None else runner_then_decline()})


def bar(price, offset, low=None, high=None, volume=100_000, day=date(2026, 2, 25)):
    ts = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=30) \
         + timedelta(minutes=offset)
    return Bar("AAAA", ts, price, high or price, low or price, price, volume, price)


def feed(strategy, bars):
    return [s for s in (strategy.on_bar(b) for b in bars) if s]


def reversal_session():
    """Twenty quiet bars, then a bar that takes out the prior high on volume."""
    bars = [bar(0.60, i, low=0.59, high=0.61, volume=50_000) for i in range(20)]
    bars += [bar(0.62 + i * 0.01, 20 + i, low=0.60, high=0.63 + i * 0.01,
                 volume=400_000) for i in range(5)]
    return bars


def test_qualifying_runner_produces_a_signal():
    s = Bounce(CFG, {"AAAA": REF}, history())
    s.on_candidate(FakeCandidate())
    assert len(feed(s, reversal_session())) == 1


def test_non_runner_is_never_watched():
    quiet = [daily(i, 1.02, 0.98, 1.00) for i in range(20)]
    s = Bounce(CFG, {"AAAA": REF}, history(quiet))
    s.on_candidate(FakeCandidate())
    assert "AAAA" not in s.watching
    assert feed(s, reversal_session()) == []


def test_runner_that_has_not_declined_enough_is_rejected():
    bars = [daily(i, 1.05, 0.95, 1.00) for i in range(5)]
    bars.append(daily(5, 2.00, 1.00, 1.90))
    bars += [daily(i, 1.85, 1.75, 1.80) for i in range(6, 20)]   # only -10%
    s = Bounce(CFG, {"AAAA": REF}, history(bars))
    s.on_candidate(FakeCandidate())
    assert "AAAA" not in s.watching


def test_no_signal_without_a_volume_surge():
    s = Bounce(CFG, {"AAAA": REF}, history())
    s.on_candidate(FakeCandidate())
    bars = [bar(0.60, i, low=0.59, high=0.61, volume=50_000) for i in range(20)]
    bars += [bar(0.62 + i * 0.01, 20 + i, low=0.60, high=0.63,
                 volume=50_000) for i in range(5)]
    assert feed(s, bars) == []


def test_only_one_entry_per_symbol_per_session():
    s = Bounce(CFG, {"AAAA": REF}, history())
    s.on_candidate(FakeCandidate())
    bars = reversal_session()
    bars += [bar(0.75 + i * 0.02, 30 + i, low=0.70, high=0.80,
                 volume=800_000) for i in range(10)]
    assert len(feed(s, bars)) == 1


def test_stop_is_below_entry_and_within_the_cap():
    s = Bounce(CFG, {"AAAA": REF}, history())
    s.on_candidate(FakeCandidate())
    signal = feed(s, reversal_session())[0]
    assert signal.stop_price < signal.entry_price
    distance = (signal.entry_price - signal.stop_price) / signal.entry_price * 100
    assert distance <= CFG["bounce"]["max_stop_pct"]


def test_premarket_bars_are_ignored():
    s = Bounce(CFG, {"AAAA": REF}, history())
    s.on_candidate(FakeCandidate())
    early = Bar("AAAA", datetime(2026, 2, 25, 7, 0), 0.6, 0.6, 0.6, 0.6, 1_000, 0.6)
    assert s.on_bar(early) is None


def test_no_history_means_no_trades():
    """Fails closed: without daily context the setup cannot be identified."""
    s = Bounce(CFG, {"AAAA": REF}, None)
    s.on_candidate(FakeCandidate())
    assert feed(s, reversal_session()) == []
