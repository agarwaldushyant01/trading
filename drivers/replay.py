"""Replay driver — runs the scanner over historical data.

    python -m drivers.replay --start 2026-01-02 --end 2026-03-31

Writes one Parquet file per day to data/candidates/, each row a scanner hit
with its full feature snapshot plus what happened next.

Design notes:

  Same scanner code as live. This module only produces Bar events; it has no
  strategy logic and makes no trading decisions. If backtest and live ever
  disagree, it is not because they run different scanning code.

  Pre-screening. Pulling minute bars for 4,000 symbols x 250 days is millions
  of requests worth of data. A symbol whose entire daily range is 3% cannot
  possibly have fired a 30% velocity trigger, so daily bars are fetched first
  (cheap, one request per chunk) and minute bars only for survivors. Typically
  cuts 4,000 symbols to under 200 per day.

  The pre-screen is deliberately a superset of the scan conditions. It may let
  through symbols that never trigger; it must never exclude one that would.

  Forward returns are computed here because the minute bars are already in
  memory. MAE (maximum adverse excursion) matters more than the returns: it
  tells you empirically how far trades went against you before working, which
  is what should set stop distance rather than a guess.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from data.reference import load_refs_for
from scanner.scanner import Bar, Scanner

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

SESSION_START = time(4, 0)
SESSION_END = time(20, 0)

FORWARD_HORIZONS = (5, 15, 30, 60)      # minutes
EXCURSION_WINDOW = 60                   # minutes for MAE / MFE

OUT_DIR = pathlib.Path("data/candidates")


# --------------------------------------------------------------- pure core

def prescreen(daily: dict, ref, cfg: dict) -> bool:
    """Could this symbol possibly have triggered on this day?

    Uses only the daily bar. Must be a superset of the scan conditions —
    a false positive costs one minute-bar fetch, a false negative silently
    removes a trade from the backtest.
    """
    u = cfg["universe"]
    if daily["high"] < u["min_price"] or daily["low"] > u["max_price"]:
        return False
    if ref.prior_close <= 0 or daily["volume"] <= 0:
        return False

    # Share count and liquidity are static per symbol, so filtering here
    # rather than in the scanner avoids fetching minute bars for names that
    # can never pass. In the first run this was ~90% of pre-screened symbols:
    # 379 survived the price/range screen but only 30 distinct names ever
    # produced a candidate.
    if ref.shares_outstanding > u["max_shares_outstanding"]:
        return False
    if ref.avg_20d_volume < u["min_avg_20d_volume"]:
        return False

    # Gap mode: the day's high must clear the threshold above prior close.
    gap_possible = (
        daily["high"] / ref.prior_close - 1
    ) * 100 >= cfg["gap"]["min_pct_change"]

    # Velocity mode: a 30% move inside the day requires at least that much
    # range across the whole day.
    velocity_possible = (
        daily["low"] > 0
        and (daily["high"] / daily["low"] - 1) * 100 >= cfg["velocity"]["min_pct_change"]
    )

    if not (gap_possible or velocity_possible):
        return False

    # Volume floor: the looser of the two modes' requirements.
    min_volume = min(
        cfg["gap"]["min_session_volume"], cfg["velocity"]["min_cumulative_volume"]
    )
    return daily["volume"] >= min_volume


def forward_metrics(entry_price: float, forward: list[Bar]) -> dict:
    """Returns and excursions after a candidate fired.

    `forward` is the bars strictly after the trigger bar, in order. Missing
    horizons yield None rather than zero — an absent value and a flat one
    are different facts, and conflating them biases the analysis.
    """
    out: dict[str, float | None] = {}

    for horizon in FORWARD_HORIZONS:
        if len(forward) >= horizon:
            out[f"fwd_{horizon}m_pct"] = round(
                (forward[horizon - 1].close / entry_price - 1) * 100, 3
            )
        else:
            out[f"fwd_{horizon}m_pct"] = None

    window = forward[:EXCURSION_WINDOW]
    if window:
        out["mae_pct"] = round((min(b.low for b in window) / entry_price - 1) * 100, 3)
        out["mfe_pct"] = round((max(b.high for b in window) / entry_price - 1) * 100, 3)
        out["bars_available"] = len(window)
    else:
        out["mae_pct"] = out["mfe_pct"] = None
        out["bars_available"] = 0

    return out


def to_bars(raw, symbol: str) -> list[Bar]:
    """Alpaca bars -> scanner Bars, converted to Eastern time.

    Alpaca timestamps are UTC. The scanner's session logic compares clock
    times against 04:00 / 09:30 / 16:00, so this conversion is load-bearing:
    skip it and every premarket bar is misclassified.
    """
    bars = []
    for b in raw:
        ts = b.timestamp.astimezone(ET)
        if not (SESSION_START <= ts.time() < SESSION_END):
            continue
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=ts,
                open=float(b.open),
                high=float(b.high),
                low=float(b.low),
                close=float(b.close),
                volume=int(b.volume),
                vwap=float(b.vwap) if b.vwap else None,
            )
        )
    return bars


def replay(bars_by_symbol: dict[str, list[Bar]], scanner: Scanner) -> list[dict]:
    """Feed bars through the scanner in timestamp order, label the hits.

    Ordering matters: the scanner keeps rolling per-symbol state, and
    interleaving symbols by time is what live execution looks like.
    """
    stream = sorted(
        ((bar, symbol) for symbol, bars in bars_by_symbol.items() for bar in bars),
        key=lambda pair: pair[0].timestamp,
    )

    # Index of each symbol's bars so forward returns are a slice, not a scan.
    position = {symbol: 0 for symbol in bars_by_symbol}

    rows = []
    for bar, symbol in stream:
        index = position[symbol]
        position[symbol] += 1

        candidate = scanner.on_bar(bar)
        if candidate is None:
            continue

        forward = bars_by_symbol[symbol][index + 1 :]
        row = candidate.to_row()
        row.update(forward_metrics(candidate.price, forward))
        rows.append(row)

    return rows


# -------------------------------------------------------------------- I/O

def fetch_daily(data_client, symbols: list[str], day: date, feed) -> dict[str, dict]:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    out = {}
    for i in range(0, len(symbols), 400):
        request = StockBarsRequest(
            symbol_or_symbols=symbols[i : i + 400],
            timeframe=TimeFrame.Day,
            start=datetime.combine(day, time.min).replace(tzinfo=ET),
            end=datetime.combine(day, time.max).replace(tzinfo=ET),
            feed=feed,
        )
        for symbol, bars in data_client.get_stock_bars(request).data.items():
            if bars:
                b = bars[0]
                out[symbol] = {
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": int(b.volume),
                }
    return out


def fetch_minutes(data_client, symbols: list[str], day: date, feed) -> dict[str, list[Bar]]:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    out: dict[str, list[Bar]] = {}
    for i in range(0, len(symbols), 100):
        request = StockBarsRequest(
            symbol_or_symbols=symbols[i : i + 100],
            timeframe=TimeFrame.Minute,
            start=datetime.combine(day, SESSION_START).replace(tzinfo=ET),
            end=datetime.combine(day, SESSION_END).replace(tzinfo=ET),
            feed=feed,
        )
        for symbol, bars in data_client.get_stock_bars(request).data.items():
            converted = to_bars(bars, symbol)
            if converted:
                out[symbol] = converted
    return out


def replay_day(data_client, day: date, cfg: dict, refs: dict, feed,
               scanner: Scanner | None = None) -> list[dict]:
    symbols = sorted(refs)
    daily = fetch_daily(data_client, symbols, day, feed)
    if not daily:
        return []                                   # market holiday

    survivors = [s for s, bar in daily.items() if prescreen(bar, refs[s], cfg)]
    print(f"  {day}  {len(daily)} traded -> {len(survivors)} pre-screened",
          file=sys.stderr, end="")

    if not survivors:
        print(file=sys.stderr)
        return []

    minutes = fetch_minutes(data_client, survivors, day, feed)
    # One scanner across the whole range, not one per day: appearance counts
    # are a multi-day signal and reset to zero if the scanner is rebuilt.
    rows = replay(minutes, scanner or Scanner(cfg, refs))
    print(f" -> {len(rows)} candidates", file=sys.stderr)
    return rows


def write_parquet(rows: list[dict], path: pathlib.Path) -> None:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False)


def main() -> None:
    import argparse

    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient

    from data.reference import load_credentials

    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--refs", default=None,
                   help="reference file to use; defaults to the newest in data/refs")
    p.add_argument("--feed", default="sip", choices=["sip", "iex"],
                   help="sip needs a funded or verified account; iex is "
                        "paper-only and covers ~3%% of volume")
    p.add_argument("--config", default="config/scanner.yaml")
    args = p.parse_args()

    cfg = yaml.safe_load(pathlib.Path(args.config).read_text())
    refs = load_refs_for(args.refs)
    feed = DataFeed.SIP if args.feed == "sip" else DataFeed.IEX

    key, secret = load_credentials()
    client = StockHistoricalDataClient(key, secret)

    day = date.fromisoformat(args.start)
    last = date.fromisoformat(args.end)
    total = 0
    scanner = Scanner(cfg, refs)

    while day <= last:
        if day.weekday() < 5:                       # skip weekends cheaply
            out = OUT_DIR / f"{day.isoformat()}.parquet"
            if out.exists():
                print(f"  {day}  already done, skipping", file=sys.stderr)
            else:
                rows = replay_day(client, day, cfg, refs, feed, scanner)
                if rows:
                    write_parquet(rows, out)
                    total += len(rows)
        day += timedelta(days=1)

    print(f"\n{total} candidates written to {OUT_DIR}/", file=sys.stderr)


if __name__ == "__main__":
    main()
