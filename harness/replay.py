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

    # Drive the trader's clock, not the machine's. Without this the check
    # only exercises the premarket path when it happens to run outside
    # 09:30-16:00; run during the session it silently tests the RTH path —
    # a market order — which then trips this check's own assertion. The
    # sibling checks below already inject the clock for the same reason.
    trader.clock = lambda: datetime(2026, 9, 4, 7, 0, tzinfo=ET)

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

    # Drive the trader's clock, not the machine's. Without this the test
    # runs at whatever time it happens to be and cannot reach the guard.
    trader.clock = lambda: datetime(2026, 9, 4, 0, 17, tzinfo=ET)
    broker.set_time(datetime(2026, 9, 4, 0, 17, tzinfo=ET))
    broker.cash = 96_000.0                  # looks like -4% against baseline

    if trader.check_daily_loss():
        return (False, "fired at 00:17 — last_equity rolls at the session "
                       "boundary and the comparison is meaningless")
    # And it must still fire during the session, or the guard is useless.
    trader.clock = lambda: datetime(2026, 9, 4, 11, 0, tzinfo=ET)
    if not trader.check_daily_loss():
        return (False, "did not fire at 11:00 on a -4% day; the cap is now "
                       "inert during the session")
    return (True, "silent at 00:17, fires at 11:00")


def check_failed_close_retries() -> tuple[bool, str]:
    """A close that does not fill must be retried, not skipped forever."""
    from engine.paper import PaperTrader

    cfg = _cfg()
    broker = FakeBroker()
    trader = PaperTrader(broker, cfg, SilentNotifier(), dry_run=False)
    trader.open_positions = {}
    trader.closing = {}

    # Drive the trader's clock too, not just the broker's. This test passed
    # on a Friday evening only because the real time happened to be past
    # 15:50; at 09:17 on Monday the same code correctly waits out the retry
    # window and the test failed. A test that depends on when it runs is not
    # a test.
    trader.clock = lambda: datetime(2026, 9, 4, 15, 55, tzinfo=ET)
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


def _grade_rank(g: str) -> int:
    return {"none": 0, "A": 1, "A++": 2}.get(g, 0)


def _triangle_bars() -> list:
    """A synthetic ascending triangle: flat resistance ~10.00, rising lows,
    volume expansion on the break. The news regression needs a real detector
    hit whose grade news can lift, without depending on which cached sessions
    are on disk."""
    def bar(i, o, h, l, c, v):
        m = 9 * 60 + 30 + i * 5
        return {"t": f"2026-08-25T{m // 60:02d}:{m % 60:02d}:00-04:00",
                "o": o, "h": h, "l": l, "c": c, "v": float(v)}

    warm = [(8.5, 8.6, 8.4, 8.55, 500), (8.55, 8.7, 8.5, 8.6, 520),
            (8.6, 8.75, 8.55, 8.7, 540)] * 4
    body = [
        (8.80, 8.95, 8.60, 8.90, 1000), (8.90, 9.10, 8.80, 9.05, 1100),
        (9.05, 9.20, 8.95, 9.00, 900), (9.00, 10.00, 8.90, 9.85, 1200),
        (9.85, 9.98, 9.40, 9.55, 1000), (9.55, 9.80, 9.45, 9.70, 800),
        (9.70, 10.02, 9.60, 9.80, 1300), (9.80, 9.95, 9.62, 9.68, 700),
        (9.68, 9.90, 9.64, 9.78, 750), (9.78, 10.01, 9.70, 9.82, 1250),
        (9.82, 9.95, 9.74, 9.80, 650), (9.80, 9.92, 9.76, 9.83, 600),
        (9.83, 9.90, 9.78, 9.85, 620), (9.85, 9.93, 9.71, 9.80, 640),
        (9.80, 9.90, 9.77, 9.84, 610),
        (9.84, 10.35, 9.82, 10.25, 3000),      # break, volume expansion
        (10.25, 10.40, 10.10, 10.30, 1500),
    ]
    return [bar(i, *row) for i, row in enumerate(warm + body)]


def _flat_bars() -> list:
    """A quiet range: nothing for the detector to find, at any grade."""
    def bar(i):
        m = 9 * 60 + 30 + i * 5
        return {"t": f"2026-08-25T{m // 60:02d}:{m % 60:02d}:00-04:00",
                "o": 5.00, "h": 5.02, "l": 4.98, "c": 5.00, "v": 500.0}
    return [bar(i) for i in range(40)]


def check_news_confluence() -> tuple[bool, str]:
    """Fresh news is a fifth confluence: it lifts the grade of a setup that
    already qualifies, and never creates one on its own.

    News is the trader's highest-expectancy setup and the detector had no
    representation for it. The risk in adding it is that it quietly becomes a
    trigger — a headline plus a shape that would not otherwise qualify. This
    pins the intended behaviour: the set of setups that fire is identical
    with and without news; only their grade moves.
    """
    from patterns.confluence import Confluences, evaluate, find_demand_zones
    from patterns.detect import detect

    solo = Confluences(news=True)
    if solo.count != 0 or solo.score != 1 or solo.grade != "none":
        return (False, "news alone reads as a gradeable setup")
    a = Confluences(demand_zone=True, bottom_wick=True)
    an = Confluences(demand_zone=True, bottom_wick=True, news=True)
    if a.grade != "A" or an.grade != "A++" or an.count != a.count:
        return (False, f"news did not lift A->A++ cleanly "
                       f"({a.grade}->{an.grade}, count {a.count}->{an.count})")

    bars = _triangle_bars()
    plain = [s for s in detect(bars, daily=None, levels=[]) if not s.rejected]
    if not plain:
        return (False, "fixture stopped producing a setup; rewrite "
                       "_triangle_bars")

    zones = find_demand_zones(bars)
    if evaluate(bars, plain[-1].index, zones, news=True).news is not True:
        return (False, "evaluate() ignored news=True")

    withnews = [s for s in detect(bars, daily=None, levels=[],
                                  news_at=lambda bar: True) if not s.rejected]
    notfresh = [s for s in detect(bars, daily=None, levels=[],
                                  news_at=lambda bar: False) if not s.rejected]
    if len(withnews) != len(plain) or len(notfresh) != len(plain):
        return (False, "news changed which setups fired, not just the grade")
    if [s.grade for s in notfresh] != [s.grade for s in plain]:
        return (False, "news_at returning False still changed the grade")

    lifted = False
    for base, boosted in zip(plain, withnews):
        if boosted.confluences.count != base.confluences.count:
            return (False, "news changed the chart-confluence count")
        if boosted.confluences.score != base.confluences.score + 1:
            return (False, "news did not add to the confluence score")
        if not boosted.confluences.news:
            return (False, "news flag not set on the boosted setup")
        if _grade_rank(boosted.grade) < _grade_rank(base.grade):
            return (False, "news lowered a grade")
        if base.confluences.count == 2:
            if base.grade != "A" or boosted.grade != "A++":
                return (False, "an A setup with news did not become A++")
            lifted = True
    if not lifted:
        return (False, "fixture has no A setup to lift; rewrite _triangle_bars")

    if [s for s in detect(_flat_bars(), daily=None, levels=[],
                          news_at=lambda bar: True) if not s.rejected]:
        return (False, "news manufactured a setup with no pattern")

    return (True, f"A->A++ with news across {len(plain)} setup(s); "
                  f"none created from news alone")


def _context_bars() -> list:
    """A synthetic session: a very heavy opening five-minute bar, then a
    base and two pullbacks that hold with confluences. Used by the
    context-setup regression — it needs real detector hits (runner /
    volume_open) whose behaviour can be pinned without leaning on which
    cached sessions happen to be on disk."""
    def bar(i, o, h, l, c, v):
        m = 9 * 60 + 30 + i * 5
        return {"t": f"2026-08-25T{m // 60:02d}:{m % 60:02d}:00-04:00",
                "o": o, "h": h, "l": l, "c": c, "v": float(v)}

    rows = [
        (5.00, 5.80, 4.90, 5.60, 90000),   # heavy opening bar
        (5.60, 5.75, 5.40, 5.50, 20000), (5.50, 5.70, 5.35, 5.65, 18000),
        (5.65, 5.95, 5.55, 5.60, 22000), (5.60, 5.62, 5.20, 5.30, 15000),
        (5.30, 5.45, 5.15, 5.42, 9000), (5.42, 5.50, 5.20, 5.25, 8000),
        (5.25, 5.40, 5.05, 5.35, 8000), (5.35, 5.45, 5.28, 5.40, 7000),
        (5.40, 5.55, 5.10, 5.52, 12000), (5.52, 5.70, 5.45, 5.66, 14000),
        (5.66, 5.72, 5.30, 5.40, 11000), (5.40, 5.58, 5.18, 5.55, 10000),
        (5.55, 5.62, 5.48, 5.58, 9000),
    ]
    rows += [(5.58, 5.65, 5.50, 5.60, 9000)] * 20
    return [bar(i, *r) for i, r in enumerate(rows)]


def check_context_setups() -> tuple[bool, str]:
    """Previous-day runner and volume-at-open fire on daily / volume context
    plus confluences — and only then.

    These are new setup paths with no chart geometry behind them. The risk is
    that they fire on any base on any name. This pins: volume_open needs a
    genuinely heavy opening bar, runner needs the prior-day move over the
    threshold, both still need min_confluences, both respect the "already
    extended today" cap, and disabling either really disables it. Also
    guards that the existing shapes are untouched.
    """
    from patterns import detect as d
    from patterns.detect import detect
    from patterns.character import market_is_thin

    bars = _context_bars()

    vo = [s for s in detect(bars, daily=None, levels=[])
          if not s.rejected and s.kind == "volume_open"]
    if not vo or not vo[-1].volume_open:
        return (False, "volume-at-open did not fire on a heavy opening bar")

    run = [s for s in detect(bars, daily=None, levels=[], prev_day_change=30.0)
           if not s.rejected and s.kind == "runner"]
    if not run or not run[-1].runner:
        return (False, "runner did not fire at prev_day_change=30%")

    below = {s.kind for s in detect(bars, daily=None, levels=[],
                                    prev_day_change=10.0) if not s.rejected}
    if "runner" in below:
        return (False, "runner fired at prev_day_change=10%, under the 25% bar")

    old = (d.VOLUME_OPEN_RANK, d.RUNNER_MIN_PREV_CHANGE_PCT,
           d.CONTEXT_MAX_TODAY_CHANGE_PCT)
    try:
        d.VOLUME_OPEN_RANK, d.RUNNER_MIN_PREV_CHANGE_PCT = 0, float("inf")
        off = {s.kind for s in detect(bars, daily=None, levels=[],
                                      prev_day_change=30.0) if not s.rejected}
        if {"runner", "volume_open"} & off:
            return (False, "context setups fired while disabled")

        d.VOLUME_OPEN_RANK, d.RUNNER_MIN_PREV_CHANGE_PCT = 3, 25.0
        d.CONTEXT_MAX_TODAY_CHANGE_PCT = 1.0            # everything is "extended"
        capped = {s.kind for s in detect(bars, daily=None, levels=[],
                                         prev_day_change=30.0)
                  if not s.rejected}
        if {"runner", "volume_open"} & capped:
            return (False, "context setup fired on an already-extended name")
    finally:
        (d.VOLUME_OPEN_RANK, d.RUNNER_MIN_PREV_CHANGE_PCT,
         d.CONTEXT_MAX_TODAY_CHANGE_PCT) = old

    if not market_is_thin(15) or market_is_thin(25):
        return (False, "dumpster thin threshold is not ~20")

    return (True, "runner + volume_open fire on context; off when disabled; "
                  "capped when extended; thin < 20")


def check_grade_sizing() -> tuple[bool, str]:
    """Position size scales with setup grade, and the max-position cap still
    binds regardless of the multiplier."""
    from engine.alerts import Alert
    from engine.paper import PaperTrader

    cfg = _cfg()
    cfg.setdefault("risk", {})["grade_size_multiplier"] = {
        "A++": 1.5, "A": 1.0, "fallback": 0.5}

    def shares_for(grade: str, stop: float = 0.90) -> int:
        broker = FakeBroker()
        t = PaperTrader(broker, cfg, SilentNotifier(), dry_run=False)
        t.open_positions = {}
        t.closing = {}
        t.clock = lambda: datetime(2026, 9, 4, 10, 0, tzinfo=ET)
        broker.set_time(datetime(2026, 9, 4, 10, 0, tzinfo=ET))
        broker.set_price("G", 1.00)
        a = Alert(symbol="G", pct_change=10.0, price=1.00, volume_1m=1e5,
                  volume_2m=0, volume_5m=1e5, volume_1d=5e6,
                  float_shares=None, alert_count=1, tags=[],
                  received_at=broker.now)
        t.consider_with_stop(a, stop, "test", "", grade=grade)
        pos = broker.positions.get("G")
        return int(float(pos.qty)) if pos else 0

    a_plus, a, fb = shares_for("A++"), shares_for("A"), shares_for("fallback")
    missing = shares_for("")
    if not (a_plus > a > fb > 0):
        return (False, f"grade did not scale size: A++={a_plus} A={a} "
                       f"fallback={fb}")
    if missing != a:
        return (False, f"missing grade should size as 1.0, got {missing} "
                       f"vs A={a}")

    # The max-position cap is not scaled by the multiplier: at a stop tight
    # enough to exceed it, every grade clamps to the same number.
    t = PaperTrader(FakeBroker(), cfg, SilentNotifier(), dry_run=False)
    if t._size(1.00, 0.5, 1.5) != t._size(1.00, 0.5, 1.0):
        return (False, "max-position cap not binding across grades")

    return (True, f"A++ {a_plus} > A {a} > fallback {fb}; cap binds")


CHECKS = [
    ("premarket stops can fill", check_premarket_stop_fills),
    ("loss cap silent outside hours", check_loss_cap_outside_hours),
    ("failed closes are retried", check_failed_close_retries),
    ("state restore without a target", check_state_restore_without_target),
    ("position limit holds", check_position_limit),
    ("news raises grade, never triggers alone", check_news_confluence),
    ("context setups fire on context, not alone", check_context_setups),
    ("grade scales position size", check_grade_sizing),
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
    import tempfile

    from engine import paper as paper_mod

    # The harness runs the real PaperTrader, which reads and writes two
    # module-level paths: TRADE_LOG (the journal) and STATE_FILE (open
    # positions). In production those are data/mosquito/trades.jsonl and
    # data/mosquito/open_positions.json — files a running scanner also
    # uses. A --check run has been appending fixture rows to the journal
    # and, through the position-limit check, overwriting the live
    # open-positions file with S0/S1/S2. Point both at throwaways for the
    # life of the process; the state-restore check still does its own
    # STATE_FILE swap on top of this.
    _tmp = pathlib.Path(tempfile.mkdtemp())
    paper_mod.TRADE_LOG = _tmp / "trades.jsonl"
    paper_mod.STATE_FILE = _tmp / "open_positions.json"

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
