"""Tests for the VWAP reclaim strategy and the backtest engine."""

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from datetime import datetime, timedelta

from backtest.engine import Engine, Trade, summarize
from risk.sizing import RiskConfig, RiskManager
from scanner.scanner import Bar, TickerRef
from strategies.base import FiveMinuteAggregator, Signal
from strategies.vwap_reclaim import VwapReclaim

REF = TickerRef("AAAA", "NASDAQ", 8_000_000, 1_000_000, 2.00, 3.00, 0.20)

CFG = {
    "engine": {"slippage_pct": 0.5, "hard_exit_time": "15:50",
               "assumed_adv": 1_000_000},
    "vwap_reclaim": {
        "min_hod_pct": 15.0,
        "min_bars_below_vwap": 2,
        "no_entry_after": "15:00",
        "max_stop_pct": 12.0,
        "stop_mode": "bar_low",
        "stop_buffer_pct": 1.0,
        "stop_pct": 8.0,
        "vwap_exit_buffer_pct": 0.5,
        "target_pct": 15.0,
        "exit_on_vwap_loss": True,
    },
}


def bar(price, minute_offset, low=None, high=None, volume=100_000, symbol="AAAA"):
    ts = datetime(2026, 3, 10, 9, 30) + timedelta(minutes=minute_offset)
    return Bar(symbol, ts, price, high or price, low or price, price, volume, price)


class FakeCandidate:
    def __init__(self, symbol="AAAA"):
        self.symbol = symbol


# ------------------------------------------------------------- aggregation

def test_five_minute_bar_closes_on_the_clock():
    agg = FiveMinuteAggregator()
    out = [agg.push(bar(2.0 + i / 100, i)) for i in range(5)]
    assert out[:4] == [None] * 4
    assert out[4] is not None
    assert out[4].timestamp.minute == 34


def test_aggregate_takes_extremes_and_sums_volume():
    agg = FiveMinuteAggregator()
    for i in range(4):
        agg.push(bar(2.0, i, low=1.9, high=2.1, volume=1000))
    five = agg.push(bar(2.5, 4, low=1.5, high=2.6, volume=1000))
    assert five.high == 2.6 and five.low == 1.5
    assert five.volume == 5000 and five.close == 2.5


def test_missing_minutes_still_close_the_bar():
    """Thin names skip minutes; the clock still governs."""
    agg = FiveMinuteAggregator()
    assert agg.push(bar(2.0, 0)) is None
    five = agg.push(bar(2.1, 6))            # jumps into the next 5-min block
    assert five is not None and five.close == 2.0


# ----------------------------------------------------------------- strategy

def feed(strategy, bars):
    signals = [strategy.on_bar(b) for b in bars]
    return [s for s in signals if s]


def run_up_then_below_then_reclaim():
    """Runs to +30%, drops under VWAP for three bars, then closes above."""
    bars = []
    for i in range(10):                       # push HOD to 2.60
        bars.append(bar(2.60, i, high=2.60, volume=200_000))
    for i in range(10, 25):                   # sag below VWAP
        bars.append(bar(2.30, i, low=2.28, volume=50_000))
    for i in range(25, 30):                   # reclaim on volume
        bars.append(bar(2.70, i, low=2.60, high=2.72, volume=400_000))
    return bars


def test_reclaim_produces_a_signal():
    s = VwapReclaim(CFG, {"AAAA": REF})
    s.on_candidate(FakeCandidate())
    signals = feed(s, run_up_then_below_then_reclaim())
    assert len(signals) == 1
    assert signals[0].setup == "vwap_reclaim"
    assert signals[0].stop_price < signals[0].entry_price


def test_only_one_entry_per_symbol_per_session():
    s = VwapReclaim(CFG, {"AAAA": REF})
    s.on_candidate(FakeCandidate())
    bars = run_up_then_below_then_reclaim()
    bars += [bar(2.00, i, low=1.98, volume=50_000) for i in range(30, 45)]
    bars += [bar(3.00, i, low=2.80, volume=600_000) for i in range(45, 50)]
    assert len(feed(s, bars)) == 1


def test_unwatched_symbols_are_ignored():
    """No scanner hit, no trade — the scanner is the universe filter."""
    s = VwapReclaim(CFG, {"AAAA": REF})
    assert feed(s, run_up_then_below_then_reclaim()) == []


def test_no_signal_without_a_qualifying_high():
    """Never ran, so there is nothing to reclaim."""
    s = VwapReclaim(CFG, {"AAAA": REF})
    s.on_candidate(FakeCandidate())
    bars = [bar(2.02, i, volume=100_000) for i in range(10)]
    bars += [bar(1.99, i, low=1.98, volume=50_000) for i in range(10, 25)]
    bars += [bar(2.05, i, low=2.00, volume=400_000) for i in range(25, 30)]
    assert feed(s, bars) == []


def test_no_signal_if_it_never_went_below_vwap():
    s = VwapReclaim(CFG, {"AAAA": REF})
    s.on_candidate(FakeCandidate())
    bars = [bar(2.60 + i / 100, i, high=2.7 + i / 100, volume=200_000)
            for i in range(30)]
    assert feed(s, bars) == []


def test_premarket_bars_do_not_affect_vwap():
    """VWAP is measured from the open; premarket prints must not shift it."""
    s = VwapReclaim(CFG, {"AAAA": REF})
    s.on_candidate(FakeCandidate())
    early = Bar("AAAA", datetime(2026, 3, 10, 7, 0), 9.0, 9.0, 9.0, 9.0, 1_000_000, 9.0)
    s.on_bar(early)
    s.on_bar(bar(2.50, 0, volume=100_000))
    watch = s.watching["AAAA"]
    assert watch.cum_volume == 100_000          # the 07:00 print is excluded
    assert abs(watch.vwap - 2.50) < 0.001


# ------------------------------------------------------------------ engine

def engine():
    risk = RiskManager(RiskConfig(equity=50_000))
    risk.start_session()
    return Engine(risk, CFG["engine"])


def signal(entry=2.70, stop=2.55, target=3.10):
    return Signal("AAAA", datetime(2026, 3, 10, 10, 0), entry, stop, target,
                  "vwap_reclaim")


def test_fill_happens_on_the_next_bar_not_the_signal_price():
    e = engine()
    e.submit(signal())
    e.on_bar(bar(2.75, 31, volume=100_000))
    trade = e.open["AAAA"]
    assert trade.entry_price > 2.75           # next open plus slippage


def test_stop_exit_is_recorded():
    e = engine()
    e.submit(signal())
    e.on_bar(bar(2.75, 31))
    e.on_bar(bar(2.50, 40, low=2.40))
    assert e.closed[0].exit_reason == "stop"
    assert e.closed[0].pnl < 0


def test_target_exit_is_recorded():
    e = engine()
    e.submit(signal())
    e.on_bar(bar(2.75, 31))
    e.on_bar(bar(3.05, 40, high=3.20))
    assert e.closed[0].exit_reason == "target"
    assert e.closed[0].pnl > 0


def test_stop_wins_when_one_bar_spans_both():
    """Pessimistic on purpose — a minute bar cannot say which came first."""
    e = engine()
    e.submit(signal())
    e.on_bar(bar(2.75, 31))
    e.on_bar(bar(2.90, 40, low=2.40, high=3.20))
    assert e.closed[0].exit_reason == "stop"


def test_time_stop_flattens_late_positions():
    e = engine()
    e.submit(signal())
    e.on_bar(bar(2.75, 31))
    late = Bar("AAAA", datetime(2026, 3, 10, 15, 55), 2.8, 2.8, 2.8, 2.8, 1000, 2.8)
    e.on_bar(late)
    assert e.closed[0].exit_reason == "time_stop"


def test_gap_through_the_stop_is_not_entered():
    """If the fill would already be below the stop, take no trade."""
    e = engine()
    e.submit(signal(stop=2.80))
    e.on_bar(bar(2.75, 31))
    assert not e.open and not e.closed


def test_r_multiple_normalizes_by_risk():
    t = Trade("AAAA", "vwap_reclaim", datetime(2026, 3, 10, 10, 0),
              entry_price=2.00, shares=100, stop_price=1.90, target_price=2.30)
    t.exit_price, t.exit_reason = 2.30, "target"
    assert t.to_row()["r_multiple"] == 3.0


def test_summary_of_no_trades_is_empty_not_an_error():
    assert summarize([])["trades"] == 0


# --------------------------------------------------------------- stop modes

def reclaim_signal(stop_mode, **extra):
    cfg = {**CFG, "vwap_reclaim": {**CFG["vwap_reclaim"],
                                   "stop_mode": stop_mode, **extra}}
    s = VwapReclaim(cfg, {"AAAA": REF})
    s.on_candidate(FakeCandidate())
    return feed(s, run_up_then_below_then_reclaim())[0]


def test_bar_low_stop_sits_just_under_the_reclaim_bar():
    assert reclaim_signal("bar_low").stop_price == 2.59


def test_vwap_stop_is_wider_than_the_bar_low_stop():
    """The whole point: a stop at the line the thesis rests on, not at a wick."""
    assert reclaim_signal("vwap").stop_price < reclaim_signal("bar_low").stop_price


def test_pct_stop_is_a_flat_distance_from_entry():
    sig = reclaim_signal("pct", stop_pct=10.0)
    assert abs(sig.stop_price - sig.entry_price * 0.90) < 0.001


def test_unknown_stop_mode_fails_loudly():
    try:
        reclaim_signal("nonsense")
    except ValueError:
        return
    raise AssertionError("should have raised")


def test_vwap_loss_triggers_an_exit():
    s = VwapReclaim(CFG, {"AAAA": REF})
    s.on_candidate(FakeCandidate())
    feed(s, run_up_then_below_then_reclaim())
    vwap = s.watching["AAAA"].vwap
    assert s.should_exit("AAAA", bar(vwap * 0.95, 60)) == "lost_vwap"


def test_holding_above_vwap_does_not_exit():
    s = VwapReclaim(CFG, {"AAAA": REF})
    s.on_candidate(FakeCandidate())
    feed(s, run_up_then_below_then_reclaim())
    vwap = s.watching["AAAA"].vwap
    assert s.should_exit("AAAA", bar(vwap * 1.05, 60)) is None
