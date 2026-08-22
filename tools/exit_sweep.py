"""Which stop and target would have made this sample work?

    python -m tools.exit_sweep
    python -m tools.exit_sweep --date 2026-08-21
    python -m tools.exit_sweep --time-stop 30

Every candidate the rules produced in a live session — taken or not — replayed
against a grid of stop and target combinations. Same entries, same minute
bars, different exits.

Entry selection is not in question here. Several of these names ran 25% or
more; the scanner found them. What the first two sessions showed is that a
12% stop against a 25% target, hit at a 20-25% win rate, loses money: that
geometry needs roughly a 33% win rate to break even.

This answers the narrower question of whether any geometry would have made
the same trades profitable. If none does, the problem is upstream and no
amount of exit tuning will fix it — which is worth knowing before spending
another week live.

Bars are cached to data/bars/ so re-running a different grid is fast.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from data.reference import load_credentials

ET = ZoneInfo("America/New_York")
CACHE = pathlib.Path("data/bars/sweep")


@dataclass
class Candidate:
    symbol: str
    at: datetime
    entry: float
    pct_change: float
    setup: str
    source: str          # approved | rejected | expired | entry


def collect(day: str | None) -> list[Candidate]:
    """Every candidate the rules accepted, from both journals.

    Approvals and direct entries are both included — what matters is that the
    rules said yes, not whether a human then intervened.
    """
    out: list[Candidate] = []
    seen = set()

    approvals = pathlib.Path("data/mosquito/approvals.jsonl")
    if approvals.exists():
        for line in approvals.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if day and not r["at"].startswith(day):
                continue
            key = (r["symbol"], r["at"][:16])
            if key in seen:
                continue
            seen.add(key)
            out.append(Candidate(
                symbol=r["symbol"],
                at=datetime.fromisoformat(r["at"]).astimezone(ET),
                entry=r["price"],
                pct_change=r.get("pct_change", 0.0),
                setup=r.get("setup", ""),
                source=("approved" if r["approved"]
                        else "rejected" if r["resolved_by"] == "manual"
                        else "expired"),
            ))

    trades = pathlib.Path("data/mosquito/trades.jsonl")
    if trades.exists():
        for line in trades.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r["kind"] not in ("entry", "awaiting_approval"):
                continue
            if day and not r["at"].startswith(day):
                continue
            key = (r["symbol"], r["at"][:16])
            if key in seen:
                continue
            seen.add(key)
            out.append(Candidate(
                symbol=r["symbol"],
                at=datetime.fromisoformat(r["at"]).astimezone(ET),
                entry=r["signal_price"],
                pct_change=0.0,
                setup=r.get("setup", ""),
                source="entry",
            ))

    return sorted(out, key=lambda c: c.at)


def bars_for(candidate: Candidate) -> list:
    """Minute bars from entry to the close, cached on disk."""
    CACHE.mkdir(parents=True, exist_ok=True)
    stamp = candidate.at.strftime("%Y%m%d-%H%M")
    path = CACHE / f"{candidate.symbol}-{stamp}.json"

    if path.exists():
        return json.loads(path.read_text())

    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    key, secret = load_credentials()
    client = StockHistoricalDataClient(key, secret)
    end = candidate.at.replace(hour=15, minute=50, second=0, microsecond=0)
    if candidate.at >= end:
        path.write_text("[]")
        return []

    try:
        raw = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=[candidate.symbol], timeframe=TimeFrame.Minute,
            start=candidate.at, end=end, feed=DataFeed.SIP,
        )).data.get(candidate.symbol, [])
    except Exception:                                     # noqa: BLE001
        raw = []

    rows = [{"t": b.timestamp.isoformat(), "h": float(b.high),
             "l": float(b.low), "c": float(b.close)} for b in raw]
    path.write_text(json.dumps(rows))
    return rows


def simulate(bars: list, entry: float, stop_pct: float, target_pct: float,
             time_stop_min: int | None) -> tuple[str, float] | None:
    """Walk the bars applying one stop/target pair.

    The stop wins when a bar spans both levels — the pessimistic assumption,
    matching the backtest engine. Optimistic fills here would make every
    result look better than it could be in practice.
    """
    if not bars:
        return None

    stop = entry * (1 - stop_pct / 100)
    target = entry * (1 + target_pct / 100)
    start = datetime.fromisoformat(bars[0]["t"])

    for i, bar in enumerate(bars):
        if bar["l"] <= stop:
            return ("stop", -stop_pct)
        if bar["h"] >= target:
            return ("target", target_pct)
        if time_stop_min and i >= time_stop_min:
            return ("time_stop", (bar["c"] / entry - 1) * 100)

    return ("close", (bars[-1]["c"] / entry - 1) * 100)


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--date", default=None)
    p.add_argument("--time-stop", type=int, default=None,
                   help="also exit after N minutes regardless")
    p.add_argument("--stops", default="5,7,8,10,12,15")
    p.add_argument("--targets", default="10,15,20,25,30")
    args = p.parse_args()

    candidates = collect(args.date)
    if not candidates:
        print("No candidates found in the journals.")
        return

    print(f"\n  {len(candidates)} candidates"
          f"{' on ' + args.date if args.date else ''}")
    print(f"  fetching bars (cached after the first run)...", flush=True)

    loaded = []
    for c in candidates:
        bars = bars_for(c)
        if bars:
            loaded.append((c, bars))
    print(f"  {len(loaded)} with usable data\n")

    if not loaded:
        return

    stops = [float(x) for x in args.stops.split(",")]
    targets = [float(x) for x in args.targets.split(",")]

    print(f"  Average return per trade, by stop and target"
          f"{f' (time stop {args.time_stop}m)' if args.time_stop else ''}:\n")
    header = "  stop \\ target" + "".join(f"{t:>9.0f}%" for t in targets)
    print(header)
    print("  " + "-" * (len(header) - 2))

    grid = {}
    for stop_pct in stops:
        row = f"  {stop_pct:>10.0f}%  "
        for target_pct in targets:
            results = []
            for c, bars in loaded:
                out = simulate(bars, c.entry, stop_pct, target_pct,
                               args.time_stop)
                if out:
                    results.append(out[1])
            avg = mean(results) if results else 0.0
            grid[(stop_pct, target_pct)] = (avg, results)
            row += f"{avg:>+9.1f}"
        print(row)

    best = max(grid.items(), key=lambda kv: kv[1][0])
    (bs, bt), (bavg, bres) = best
    wins = [x for x in bres if x > 0]

    print(f"\n  Best: stop {bs:.0f}% / target {bt:.0f}%")
    print(f"    average {bavg:+.2f}% per trade")
    print(f"    {len(wins)}/{len(bres)} profitable ({len(wins)/len(bres)*100:.0f}%)")
    print(f"    over {len(bres)} trades: {sum(bres):+.0f}% total")

    current = grid.get((12.0, 25.0))
    if current:
        print(f"\n  Current config (12% / 25%): {current[0]:+.2f}% per trade")
        print(f"    difference: {bavg - current[0]:+.2f} points per trade")

    if bavg <= 0:
        print(f"\n  NO combination is profitable on this sample.")
        print(f"  That points upstream of the exits — the entries themselves,")
        print(f"  or the moment of entry. Exit tuning will not fix it.")
    else:
        print(f"\n  Caveat: this grid was chosen by looking at these same")
        print(f"  trades, so the best cell is optimistic by construction.")
        print(f"  Two sessions is far too few to fix parameters on. Treat it")
        print(f"  as a direction, then confirm it forward.")
    print()


if __name__ == "__main__":
    main()
