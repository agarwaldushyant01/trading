"""Daily reference data — the static per-symbol facts the scanner needs.

Run once before the session (or once per historical date when backtesting):

    python -m data.reference --out data/refs/2026-03-10.json

Produces one TickerRef per symbol: exchange, prior close, prior high, ATR(14),
20-day average volume, shares outstanding.

The computation is separated from the fetching so the maths can be unit tested
without a network call. `compute_ref` is pure; everything above it is I/O.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scanner.scanner import TickerRef

# Exchanges we trade. Alpaca reports OTC as "OTC" — excluded by omission.
LISTED_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS"}

ATR_PERIOD = 14
ADV_PERIOD = 20


# --------------------------------------------------------------- pure core

def true_range(high: float, low: float, prior_close: float | None) -> float:
    """Wilder's true range. Falls back to the bar range on the first bar."""
    if prior_close is None:
        return high - low
    return max(high - low, abs(high - prior_close), abs(low - prior_close))


def atr(bars: list[dict], period: int = ATR_PERIOD) -> float:
    """Simple average of true range over `period` bars.

    Simple rather than Wilder-smoothed: it is easier to reason about and the
    difference is immaterial for a stop-width sanity check.
    """
    if len(bars) < 2:
        return 0.0
    trs = [
        true_range(b["high"], b["low"], bars[i - 1]["close"] if i else None)
        for i, b in enumerate(bars)
    ]
    window = trs[-period:]
    return sum(window) / len(window)


def compute_ref(
    symbol: str,
    exchange: str,
    daily_bars: list[dict],
    shares_outstanding: float,
) -> TickerRef | None:
    """Build one TickerRef from a symbol's daily bars, oldest first.

    Returns None when there is not enough history to compute the fields —
    a symbol with two days of data cannot be filtered sensibly, and letting
    it through with zeros would quietly corrupt the scan.
    """
    if len(daily_bars) < ATR_PERIOD:
        return None

    prior = daily_bars[-1]
    adv_window = daily_bars[-ADV_PERIOD:]
    avg_volume = sum(b["volume"] for b in adv_window) / len(adv_window)

    return TickerRef(
        symbol=symbol,
        exchange=exchange,
        shares_outstanding=shares_outstanding,
        avg_20d_volume=round(avg_volume),
        prior_close=round(prior["close"], 4),
        prior_high=round(prior["high"], 4),
        atr_14=round(atr(daily_bars), 4),
    )


# -------------------------------------------------------------------- I/O

def load_credentials() -> tuple[str, str]:
    """Read keys from the environment, or from a .env file beside the repo."""
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")

    if not (key and secret):
        env = pathlib.Path(__file__).resolve().parents[1] / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
            key = os.environ.get("ALPACA_API_KEY")
            secret = os.environ.get("ALPACA_SECRET_KEY")

    if not (key and secret):
        raise SystemExit(
            "Missing credentials. Create a .env file in the repo root:\n"
            "  ALPACA_API_KEY=your_key\n"
            "  ALPACA_SECRET_KEY=your_secret"
        )
    return key, secret


def fetch_listed_symbols(trading_client) -> dict[str, str]:
    """Return {symbol: exchange} for active, tradable US equities on a real
    exchange. This is where OTC is excluded — at the universe level, before
    any bar is fetched.
    """
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    assets = trading_client.get_all_assets(
        GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    )
    return {
        a.symbol: str(a.exchange.value if hasattr(a.exchange, "value") else a.exchange)
        for a in assets
        if a.tradable
        and str(a.exchange.value if hasattr(a.exchange, "value") else a.exchange)
        in LISTED_EXCHANGES
    }


def fetch_daily_bars(data_client, symbols: list[str], as_of: date, lookback: int = 60):
    """Daily bars for a batch of symbols, ending the day before `as_of`.

    Chunked because the request URL length is bounded. 200 symbols per call
    is comfortably inside the limit.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    out: dict[str, list[dict]] = {}
    start = as_of - timedelta(days=lookback * 2)   # calendar days -> trading days

    for i in range(0, len(symbols), 200):
        chunk = symbols[i : i + 200]
        request = StockBarsRequest(
            symbol_or_symbols=chunk,
            timeframe=TimeFrame.Day,
            start=datetime.combine(start, datetime.min.time()),
            end=datetime.combine(as_of - timedelta(days=1), datetime.max.time()),
        )
        barset = data_client.get_stock_bars(request)
        for symbol, bars in barset.data.items():
            out[symbol] = [
                {
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": int(b.volume),
                }
                for b in bars
            ]
        print(f"  fetched {min(i + 200, len(symbols))}/{len(symbols)} symbols",
              file=sys.stderr)
    return out


def load_shares_outstanding() -> dict[str, float]:
    """Shares outstanding per symbol, from SEC EDGAR.

    Not available from Alpaca — their assets endpoint returns exchange,
    tradability and borrow flags, but no share count. Build the file with:

        python -m data.shares_outstanding --email you@example.com

    Symbols absent from this map are DROPPED, not defaulted. That is
    deliberate: only operating companies report shares outstanding to the
    SEC, so absence is a reliable signal that a symbol is an ETF, fund,
    warrant or unit — none of which your setups apply to.

    Shares outstanding is a proxy for float and overstates it, sometimes
    badly, on the exact low-float names you trade. Treat the filter as
    approximate until real float data is in place.
    """
    path = pathlib.Path(__file__).parent / "shares_outstanding.json"
    if path.exists():
        return {k: float(v) for k, v in json.loads(path.read_text()).items()}
    return {}


def build(as_of: date) -> dict[str, TickerRef]:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient

    key, secret = load_credentials()
    trading = TradingClient(key, secret, paper=True)
    data = StockHistoricalDataClient(key, secret)

    print("Fetching asset list...", file=sys.stderr)
    exchanges = fetch_listed_symbols(trading)
    print(f"  {len(exchanges)} listed, tradable US equities", file=sys.stderr)

    shares = load_shares_outstanding()
    if not shares:
        raise SystemExit(
            "No shares-outstanding data. Build it first:\n"
            "  python -m data.shares_outstanding --email you@example.com"
        )

    print("Fetching daily bars...", file=sys.stderr)
    bars = fetch_daily_bars(data, sorted(exchanges), as_of)

    refs = {}
    no_shares = short_history = 0
    for symbol, daily in bars.items():
        if symbol not in shares:
            no_shares += 1        # ETF, fund, warrant, unit or non-filer
            continue
        ref = compute_ref(symbol, exchanges[symbol], daily, shares[symbol])
        if ref is None:
            short_history += 1
            continue
        refs[symbol] = ref

    print(f"  {no_shares} dropped: no SEC share count (ETFs, funds, warrants)",
          file=sys.stderr)
    print(f"  {short_history} dropped: under {ATR_PERIOD} sessions of history",
          file=sys.stderr)
    print(f"  {len(refs)} symbols in the universe", file=sys.stderr)
    return refs


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--date", default=date.today().isoformat(),
                   help="session date the refs are for (YYYY-MM-DD)")
    p.add_argument("--out", default=None)
    args = p.parse_args()

    as_of = date.fromisoformat(args.date)
    refs = build(as_of)

    out = pathlib.Path(args.out or f"data/refs/{as_of.isoformat()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({s: asdict(r) for s, r in refs.items()}, indent=1))
    print(f"Wrote {len(refs)} refs to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
