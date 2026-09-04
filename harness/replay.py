"""Run a whole session through the real code, against a broker that fails.

    python -m harness.replay --date 2026-09-03
    python -m harness.replay --date 2026-09-03 --symbols SDST,MSTZ,BMNZ
    python -m harness.replay --check          # regression suite

Feeds cached bars through the detector, the sizing, the entry path and the
exit management — the actual PaperTrader, not a copy — with harness.broker
standing in for Alpaca. A full day runs in seconds.

WHY THIS EXISTS

Every module in this project has unit tests and every one of them passed
while four bugs reached production in three days:

    a premarket stop that could not fill and left at 23% below its level
    a loss cap firing at 00:17 against a rolled-over baseline
    a failed close that left a position permanently unmanaged
    a None target crashing the state restore on startup

None of those are component bugs. They live where our code meets the
broker's behaviour, and the only way to catch them is to run the whole thing
against something that behaves like the broker — including the ways it
refuses.

--check runs each of those four as a named regression. If one ever returns,
it fails here in ten seconds rather than at 4am with positions open.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from harness.broker import BrokerError, FakeBroker

ET = ZoneInfo("America/New_York")


class SilentNotifier:
    """Collects notifications instead of sending them."""

    def __init__(self) -> None:
        self.sent: list = []

    def send(self, title: str, body: str, priority: str = "default") -> bool:
        self.sent.append((title, body, priority))
        return True


def load_session(symbol: str, day: str) -> list:
    """Five-minute bars from whatever the tools have already cached."""
    for folder, suffix in (("validate", "-5m"), ("reclaim", ""),
                           ("sweep", "")):
        path = pathlib.Path(f"data/bars/{folder}/{symbol}-{day}{suffix}.json")
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:                             # noqa: BLE001
                continue
    return []


def replay(symbol: str, day: str, cfg: dict, verbose: bool = True) -> dict:
    """One symbol, one session, through the real trading code."""
    from engine.paper import PaperTrader

    bars = load_session(symbol, day)
    if len(bars) < 20:
        return {"symbol": symbol, "status": "no cached bars"}

    broker = FakeBroker(equity=100_000.0)
    notifier = SilentNotifier()
    trader = PaperTrader(broker, cfg, notifier, dry_run=False)
    trader.open_positions = {}
    trader.closing = {}

    from engine.alerts import Alert
    from patterns.detect import detect

    entered = False
    for i, bar in enumerate(bars):
        when = datetime.fromisoformat(bar["t"])
        broker.set_time(when)
        broker.set_price(symbol, bar["c"])

        if not entered and i >= 20:
            found = [s for s in detect(bars[:i + 1], daily=None, levels=[])
                     if not s.rejected]
            if found and found[-1].index >= i - 1:
                setup = found[-1]
                alert = Alert(symbol=symbol, pct_change=0.0, price=setup.entry,
                              volume_1m=bar["v"], volume_2m=0.0,
                              volume_5m=bar["v"], volume_1d=bar["v"] * 50,
                              float_shares=None, alert_count=1, tags=[],
                              received_at=when)
                trader.consider_with_stop(alert, setup.stop, setup.kind, "")
                entered = symbol in trader.open_positions

        if trader.open_positions:
            trader._check_positions()

    account = broker.get_account()
    return {
        "symbol": symbol,
        "status": "ok",
        "entered": entered,
        "still_open": list(broker.positions),
        "realised": broker.realised,
        "equity": account.equity,
        "rejections": broker.rejections,
        "notifications": len(notifier.sent),
    }


# ------------------------------------------------------------ regressions

def check_premarket_stop_fills() -> tuple[bool, str]:
    """A stop in premarket must produce an order that can actually fill."""
    from engine.paper import PaperTrader

    cfg = _cfg()
    broker = FakeBroker()
    trader = PaperTrader(broker, cfg, SilentNotifier(), dry_run=False)
    trader.open_positions = {}
    trader.closing = {}

    broker.set_time(datetime(2026, 9, 4, 7, 0, tzinfo=ET))
    broker.set_price("TEST", 1.00)
    broker.positions["TEST"] = _position("TEST", 1000, 1.00, 1.00)
    trader.open_positions["TEST"] = {
        "symbol": "TEST", "shares": 1000, "signal_price": 1.00,
        "stop": 0.95, "target": None, "setup": "test", "reason": "",
        "opened_at": broker.now.isoformat()}

    broker.set_price("TEST", 0.94)          # through the stop
    trader._check_positions()

    submitted = [o for o in broker.orders if o.side.startswith("sell")]
    if not submitted:
        return (False, "no sell order was submitted at all")
    order = submitted[-1]
    if order.limit_price is None:
        return (False, "submitted a MARKET order in premarket; it cannot fill "
                       "until 09:30 and the price can run away first")
    if not order.extended_hours:
        return (False, "limit order without extended_hours; will not fill "
                       "before 09:30")
    return (True, f"limit {order.limit_price} with extended_hours")


def check_loss_cap_outside_hours() -> tuple[bool, str]:
    """The daily loss cap must not fire when the market is closed."""
    from engine.paper import PaperTrader

    cfg = _cfg()
    broker = FakeBroker(equity=100_000.0)
    broker.starting_equity = 100_000.0
    trader = PaperTrader(broker, cfg, SilentNotifier(), dry_run=False)

    broker.set_time(datetime(2026, 9, 4, 0, 17, tzinfo=ET))
    broker.cash = 96_000.0                  # looks like -4% against baseline

    if trader.check_daily_loss():
        return (False, "fired at 00:17 — last_equity rolls at the session "
                       "boundary and the comparison is meaningless")
    return (True, "silent outside market hours")


def check_failed_close_retries() -> tuple[bool, str]:
    """A close that does not fill must be retried, not skipped forever."""
    from engine.paper import PaperTrader

    cfg = _cfg()
    broker = FakeBroker()
    trader = PaperTrader(broker, cfg, SilentNotifier(), dry_run=False)
    trader.open_positions = {}
    trader.closing = {}

    broker.set_time(datetime(2026, 9, 4, 15, 55, tzinfo=ET))
    broker.set_price("TEST", 1.00)
    pos = _position("TEST", 1000, 1.00, 1.00)
    pos.held_for_orders = 1000              # a prior order holds the shares
    broker.positions["TEST"] = pos
    trader.open_positions["TEST"] = {
        "symbol": "TEST", "shares": 1000, "signal_price": 1.00,
        "stop": 0.95, "target": None, "setup": "test", "reason": "",
        "opened_at": broker.now.isoformat()}

    trader._check_positions()               # first attempt: refused
    before = len(broker.rejections)
    trader._check_positions()               # past 15:50, must try again
    after = len(broker.rejections)

    if after <= before:
        return (False, "did not retry after a failed close; the position is "
                       "unmanaged and will carry overnight")
    return (True, "retried after the failed close")


def check_state_restore_without_target() -> tuple[bool, str]:
    """Restoring a trailing position (target None) must not crash."""
    import tempfile

    from engine import paper as paper_mod
    from engine.paper import PaperTrader

    original = paper_mod.STATE_FILE
    tmp = pathlib.Path(tempfile.mkdtemp()) / "state.json"
    tmp.write_text(json.dumps({"TEST": {
        "symbol": "TEST", "shares": 100, "signal_price": 1.0, "stop": 0.9,
        "target": None, "setup": "pennant", "reason": "",
        "opened_at": "2026-09-04T10:00:00-04:00"}}))
    paper_mod.STATE_FILE = tmp

    broker = FakeBroker()
    broker.positions["TEST"] = _position("TEST", 100, 1.0, 1.0)
    try:
        PaperTrader(broker, _cfg(), SilentNotifier(), dry_run=False)
    except Exception as exc:                              # noqa: BLE001
        return (False, f"crashed restoring a trailing position: {exc}")
    finally:
        paper_mod.STATE_FILE = original
    return (True, "restored a position with no fixed target")


def check_position_limit() -> tuple[bool, str]:
    """Simultaneous entries must not exceed max_concurrent."""
    from engine.alerts import Alert
    from engine.paper import PaperTrader

    cfg = _cfg()
    limit = cfg["risk"]["max_concurrent"]
    broker = FakeBroker()
    trader = PaperTrader(broker, cfg, SilentNotifier(), dry_run=False)
    trader.open_positions = {}
    trader.closing = {}

    broker.set_time(datetime(2026, 9, 4, 10, 0, tzinfo=ET))
    for i in range(limit + 3):
        sym = f"S{i}"
        broker.set_price(sym, 1.00)
        alert = Alert(symbol=sym, pct_change=10.0, price=1.00,
                      volume_1m=1e5, volume_2m=0, volume_5m=1e5,
                      volume_1d=5e6, float_shares=None, alert_count=1,
                      tags=[], received_at=broker.now)
        trader.consider_with_stop(alert, 0.90, "test", "")

    held = len(broker.positions)
    if held > limit:
        return (False, f"{held} positions opened against a limit of {limit}")
    return (True, f"{held} positions, limit {limit}")


def _cfg() -> dict:
    path = pathlib.Path("config/rules.yaml")
    if path.exists():
        return yaml.safe_load(path.read_text())
    return {"universe": {"min_price": 0.1, "max_price": 20.0,
                         "max_float": 20_000_000, "min_daily_volume": 50_000},
            "risk": {"risk_per_trade_pct": 0.5, "max_position_pct": 10.0,
                     "max_concurrent": 3, "fallback_equity": 100_000,
                     "max_daily_loss_pct": 2.0},
            "execution": {"entry_slippage_pct": 2.0, "exit_slippage_pct": 3.0,
                          "hard_exit_time": "15:50", "trail_pct": 12.0,
                          "trail_arms_at_pct": 10.0, "keep_gain_fraction": 0.5,
                          "min_stop_pct": 5.0, "adopted_stop_pct": 12.0,
                          "adopted_target_pct": None}}


def _position(symbol, qty, entry, price):
    from harness.broker import FakePosition
    return FakePosition(symbol=symbol, qty=qty, avg_entry_price=entry,
                        current_price=price)


CHECKS = [
    ("premarket stops can fill", check_premarket_stop_fills),
    ("loss cap silent outside hours", check_loss_cap_outside_hours),
    ("failed closes are retried", check_failed_close_retries),
    ("state restore without a target", check_state_restore_without_target),
    ("position limit holds", check_position_limit),
]


def run_checks() -> int:
    print(f"\n{'=' * 68}")
    print(f"  REGRESSION CHECKS")
    print(f"{'=' * 68}\n")
    print(f"  Each of these reached production once.\n")

    failed = 0
    for name, fn in CHECKS:
        try:
            ok, detail = fn()
        except Exception as exc:                          # noqa: BLE001
            ok, detail = False, f"raised {type(exc).__name__}: {exc}"
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}]  {name}")
        print(f"          {detail}")
        if not ok:
            failed += 1

    print(f"\n  {len(CHECKS) - failed}/{len(CHECKS)} passing\n")
    return failed


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--date", default=None)
    p.add_argument("--symbols", default=None)
    p.add_argument("--check", action="store_true")
    args = p.parse_args()

    if args.check or not args.date:
        raise SystemExit(1 if run_checks() else 0)

    cfg = _cfg()
    symbols = (args.symbols.split(",") if args.symbols
               else _symbols_with_bars(args.date))
    if not symbols:
        print(f"\n  No cached bars for {args.date}.\n")
        return

    print(f"\n{'=' * 68}")
    print(f"  REPLAY — {args.date}, {len(symbols)} symbols")
    print(f"{'=' * 68}\n")

    total_rejections = 0
    for symbol in symbols:
        r = replay(symbol, args.date, cfg)
        if r["status"] != "ok":
            continue
        total_rejections += len(r["rejections"])
        flag = "  <-- STILL OPEN" if r["still_open"] else ""
        print(f"  {symbol:<7} entered={str(r['entered']):<5} "
              f"realised {r['realised']:>+9.2f}{flag}")
        for when, sym, why in r["rejections"][:3]:
            print(f"            {when:%H:%M} refused: {why}")

    print(f"\n  {total_rejections} broker refusals across the replay")
    print(f"  Any 'market order outside session hours' is a stop that would")
    print(f"  not have filled where it was set.\n")


def _symbols_with_bars(day: str) -> list:
    found = set()
    for folder in ("validate", "reclaim", "sweep"):
        base = pathlib.Path(f"data/bars/{folder}")
        if base.exists():
            for path in base.glob(f"*{day}*.json"):
                found.add(path.stem.split("-")[0])
    return sorted(found)


if __name__ == "__main__":
    main()
