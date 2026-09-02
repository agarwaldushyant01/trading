"""Backtest the VWAP-reclaim setup over historical minute bars.

    python -m tools.backtest_reclaim --start 2025-09-01 --end 2026-02-28
    python -m tools.backtest_reclaim --sweep

Detects the setup the way the live rule would — a name that spends a
sustained stretch below VWAP, well off its session high, then closes back
above VWAP on expanded volume — and applies the trailing exit.

WHAT THIS CAN AND CANNOT TELL YOU

It can tell you whether the mechanical version of the setup has a hit rate
anywhere near the 50% the manual log showed. If a machine picking these
entries lands at 25%, the edge lives in judgment the rule cannot see, and
that is worth knowing before any money moves.

It cannot tell you the strategy works. Two reasons, both structural:

  SURVIVORSHIP. The universe comes from Alpaca's currently-active ticker
  list, so names delisted since are absent. Sub-$1 small caps delist often,
  and the ones that vanish are disproportionately the ones that failed. Every
  number here is therefore optimistic by an unknown margin.

  SAMPLE. The rule being tested was derived from 32 manual trades over four
  days. A backtest that agrees with it is weak confirmation; the parameters
  were chosen with those trades in mind.

Which is why the sweep splits in-sample from out-of-sample and reports both.
A configuration that only works on the half it was chosen from has told you
nothing.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from statistics import mean, stdev
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from data.reference import load_credentials, load_refs_for
from strategies.reclaim import ReclaimConfig, ReclaimState, exit_for, qualifies

ET = ZoneInfo("America/New_York")
CACHE = pathlib.Path("data/bars/reclaim")


@dataclass
class Trade:
    symbol: str
    day: str
    entry_time: str
    entry: float
    exit_reason: str
    pnl_pct: float
    pct_change_at_entry: float


def session_bars(client, symbol: str, day: date) -> list:
    """Regular-session minute bars for one symbol and day, cached."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{symbol}-{day.isoformat()}.json"
    if path.exists():
        return json.loads(path.read_text())

    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    start = datetime.combine(day, datetime.min.time(), ET).replace(hour=9, minute=30)
    end = start.replace(hour=15, minute=55)
    try:
        raw = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=[symbol], timeframe=TimeFrame.Minute,
            start=start, end=end, feed=DataFeed.SIP)).data.get(symbol, [])
    except Exception:                                     # noqa: BLE001
        raw = []

    rows = [{"t": b.timestamp.astimezone(ET).isoformat(), "o": float(b.open),
             "h": float(b.high), "l": float(b.low), "c": float(b.close),
             "v": float(b.volume)} for b in raw]
    path.write_text(json.dumps(rows))
    return rows


class Bar:
    """Minimal shape the strategy expects."""
    __slots__ = ("timestamp", "high", "low", "close", "volume")

    def __init__(self, row):
        self.timestamp = row["t"]
        self.high, self.low = row["h"], row["l"]
        self.close, self.volume = row["c"], row["v"]


def run_symbol_day(rows: list, prior_close: float,
                   cfg: ReclaimConfig) -> Trade | None:
    """One symbol, one session. At most one trade — the first signal.

    Taking only the first keeps the test honest: a rule that needs three
    attempts on the same name to find a winner is not the rule that was
    described.
    """
    if len(rows) < 40:
        return None

    state = ReclaimState()
    cum_pv = cum_v = 0.0
    session_volume = 0.0

    for i, row in enumerate(rows):
        typical = (row["h"] + row["l"] + row["c"]) / 3
        cum_pv += typical * row["v"]
        cum_v += row["v"]
        session_volume += row["v"]
        if cum_v <= 0:
            continue
        vwap = cum_pv / cum_v

        bar = Bar(row)
        if not state.update(bar, vwap, cfg):
            continue

        pct_change = ((row["c"] / prior_close - 1) * 100) if prior_close else 0.0
        if qualifies(None, row["c"], pct_change, session_volume, cfg):
            return None

        reason, pnl = exit_for(rows, i, row["c"], cfg)
        return Trade(symbol="", day="", entry_time=row["t"][11:16],
                     entry=row["c"], exit_reason=reason, pnl_pct=pnl,
                     pct_change_at_entry=pct_change)

    return None


def backtest(symbols: list, days: list, refs: dict,
             cfg: ReclaimConfig, verbose: bool = True) -> list:
    from alpaca.data.historical import StockHistoricalDataClient

    key, secret = load_credentials()
    client = StockHistoricalDataClient(key, secret)

    trades = []
    for n, day in enumerate(days, 1):
        for symbol in symbols:
            ref = refs.get(symbol)
            if ref is None:
                continue
            rows = session_bars(client, symbol, day)
            if not rows:
                continue
            t = run_symbol_day(rows, ref.prior_close, cfg)
            if t:
                t.symbol, t.day = symbol, day.isoformat()
                trades.append(t)
        if verbose and n % 5 == 0:
            print(f"    {n}/{len(days)} sessions, {len(trades)} trades",
                  flush=True)
    return trades


def report(trades: list, label: str) -> dict:
    if not trades:
        print(f"  {label}: no trades")
        return {}

    rets = [t.pnl_pct for t in trades]
    wins = [r for r in rets if r > 0]
    avg = mean(rets)
    se = stdev(rets) / (len(rets) ** 0.5) if len(rets) > 1 else 0

    print(f"\n  {label}")
    print(f"    {len(rets)} trades, {len(wins)} winners "
          f"({len(wins)/len(rets)*100:.0f}%)")
    print(f"    average {avg:+.2f}% ± {se:.2f}")
    if wins:
        losses = [r for r in rets if r <= 0]
        print(f"    winners {mean(wins):+.1f}%   "
              f"losers {mean(losses):+.1f}%" if losses else "")

    by_exit = defaultdict(int)
    for t in trades:
        by_exit[t.exit_reason] += 1
    print(f"    exits: " + ", ".join(f"{k} {v}" for k, v in
                                     sorted(by_exit.items())))
    return {"n": len(rets), "avg": avg, "se": se,
            "win_rate": len(wins) / len(rets)}


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2026-02-02")
    p.add_argument("--end", default="2026-02-27")
    p.add_argument("--symbols", type=int, default=150,
                   help="how many of the universe to test")
    p.add_argument("--sweep", action="store_true")
    args = p.parse_args()

    refs = load_refs_for(None)
    if not refs:
        raise SystemExit("No reference data. Run data.reference first.")

    # Small caps under $20 with real volume — the population the setup is
    # about. Testing the whole universe would take hours and mostly measure
    # names this strategy would never touch.
    candidates = [s for s, r in refs.items()
                  if 0.25 <= r.prior_close <= 20
                  and r.avg_20d_volume >= 300_000]
    candidates.sort(key=lambda s: -refs[s].avg_20d_volume)
    symbols = candidates[:args.symbols]

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)

    print(f"\n{'=' * 64}")
    print(f"  RECLAIM BACKTEST — {len(symbols)} symbols, {len(days)} sessions")
    print(f"{'=' * 64}")
    print(f"\n  Fetching bars (cached after the first run)...\n")

    if not args.sweep:
        cfg = ReclaimConfig()
        trades = backtest(symbols, days, refs, cfg)
        report(trades, "all sessions")
        show_examples(trades)
        caveats()
        return

    # --- parameter sweep with a holdout ----------------------------------
    half = len(days) // 2
    in_days, out_days = days[:half], days[half:]

    grid = []
    for bars_below in (10, 15, 20):
        for vol_ratio in (1.5, 2.0, 3.0):
            for trail in (8.0, 12.0, 20.0):
                grid.append(ReclaimConfig(min_bars_below_vwap=bars_below,
                                          min_volume_ratio=vol_ratio,
                                          trail_pct=trail))

    print(f"  {len(grid)} configurations, "
          f"{len(in_days)} in-sample / {len(out_days)} out-of-sample\n")

    results = []
    for i, cfg in enumerate(grid, 1):
        ins = backtest(symbols, in_days, refs, cfg, verbose=False)
        if len(ins) < 10:
            continue
        avg = mean([t.pnl_pct for t in ins])
        results.append((avg, cfg, len(ins)))
        print(f"    {i}/{len(grid)}  below={cfg.min_bars_below_vwap} "
              f"vol={cfg.min_volume_ratio} trail={cfg.trail_pct}  "
              f"{len(ins)} trades  {avg:+.2f}%", flush=True)

    if not results:
        print("\n  Not enough trades in any configuration.")
        return

    results.sort(reverse=True, key=lambda r: r[0])
    best_avg, best_cfg, n = results[0]
    print(f"\n  Best in-sample: below={best_cfg.min_bars_below_vwap} "
          f"vol={best_cfg.min_volume_ratio} trail={best_cfg.trail_pct} "
          f"({best_avg:+.2f}% over {n})")

    out = backtest(symbols, out_days, refs, best_cfg, verbose=False)
    stats = report(out, "OUT OF SAMPLE (the number that matters)")

    if stats and stats["avg"] > 0 and stats["avg"] > 2 * stats["se"]:
        print(f"\n  Holds up out of sample.")
    else:
        print(f"\n  Does NOT hold out of sample. The in-sample result was fitting.")
    caveats()


def show_examples(trades: list) -> None:
    if not trades:
        return
    print(f"\n  best and worst:")
    for t in sorted(trades, key=lambda x: -x.pnl_pct)[:3]:
        print(f"    {t.symbol:<6} {t.day} {t.entry_time}  "
              f"{t.pnl_pct:>+7.1f}%  ({t.exit_reason})")
    for t in sorted(trades, key=lambda x: x.pnl_pct)[:3]:
        print(f"    {t.symbol:<6} {t.day} {t.entry_time}  "
              f"{t.pnl_pct:>+7.1f}%  ({t.exit_reason})")


def caveats() -> None:
    print(f"""
  Two things this cannot account for:

    Survivorship — the universe is today's active tickers, so names that
    delisted are missing, and those skew toward failures. Every number above
    is optimistic by an unknown margin.

    Fills — entries are taken at the signal bar's close with no slippage or
    spread. On sub-$1 names that is generous.

  A positive result here is a reason to test forward on paper, not a reason
  to trade money.
""")


if __name__ == "__main__":
    main()
