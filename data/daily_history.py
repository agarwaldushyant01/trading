"""Daily bar history, queryable as of any past date.

    python -m data.daily_history --start 2025-06-01 --end 2026-03-01

Setups 2 and 3 both ask questions about a stock's recent past — did it run,
how far has it fallen, has it stopped making lower lows. Those answers change
every day, so a single reference snapshot cannot serve them: using March data
to judge a September trade is look-ahead, and it is the kind that makes a
backtest look brilliant and a live account lose money.

This module caches one row per symbol per day and answers strictly from bars
dated BEFORE the date you ask about.
"""

from __future__ import annotations

import pathlib
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

CACHE = pathlib.Path("data/daily_history.parquet")


@dataclass(frozen=True)
class DailyBar:
    day: date
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class DailyContext:
    """What a symbol's recent past looked like, as of a given morning."""

    had_runner: bool           # a big intraday move inside the lookback
    runner_high: float         # the peak it reached
    pct_off_runner_high: float # how far it has fallen from that peak (negative)
    lower_low_streak: int      # consecutive sessions making lower lows
    sessions: int              # how much history was actually available


# --------------------------------------------------------------- pure core

def build_context(
    bars: list[DailyBar],
    as_of: date,
    lookback: int = 30,
    runner_move_pct: float = 50.0,
) -> DailyContext:
    """Summarize a symbol's recent past using only bars before `as_of`.

    The date filter is the whole point. Passing bars that include `as_of`
    itself, or anything after it, silently gives the strategy tomorrow's
    prices.
    """
    history = [b for b in bars if b.day < as_of][-lookback:]
    if len(history) < 5:
        return DailyContext(False, 0.0, 0.0, 0, len(history))

    had_runner = any(
        b.low > 0 and (b.high / b.low - 1) * 100 >= runner_move_pct
        for b in history
    )
    runner_high = max(b.high for b in history)
    last_close = history[-1].close
    pct_off = (last_close / runner_high - 1) * 100 if runner_high else 0.0

    streak = 0
    for earlier, later in zip(reversed(history[:-1]), reversed(history[1:])):
        if later.low < earlier.low:
            streak += 1
        else:
            break

    return DailyContext(
        had_runner=had_runner,
        runner_high=round(runner_high, 4),
        pct_off_runner_high=round(pct_off, 2),
        lower_low_streak=streak,
        sessions=len(history),
    )


class History:
    """In-memory index of the cached daily bars."""

    def __init__(self, by_symbol: dict[str, list[DailyBar]]) -> None:
        self.by_symbol = by_symbol

    @classmethod
    def load(cls, path: pathlib.Path = CACHE) -> "History":
        import pandas as pd

        if not path.exists():
            raise SystemExit(
                "No daily history. Build it first:\n"
                "  python -m data.daily_history --start ... --end ..."
            )
        frame = pd.read_parquet(path)
        out: dict[str, list[DailyBar]] = {}
        for row in frame.itertuples(index=False):
            day = row.day if isinstance(row.day, date) else row.day.date()
            out.setdefault(row.symbol, []).append(
                DailyBar(day, row.high, row.low, row.close, int(row.volume))
            )
        for bars in out.values():
            bars.sort(key=lambda b: b.day)
        return cls(out)

    def context(self, symbol: str, as_of: date, **kwargs) -> DailyContext:
        return build_context(self.by_symbol.get(symbol, []), as_of, **kwargs)


# -------------------------------------------------------------------- I/O

def fetch(client, symbols: list[str], start: date, end: date, feed) -> list[dict]:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    rows = []
    for i in range(0, len(symbols), 300):
        chunk = symbols[i : i + 300]
        request = StockBarsRequest(
            symbol_or_symbols=chunk,
            timeframe=TimeFrame.Day,
            start=datetime.combine(start, time.min),
            end=datetime.combine(end, time.max),
        )
        for symbol, bars in client.get_stock_bars(request).data.items():
            for b in bars:
                rows.append({
                    "symbol": symbol,
                    "day": b.timestamp.date(),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": int(b.volume),
                })
        print(f"  {min(i + 300, len(symbols))}/{len(symbols)} symbols",
              file=sys.stderr)
    return rows


def main() -> None:
    import argparse

    import pandas as pd
    from alpaca.data.historical import StockHistoricalDataClient

    from data.reference import load_credentials, load_refs_for

    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True,
                   help="fetch from here; go ~3 months before your backtest "
                        "start so day one has a full lookback")
    p.add_argument("--end", required=True)
    args = p.parse_args()

    refs = load_refs_for(None)
    key, secret = load_credentials()
    client = StockHistoricalDataClient(key, secret)

    print(f"Fetching daily bars for {len(refs)} symbols...", file=sys.stderr)
    rows = fetch(client, sorted(refs), date.fromisoformat(args.start),
                 date.fromisoformat(args.end), None)

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(CACHE, index=False)
    print(f"Wrote {len(rows):,} daily bars to {CACHE}", file=sys.stderr)


if __name__ == "__main__":
    main()
