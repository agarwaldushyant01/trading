"""Score every stock's character, once, and cache it.

    python -m tools.build_character
    python -m tools.build_character --days 120 --refresh

Fetches daily bars for the whole universe and scores each name's behaviour:
how often a spike was fully given back, how hard it falls afterwards, whether
rallies hold. Writes data/character.json for the live driver to read at
startup.

WHY THIS MATTERS MORE THAN IT LOOKS

Every pass the trader logged on 2026-09-03 was about a stock's history rather
than the setup in front of them — "has huge pump and dump price action
previously", "falls rapidly on dumps", "have lost on it previously". None of
that is visible in a five-minute chart of today.

Until now the live driver scored 27 symbols out of 12,985, because it read
whatever daily bars happened to be cached from validation runs. So the filter
doing most of the work in the trader's own selection was inactive on 99.8% of
the universe.

The fetch is batched — Alpaca accepts many symbols per request — so 13,000
names take minutes rather than hours. Run it weekly; character changes slowly
and a stale score is far better than none.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from data.reference import load_credentials, load_refs_for
from patterns.character import analyse

ET = ZoneInfo("America/New_York")
OUT = pathlib.Path("data/character.json")
BATCH = 200


def fetch_batch(client, symbols: list, start: datetime,
                end: datetime) -> dict:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    try:
        result = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
            start=start, end=end, feed=DataFeed.SIP)).data
    except Exception as exc:                              # noqa: BLE001
        print(f"    batch failed ({type(exc).__name__}), splitting",
              file=sys.stderr)
        if len(symbols) <= 1:
            return {}
        mid = len(symbols) // 2
        out = fetch_batch(client, symbols[:mid], start, end)
        out.update(fetch_batch(client, symbols[mid:], start, end))
        return out

    return {sym: [{"o": float(b.open), "h": float(b.high),
                   "l": float(b.low), "c": float(b.close),
                   "v": float(b.volume)} for b in bars]
            for sym, bars in result.items()}


def main() -> None:
    import argparse

    from alpaca.data.historical import StockHistoricalDataClient

    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=120,
                   help="how much history to score against")
    p.add_argument("--refresh", action="store_true",
                   help="rescore symbols already in the cache")
    p.add_argument("--max-price", type=float, default=25.0,
                   help="skip names well outside the traded range")
    args = p.parse_args()

    refs = load_refs_for(None)
    if not refs:
        raise SystemExit("No reference data. Run data.reference first.")

    existing = {}
    if OUT.exists() and not args.refresh:
        try:
            existing = json.loads(OUT.read_text())
        except Exception:                                 # noqa: BLE001
            existing = {}

    # Only names that could plausibly be traded. Scoring the whole listed
    # market would triple the runtime for symbols the universe filter
    # rejects anyway.
    candidates = [s for s, r in refs.items()
                  if r.prior_close and r.prior_close <= args.max_price]
    todo = [s for s in candidates if s not in existing]

    print(f"\n{'=' * 66}")
    print(f"  BUILDING CHARACTER CACHE")
    print(f"{'=' * 66}\n")
    print(f"  {len(refs):,} in the universe")
    print(f"  {len(candidates):,} under ${args.max_price:.0f}")
    print(f"  {len(existing):,} already scored, {len(todo):,} to fetch")

    if not todo:
        print(f"\n  Nothing to do. Use --refresh to rescore.\n")
        return

    key, secret = load_credentials()
    client = StockHistoricalDataClient(key, secret)

    end = datetime.now(ET) - timedelta(days=1)
    start = end - timedelta(days=args.days * 2)     # calendar vs trading days

    print(f"\n  Fetching {args.days} sessions in batches of {BATCH}...\n")

    scored = dict(existing)
    verdicts: dict = defaultdict(int)
    fetched = 0

    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        bars = fetch_batch(client, batch, start, end)
        fetched += len(bars)

        for symbol, rows in bars.items():
            c = analyse(rows)
            scored[symbol] = {
                "verdict": c.verdict,
                "spikes": c.spikes,
                "pump_dumps": c.pump_dumps,
                "avg_fall_pct": round(c.avg_fall_pct, 1),
                "follow_through": round(c.follow_through, 2),
                "sessions": c.sessions,
                "reasons": c.reasons,
                "scored_on": date.today().isoformat(),
            }
            verdicts[c.verdict] += 1

        done = min(i + BATCH, len(todo))
        print(f"    {done:>6,}/{len(todo):,}   {fetched:,} with data",
              flush=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(scored, indent=1))

    print(f"\n  Scored {len(scored):,} symbols -> {OUT}\n")
    for verdict, n in sorted(verdicts.items(), key=lambda kv: -kv[1]):
        share = n / max(sum(verdicts.values()), 1) * 100
        marker = "  <- excluded" if verdict in ("pump and dump",
                                                "falls hard") else ""
        print(f"    {verdict:<16} {n:>6,}  ({share:>4.1f}%){marker}")

    excluded = verdicts["pump and dump"] + verdicts["falls hard"]
    print(f"\n  {excluded:,} names the filter will now refuse.")
    print(f"\n  Character changes slowly — rerun weekly, not daily.\n")


if __name__ == "__main__":
    main()
