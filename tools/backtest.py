"""Run a strategy over history and report what it would have made.

    python -m tools.backtest --start 2025-09-01 --end 2026-02-28

Caches minute bars to data/bars/ on the first run. Re-runs after a parameter
change then take seconds instead of an hour, which matters because you will
change parameters many times.

The pipeline: scanner picks the universe -> strategy decides entries ->
risk sizes them -> engine fills, stops and exits. Every component is the one
the live bot will use.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date, timedelta

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backtest.engine import Engine, summarize
from data.reference import load_credentials, load_refs_for
from drivers.replay import fetch_daily, fetch_minutes, prescreen, to_bars
from risk.sizing import RiskConfig, RiskManager
from scanner.scanner import Bar, Scanner
from strategies.bounce import Bounce
from strategies.vwap_reclaim import VwapReclaim

BAR_CACHE = pathlib.Path("data/bars")
STRATEGIES = {"vwap_reclaim": VwapReclaim, "bounce": Bounce}
NEEDS_HISTORY = {"bounce"}


def cached_minutes(client, day: date, cfg, refs, feed) -> dict[str, list[Bar]]:
    import pandas as pd

    path = BAR_CACHE / f"{day.isoformat()}.parquet"
    if path.exists():
        frame = pd.read_parquet(path)
        out: dict[str, list[Bar]] = {}
        for row in frame.itertuples(index=False):
            out.setdefault(row.symbol, []).append(
                Bar(row.symbol, row.timestamp, row.open, row.high, row.low,
                    row.close, int(row.volume), row.vwap)
            )
        return out

    daily = fetch_daily(client, sorted(refs), day, feed)
    if not daily:
        return {}
    survivors = [s for s, b in daily.items() if prescreen(b, refs[s], cfg)]
    if not survivors:
        return {}

    minutes = fetch_minutes(client, survivors, day, feed)
    BAR_CACHE.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([
        {"symbol": b.symbol, "timestamp": b.timestamp, "open": b.open,
         "high": b.high, "low": b.low, "close": b.close, "volume": b.volume,
         "vwap": b.vwap}
        for bars in minutes.values() for b in bars
    ]).to_parquet(path, index=False)
    return minutes


def run_day(minutes, scanner, strategy, engine) -> int:
    """One session. Bars interleaved by time, exactly as they arrive live."""
    stream = sorted(
        (bar for bars in minutes.values() for bar in bars),
        key=lambda b: b.timestamp,
    )

    before = len(engine.closed)
    last_bars: dict[str, Bar] = {}

    for bar in stream:
        last_bars[bar.symbol] = bar

        candidate = scanner.on_bar(bar)
        if candidate is not None:
            strategy.on_candidate(candidate)

        signal = strategy.on_bar(bar)
        if signal is not None:
            engine.submit(signal)

        # Thesis invalidation, checked before the mechanical stop so a failed
        # reclaim exits on its own terms rather than waiting to be stopped.
        if bar.symbol in engine.open:
            reason = strategy.should_exit(bar.symbol, bar)
            if reason:
                engine.exit_now(bar.symbol, bar, reason)

        engine.on_bar(bar)

    engine.end_session(last_bars)
    return len(engine.closed) - before


def report(trades, risk: RiskManager) -> None:
    stats = summarize(trades)
    if not stats["trades"]:
        print("\nNo trades. Either the setup never appeared or a filter is "
              "too tight — check config/strategies.yaml.\n")
        return

    equity = risk.cfg.equity
    name = trades[0].setup.replace("_", " ").upper()
    print(f"\n{'=' * 60}\n{name} — {stats['trades']} trades\n{'=' * 60}")
    print(f"  hit rate        {stats['hit_rate']:>10.1f}%")
    print(f"  total P&L       {stats['total_pnl']:>10,.0f}   "
          f"({stats['total_pnl'] / equity * 100:+.1f}% on ${equity:,.0f})")
    print(f"  average win     {stats['avg_win']:>10,.0f}")
    print(f"  average loss    {stats['avg_loss']:>10,.0f}")
    print(f"  per trade       {stats['expectancy']:>10,.0f}")
    r, se = stats["expectancy_r"], stats.get("stderr_r")
    band = f" +/- {se:.3f}" if se else ""
    print(f"  per trade in R  {r:>10}{band}   <- one unit of risk returns this")
    if se and abs(r) < 2 * se:
        print(f"  {'':>18}not distinguishable from zero at this sample size")
    print(f"  best / worst    {stats['best']:>10,.0f} / {stats['worst']:,.0f}")

    reasons: dict[str, list] = {}
    for t in trades:
        reasons.setdefault(t.exit_reason, []).append(t)

    print(f"\n  exits:")
    for reason, group in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
        total = sum(t.pnl for t in group)
        print(f"    {reason:<14} {len(group):>4}   {total:>10,.0f}")

    print(f"\n  A positive R above ~0.2 is worth pursuing. Negative means the")
    print(f"  setup as specified does not work — change the rules, not the")
    print(f"  date range.\n")


def main() -> None:
    import argparse

    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient

    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--strategy", default="vwap_reclaim", choices=list(STRATEGIES))
    p.add_argument("--feed", default="sip", choices=["sip", "iex"])
    p.add_argument("--equity", type=float, default=50_000)
    p.add_argument("--trades-out", default="data/trades.parquet")
    args = p.parse_args()

    scanner_cfg = yaml.safe_load(pathlib.Path("config/scanner.yaml").read_text())
    strategy_cfg = yaml.safe_load(pathlib.Path("config/strategies.yaml").read_text())
    refs = load_refs_for(None)

    key, secret = load_credentials()
    client = StockHistoricalDataClient(key, secret)
    feed = DataFeed.SIP if args.feed == "sip" else DataFeed.IEX

    scanner = Scanner(scanner_cfg, refs)
    if args.strategy in NEEDS_HISTORY:
        from data.daily_history import History
        strategy = STRATEGIES[args.strategy](strategy_cfg, refs, History.load())
    else:
        strategy = STRATEGIES[args.strategy](strategy_cfg, refs)
    risk = RiskManager(RiskConfig(equity=args.equity))
    engine = Engine(risk, strategy_cfg["engine"])

    day = date.fromisoformat(args.start)
    last = date.fromisoformat(args.end)

    while day <= last:
        if day.weekday() < 5:
            risk.start_session()
            minutes = cached_minutes(client, day, scanner_cfg, refs, feed)
            if minutes:
                n = run_day(minutes, scanner, strategy, engine)
                if n:
                    print(f"  {day}  {n} trade(s)", file=sys.stderr)
        day += timedelta(days=1)

    report(engine.closed, risk)

    if engine.closed:
        import pandas as pd

        out = pathlib.Path(args.trades_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([t.to_row() for t in engine.closed]).to_parquet(out, index=False)
        print(f"Trade log: {out}\n")


if __name__ == "__main__":
    main()
