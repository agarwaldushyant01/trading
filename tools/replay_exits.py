"""What would today's trades have done under the current exit rules?

    python -m tools.replay_exits --date 2026-09-03

Takes every entry the bot actually made, fetches the minute bars from that
entry to the close, and runs them through the exit logic as it stands now.
Then compares against what actually happened.

WHY THIS EXISTS

Exit rules get changed after a bad session, and the change always sounds
right in the abstract: "the trail was too wide", "the stop was too tight".
Whether it would actually have helped is a different question, and estimating
it from memory is how people convince themselves a fix worked.

On 2026-09-03 two changes went in — the trail now also keeps half the peak
gain, and structural stops are floored at 5%. This measures them against the
three trades that prompted them rather than asserting an improvement.

WHAT IT CANNOT SHOW

Whether the trade was worth taking. A better exit on an entry that should
never have happened still loses money, only more slowly. Entry selection is a
separate problem and this says nothing about it.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from data.reference import load_credentials

ET = ZoneInfo("America/New_York")


def entries_for(day: str) -> list:
    path = pathlib.Path("data/mosquito/trades.jsonl")
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip() or day not in line:
            continue
        r = json.loads(line)
        if r.get("kind") == "entry" and r.get("at", "").startswith(day):
            out.append(r)
    return out


def exits_for(day: str) -> dict:
    path = pathlib.Path("data/mosquito/trades.jsonl")
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        if not line.strip() or day not in line:
            continue
        r = json.loads(line)
        if r.get("kind") == "exit" and r.get("at", "").startswith(day):
            out[r["symbol"]] = r
    return out


def bars_after(client, symbol: str, start: datetime) -> list:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    end = start.replace(hour=15, minute=55, second=0, microsecond=0)
    if start >= end:
        end = start + timedelta(hours=2)
    try:
        raw = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=[symbol], timeframe=TimeFrame.Minute,
            start=start, end=end, feed=DataFeed.SIP)).data.get(symbol, [])
    except Exception as exc:                              # noqa: BLE001
        print(f"    {symbol}: fetch failed ({exc})", file=sys.stderr)
        return []
    return [{"t": b.timestamp.astimezone(ET).isoformat(), "h": float(b.high),
             "l": float(b.low), "c": float(b.close)} for b in raw]


def simulate(bars: list, entry: float, stop: float, cfg: dict
             ) -> tuple[str, float]:
    """The exit logic as it stands, applied bar by bar.

    Mirrors PaperTrader._trail_stop rather than importing it, because that
    method reads live broker state. Any divergence between the two would make
    this report misleading, so the arithmetic is kept deliberately simple and
    identical.
    """
    trail_pct = cfg.get("trail_pct", 12.0)
    arm_at = cfg.get("trail_arms_at_pct", 10.0)
    keep = cfg.get("keep_gain_fraction", 0.5)
    floor_pct = cfg.get("min_stop_pct", 5.0)

    stop = min(stop, entry * (1 - floor_pct / 100))
    peak = entry

    for bar in bars:
        if bar["l"] <= stop:
            reason = "trail" if peak > entry * 1.05 else "stop"
            return (reason, (stop / entry - 1) * 100)

        if bar["h"] > peak:
            peak = bar["h"]
            if peak >= entry * (1 + arm_at / 100):
                gain = peak - entry
                stop = max(stop,
                           peak * (1 - trail_pct / 100),
                           entry + gain * keep,
                           entry * 1.001)

    if not bars:
        return ("no data", 0.0)
    return ("close", (bars[-1]["c"] / entry - 1) * 100)


def main() -> None:
    import argparse

    from alpaca.data.historical import StockHistoricalDataClient

    p = argparse.ArgumentParser()
    p.add_argument("--date", default=date.today().isoformat())
    p.add_argument("--rules-config", default="config/rules.yaml")
    args = p.parse_args()

    cfg = yaml.safe_load(pathlib.Path(args.rules_config).read_text())
    execution = cfg.get("execution", {})

    entries = entries_for(args.date)
    exits = exits_for(args.date)
    if not entries:
        print(f"\n  No entries recorded for {args.date}.\n")
        return

    key, secret = load_credentials()
    client = StockHistoricalDataClient(key, secret)

    print(f"\n{'=' * 74}")
    print(f"  EXIT REPLAY — {args.date}")
    print(f"{'=' * 74}")
    print(f"\n  Current rules: {execution.get('min_stop_pct', 5)}% stop floor, "
          f"{execution.get('trail_pct', 12)}% trail, keeping "
          f"{execution.get('keep_gain_fraction', 0.5):.0%} of the peak gain\n")
    print(f"  {'':<7}{'entry':>9}{'actual':>10}{'':<12}"
          f"{'replayed':>10}{'':<12}{'change':>9}")
    print(f"  {'-' * 70}")

    actual_total = replay_total = 0.0

    for e in entries:
        symbol = e["symbol"]
        entry = e["signal_price"]
        start = datetime.fromisoformat(e["at"])
        bars = bars_after(client, symbol, start)

        was = exits.get(symbol)
        actual_pnl = was["pnl_pct"] if was else None
        actual_reason = was["exit_reason"] if was else "not logged"

        reason, pnl = simulate(bars, entry, e["stop"], execution)

        shares = e["shares"]
        if actual_pnl is not None:
            actual_total += actual_pnl / 100 * shares * entry
        replay_total += pnl / 100 * shares * entry

        actual_s = (f"{actual_pnl:+.2f}%" if actual_pnl is not None else "  -  ")
        delta = (f"{pnl - actual_pnl:+.2f}" if actual_pnl is not None else "  -  ")
        print(f"  {symbol:<7}{entry:>9.4f}{actual_s:>10}  "
              f"{actual_reason:<10}{pnl:>+9.2f}%  {reason:<10}{delta:>9}")

    print(f"\n  actual   ${actual_total:>+10,.0f}")
    print(f"  replayed ${replay_total:>+10,.0f}")
    print(f"  difference ${replay_total - actual_total:>+8,.0f}")

    print(f"""
  This measures the exit changes only. It says nothing about whether these
  entries were worth taking — a better exit on a trade that should not have
  been opened still loses money.
""")


if __name__ == "__main__":
    main()
