"""Does the detector find the trades you actually took?

    python -m tools.validate

The only test that matters before this goes anywhere near live orders. Every
backtest so far reported an average and told us nothing, because we had no
idea what the right answer looked like. Here we do: 32 real trades, 16
winners and 16 losers, with entry prices and dates.

Three things it reports:

  RECALL     of the 16 winners, how many does the detector find? A rule that
             cannot see trades you took is encoding something other than what
             you do, and no amount of parameter tuning fixes that.

  PRECISION  of the 16 losers, how many does it correctly refuse? Finding
             every winner is trivial if you also take everything else.

  TIMING     when it does find a trade, is the entry near yours? Detecting
             GIPR forty minutes after you bought it is not the same trade.

A detector that finds 12 of 16 winners and avoids 10 of 16 losers is worth
developing. One that finds 3 is not, however good the average looks.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from data.reference import load_credentials
from patterns.detect import detect

ET = ZoneInfo("America/New_York")
CACHE = pathlib.Path("data/bars/validate")

# The logged trades. Entry prices are as recorded; exits are omitted because
# what is being tested is entry detection, not exit management.
WINNERS = [
    ("2026-08-24", "BTCT", 1.19),  ("2026-08-24", "LUCY", 0.71),
    ("2026-08-24", "XPON", 4.07),  ("2026-08-24", "DAIC", 0.88),
    ("2026-08-24", "GIPR", 0.5941), ("2026-08-24", "JEM", 4.66),
    ("2026-08-24", "JEM", 5.19),   ("2026-08-25", "TNMG", 0.538),
    ("2026-08-25", "SWVL", 1.82),  ("2026-08-25", "FNGR", 0.32),
    ("2026-08-25", "NCPL", 0.48),  ("2026-08-26", "VNRX", 0.40),
    ("2026-08-26", "RPGL", 1.72),  ("2026-08-26", "WKSP", 0.64),
    ("2026-08-27", "GSUN", 0.29),  ("2026-08-27", "EPOW", 0.48),
]

LOSERS = [
    ("2026-08-24", "OLOX", 2.52),  ("2026-08-25", "PRZO", 0.93),
    ("2026-08-25", "CBAT", 0.98),  ("2026-08-25", "RIME", 0.39),
    ("2026-08-25", "RMSG", 0.44),  ("2026-08-25", "DFNS", 16.70),
    ("2026-08-25", "LHSW", 3.65),  ("2026-08-25", "SCAG", 0.30),
    ("2026-08-25", "AKAN", 4.63),  ("2026-08-26", "TRUG", 0.82),
    ("2026-08-26", "SMTK", 5.54),  ("2026-08-26", "LUCY", 1.23),
    ("2026-08-26", "NCPL", 0.56),  ("2026-08-27", "FNGR", 0.18),
    ("2026-08-27", "ONFO", 1.51),  ("2026-08-27", "SOAR", 0.21),
]


def five_minute_bars(client, symbol: str, day: date) -> list:
    """Regular-session 5-minute bars, cached.

    Five minutes is the primary timeframe the setups are drawn on.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{symbol}-{day.isoformat()}-5m.json"
    if path.exists():
        return json.loads(path.read_text())

    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    start = datetime.combine(day, datetime.min.time(), ET).replace(hour=4)
    end = start.replace(hour=16)
    try:
        raw = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start, end=end, feed=DataFeed.SIP)).data.get(symbol, [])
    except Exception as exc:                              # noqa: BLE001
        print(f"    {symbol} {day}: fetch failed ({exc})", file=sys.stderr)
        raw = []

    rows = [{"t": b.timestamp.astimezone(ET).isoformat(), "o": float(b.open),
             "h": float(b.high), "l": float(b.low), "c": float(b.close),
             "v": float(b.volume)} for b in raw]
    path.write_text(json.dumps(rows))
    return rows


def daily_bars(client, symbol: str, day: date) -> list:
    """Daily bars before the trade, for higher-timeframe trend and levels."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{symbol}-{day.isoformat()}-1d.json"
    if path.exists():
        return json.loads(path.read_text())

    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    try:
        raw = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=[symbol], timeframe=TimeFrame.Day,
            start=datetime.combine(day - timedelta(days=90),
                                   datetime.min.time(), ET),
            end=datetime.combine(day - timedelta(days=1),
                                 datetime.min.time(), ET),
            feed=DataFeed.SIP)).data.get(symbol, [])
    except Exception:                                     # noqa: BLE001
        raw = []

    rows = [{"t": b.timestamp.isoformat(), "o": float(b.open),
             "h": float(b.high), "l": float(b.low), "c": float(b.close),
             "v": float(b.volume)} for b in raw]
    path.write_text(json.dumps(rows))
    return rows


def levels_from(daily: list, min_touches: int = 2,
                tolerance_pct: float = 2.0) -> list:
    """Prior swing highs that price reversed from and has not cleared.

    These are the levels drawn from a higher timeframe: a rejection from
    three months ago is as real as last week's, because it stays valid until
    it is broken rather than expiring with age.
    """
    if len(daily) < 5:
        return []

    highs = []
    for i in range(2, len(daily) - 2):
        window = daily[i - 2:i + 3]
        if daily[i]["h"] >= max(b["h"] for b in window):
            highs.append(daily[i]["h"])

    clusters = []
    for price in sorted(highs, reverse=True):
        for c in clusters:
            if abs(price - c[0]) / c[0] * 100 <= tolerance_pct:
                c.append(price)
                break
        else:
            clusters.append([price])

    return sorted(sum(c) / len(c) for c in clusters if len(c) >= min_touches)


def check(client, day_s: str, symbol: str, entry: float) -> dict:
    """Run the detector on one traded symbol-day."""
    day = date.fromisoformat(day_s)
    bars = five_minute_bars(client, symbol, day)
    if len(bars) < 20:
        return {"status": "no data", "found": False}

    daily = daily_bars(client, symbol, day)
    levels = levels_from(daily)

    setups = detect(bars, daily=daily, levels=levels)
    taken = [s for s in setups if not s.rejected]

    if not taken:
        reasons = {}
        for s in setups:
            if s.rejected:
                reasons[s.rejected] = reasons.get(s.rejected, 0) + 1
        top = max(reasons.items(), key=lambda kv: kv[1])[0] if reasons else \
            "no pattern at all"
        return {"status": top, "found": False}

    # Closest detection to the recorded entry price.
    best = min(taken, key=lambda s: abs(s.entry - entry))
    drift = (best.entry / entry - 1) * 100 if entry else 0
    return {"status": f"{best.kind} {best.grade}", "found": True,
            "entry": best.entry, "drift": drift,
            "time": bars[best.index]["t"][11:16],
            "count": len(taken)}


def main() -> None:
    from alpaca.data.historical import StockHistoricalDataClient

    key, secret = load_credentials()
    client = StockHistoricalDataClient(key, secret)

    print(f"\n{'=' * 74}")
    print(f"  VALIDATION — does the detector find your trades?")
    print(f"{'=' * 74}")

    print(f"\n  WINNERS you took ({len(WINNERS)}) — we want these FOUND\n")
    print(f"  {'':<6}{'date':<12}{'your entry':>11}   {'detector':<28}")
    print(f"  {'-' * 66}")
    hits = 0
    for day, symbol, entry in WINNERS:
        r = check(client, day, symbol, entry)
        if r["found"]:
            hits += 1
            mark = "FOUND"
            extra = (f"{r['status']} @ {r['entry']:.4f} "
                     f"({r['drift']:+.1f}%) {r['time']}")
        else:
            mark = "  -  "
            extra = r["status"]
        print(f"  {symbol:<6}{day:<12}{entry:>11.4f}   {mark}  {extra}")

    print(f"\n  RECALL: {hits}/{len(WINNERS)} "
          f"({hits / len(WINNERS) * 100:.0f}%)")

    print(f"\n\n  LOSERS you took ({len(LOSERS)}) — we want these AVOIDED\n")
    print(f"  {'':<6}{'date':<12}{'your entry':>11}   {'detector':<28}")
    print(f"  {'-' * 66}")
    avoided = 0
    for day, symbol, entry in LOSERS:
        r = check(client, day, symbol, entry)
        if not r["found"]:
            avoided += 1
            print(f"  {symbol:<6}{day:<12}{entry:>11.4f}   avoided  {r['status']}")
        else:
            print(f"  {symbol:<6}{day:<12}{entry:>11.4f}   TOOK IT  "
                  f"{r['status']} @ {r['entry']:.4f}")

    print(f"\n  AVOIDED: {avoided}/{len(LOSERS)} "
          f"({avoided / len(LOSERS) * 100:.0f}%)")

    print(f"\n{'=' * 74}")
    total = hits + avoided
    print(f"  Agrees with you on {total}/32 decisions "
          f"({total / 32 * 100:.0f}%)\n")

    if hits < 6:
        print(f"  Recall is too low to proceed. The detector is not seeing")
        print(f"  what you see, and tuning thresholds will not close a gap")
        print(f"  this size — something structural is missing.")
    elif hits >= 10 and avoided >= 8:
        print(f"  Worth taking forward. Next step is exits and sizing, then")
        print(f"  paper — not live.")
    else:
        print(f"  Partial. Look at the refusal reasons above: if one reason")
        print(f"  dominates the misses, that is the parameter to revisit.")
    print()


if __name__ == "__main__":
    main()
