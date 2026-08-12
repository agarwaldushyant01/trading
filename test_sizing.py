"""Tests for risk/sizing.py — run with: python -m pytest tests/ -v"""

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from risk.sizing import Reject, RiskConfig, RiskManager


def rm(**overrides) -> RiskManager:
    return RiskManager(RiskConfig(**overrides))


# ------------------------------------------------------------------ sizing

def test_normal_trade_risks_250_dollars():
    s = rm().size(entry_price=2.00, stop_price=1.76, atr=0.20,
                  avg_20d_volume=5_000_000)
    assert s.allowed
    assert s.shares == 1041                 # floor(250 / 0.24)
    assert abs(s.risk_dollars - 249.84) < 0.01
    assert s.binding_cap == "risk"


def test_tight_stop_is_widened_to_half_atr():
    """A 2% stop on a stock with a 20c ATR gets pushed out to 10c."""
    s = rm().size(entry_price=2.00, stop_price=1.96, atr=0.20,
                  avg_20d_volume=5_000_000)
    assert s.stop_price == 1.90             # 2.00 - (0.5 * 0.20)
    assert s.shares == 2500                 # floor(250 / 0.10)


def test_concentration_cap_binds_on_tight_stops():
    """Tight stop wants a huge position; 10% of equity caps it."""
    s = rm().size(entry_price=2.00, stop_price=1.98, atr=0.02,
                  avg_20d_volume=50_000_000)
    assert s.shares == 2500                 # 10% of 50k / $2
    assert s.binding_cap == "concentration"
    assert s.notional == 5000.0


def test_liquidity_cap_binds_on_thin_names():
    s = rm().size(entry_price=2.00, stop_price=1.76, atr=0.20,
                  avg_20d_volume=50_000)
    assert s.shares == 500                  # 1% of ADV
    assert s.binding_cap == "liquidity"


def test_overnight_multiplier_halves_the_size():
    full = rm().size(2.00, 1.76, 0.20, 5_000_000, risk_multiplier=1.0)
    half = rm().size(2.00, 1.76, 0.20, 5_000_000, risk_multiplier=0.5)
    assert half.shares == full.shares // 2


# -------------------------------------------------------------- rejections

def test_stop_wider_than_20pct_is_rejected():
    s = rm().size(entry_price=2.00, stop_price=1.50, atr=0.10,
                  avg_20d_volume=5_000_000)
    assert not s.allowed and s.reject is Reject.STOP_TOO_WIDE


def test_max_concurrent_positions():
    m = rm()
    for _ in range(3):
        m.record_fill()
    assert m.size(2.00, 1.76, 0.20, 5_000_000).reject is Reject.MAX_POSITIONS


def test_daily_loss_limit_halts_trading():
    m = rm()
    m.record_fill(); m.record_close(realized_pnl=-1000.0)   # full 2% cap
    assert m.daily_budget_left == 0
    assert m.should_flatten
    assert m.size(2.00, 1.76, 0.20, 5_000_000).reject is Reject.DAILY_LOSS_LIMIT


def test_four_full_losses_exhaust_the_day():
    """0.5% risk against a 2% cap means the 5th trade is refused."""
    m = rm()
    for _ in range(4):
        m.record_fill(); m.record_close(realized_pnl=-250.0)
    assert m.size(2.00, 1.76, 0.20, 5_000_000).reject is Reject.DAILY_LOSS_LIMIT


def test_partial_budget_rejects_rather_than_downsizes():
    m = rm()
    m.record_fill(); m.record_close(realized_pnl=-900.0)     # $100 left
    s = m.size(2.00, 1.76, 0.20, 5_000_000)
    assert s.reject is Reject.INSUFFICIENT_BUDGET


def test_wins_do_not_replenish_the_budget():
    """Profit does not buy extra risk; the cap is a floor on the day."""
    m = rm()
    m.record_fill(); m.record_close(realized_pnl=-500.0)
    m.record_fill(); m.record_close(realized_pnl=+2000.0)
    assert m.daily_budget_left == 1000.0                     # not more


def test_three_losing_days_halts_the_bot():
    m = rm()
    for _ in range(3):
        m.start_session()
        m.record_fill(); m.record_close(realized_pnl=-50.0)
        m.end_session()
    m.start_session()
    assert m.halted
    assert m.size(2.00, 1.76, 0.20, 5_000_000).reject is Reject.CONSECUTIVE_LOSING_DAYS


def test_winning_day_resets_the_losing_streak():
    m = rm()
    for _ in range(2):
        m.start_session(); m.record_fill(); m.record_close(-50.0); m.end_session()
    m.start_session(); m.record_fill(); m.record_close(+50.0); m.end_session()
    assert m.consecutive_losing_days == 0


def test_stop_above_entry_is_rejected():
    s = rm().size(2.00, 2.10, 0.20, 5_000_000)
    assert s.reject is Reject.STOP_ABOVE_ENTRY
