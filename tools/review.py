"""End-of-day review — what the alerts did, and what you did about them.

    python -m tools.review --date 2026-08-17            # look at the day
    python -m tools.review --date 2026-08-17 --annotate # record your calls

This is the point of running the scanner for the next few weeks.

The backtest could only ask "what if you took every alert", and the answer
was that it loses money. The interesting question is different: does YOUR
selection beat the raw alert? That needs both halves — what happened, and
what you chose — and only you can supply the second one.

Skipped alerts matter as much as taken ones. A record of only the trades you
took cannot tell you whether your filtering helped or hurt; the ones you
passed on are the control group.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from data.reference import load_credentials
from drivers.replay import fetch_minutes

ALERT_DIR = pathlib.Path("data/live")
DECISION_DIR = pathlib.Path("data/decisions")
HORIZONS = (15, 30, 60)


def load_alerts(day: date) -> list[dict]:
    path = ALERT_DIR / f"{day.isoformat()}.jsonl"
    if not path.exists():
        raise SystemExit(f"No alerts logged for {day}. Expected {path}")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def outcomes(alerts: list[dict], bars_by_symbol: dict) -> list[dict]:
    """What each alerted symbol did afterwards.

    MFE and MAE matter more than the point-in-time returns: together they say
    whether a trade was ever comfortable, or whether it only worked for
    someone who sat through a large drawdown.
    """
    from datetime import datetime

    enriched = []
    for alert in alerts:
        bars = bars_by_symbol.get(alert["symbol"], [])
        fired_at = datetime.fromisoformat(alert["timestamp"])
        after = [b for b in bars if b.timestamp > fired_at]

        row = dict(alert)
        entry = alert["price"]

        for horizon in HORIZONS:
            row[f"pct_{horizon}m"] = (
                round((after[horizon - 1].close / entry - 1) * 100, 1)
                if len(after) >= horizon else None
            )

        window = after[:60]
        row["mfe"] = round((max(b.high for b in window) / entry - 1) * 100, 1) if window else None
        row["mae"] = round((min(b.low for b in window) / entry - 1) * 100, 1) if window else None
        enriched.append(row)

    return enriched


def show(rows: list[dict]) -> None:
    print(f"\n{len(rows)} alerts\n")
    header = (f"{'time':<6} {'symbol':<7} {'mode':<9} {'price':>7} {'chg':>6} "
              f"{'relvol':>7} {'vwap':>6} {'+15m':>7} {'+30m':>7} {'+60m':>7} "
              f"{'best':>7} {'worst':>7}")
    print(header)
    print("-" * len(header))

    for r in rows:
        fmt = lambda v: f"{v:>7.1f}" if v is not None else f"{'-':>7}"
        print(f"{r['timestamp'][11:16]:<6} {r['symbol']:<7} {r['mode']:<9} "
              f"{r['price']:>7.2f} {r['pct_change_from_prior_close']:>5.0f}% "
              f"{r['rel_volume']:>7.1f} "
              f"{'above' if r['above_vwap'] else 'below':>6} "
              f"{fmt(r['pct_15m'])} {fmt(r['pct_30m'])} {fmt(r['pct_60m'])} "
              f"{fmt(r['mfe'])} {fmt(r['mae'])}")

    resolved = [r for r in rows if r["pct_30m"] is not None]
    if resolved:
        up = sum(1 for r in resolved if r["pct_30m"] > 0)
        median = sorted(r["pct_30m"] for r in resolved)[len(resolved) // 2]
        print(f"\n  {up}/{len(resolved)} higher after 30 minutes, "
              f"median {median:+.1f}%")
        print("  This is the raw alert, before any judgement is applied.")


def annotate(rows: list[dict], day: date) -> None:
    """Record which alerts you acted on. Skipped ones are the control group."""
    DECISION_DIR.mkdir(parents=True, exist_ok=True)
    path = DECISION_DIR / f"{day.isoformat()}.jsonl"

    print("\nFor each alert: [t]raded, [w]atched but passed, [n]ot interesting, "
          "[q]uit\n")

    with path.open("w", encoding="utf-8") as out:
        for r in rows:
            print(f"  {r['timestamp'][11:16]}  {r['symbol']:<7} "
                  f"${r['price']:.2f}  {r['pct_change_from_prior_close']:+.0f}%  "
                  f"relvol {r['rel_volume']:.1f}  "
                  f"{'above' if r['above_vwap'] else 'below'} VWAP")

            answer = input("    > ").strip().lower()
            if answer.startswith("q"):
                break

            record = {"symbol": r["symbol"], "timestamp": r["timestamp"],
                      "decision": {"t": "traded", "w": "passed",
                                   "n": "ignored"}.get(answer[:1], "ignored")}

            if record["decision"] == "traded":
                record["note"] = input("    entry/exit/why: ").strip()
            elif record["decision"] == "passed":
                record["note"] = input("    why not: ").strip()

            record["pct_30m"] = r["pct_30m"]
            record["mfe"] = r["mfe"]
            record["mae"] = r["mae"]
            out.write(json.dumps(record) + "\n")

    print(f"\nSaved to {path}")


def main() -> None:
    import argparse

    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient

    p = argparse.ArgumentParser()
    p.add_argument("--date", default=date.today().isoformat())
    p.add_argument("--feed", default="iex", choices=["sip", "iex"])
    p.add_argument("--annotate", action="store_true")
    args = p.parse_args()

    day = date.fromisoformat(args.date)
    alerts = load_alerts(day)

    key, secret = load_credentials()
    client = StockHistoricalDataClient(key, secret)
    feed = DataFeed.SIP if args.feed == "sip" else DataFeed.IEX

    symbols = sorted({a["symbol"] for a in alerts})
    bars = fetch_minutes(client, symbols, day, feed)

    rows = outcomes(alerts, bars)
    show(rows)

    if args.annotate:
        annotate(rows, day)


if __name__ == "__main__":
    main()
