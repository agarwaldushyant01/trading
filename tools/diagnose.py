"""Why did nothing trigger? — diagnostic for a single replay day.

    python -m tools.diagnose --date 2026-03-02 --feed iex

Replays one day, but instead of reporting hits it reports the maximum value
each scan condition reached across the day. If the highest relative volume
anywhere was 0.08 against a threshold of 3.0, that is the answer, and no
amount of staring at the scanner code would have found it faster.

Also compares the daily bar's volume against the sum of that day's minute
bars. Those should agree closely. A large gap means the reference data and
the replay data came from different feeds, which silently breaks every
volume-based threshold.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from data.reference import load_credentials, load_refs_for
from drivers.replay import fetch_daily, fetch_minutes, prescreen


def analyse(symbol: str, bars, ref, cfg: dict) -> dict:
    """Highest value each scan input reached during the day."""
    session_volume = 0
    high_of_day = 0.0
    max_rel_volume = 0.0
    max_pct_change = -999.0
    max_velocity = 0.0
    max_minute_volume = 0

    window = []
    for bar in bars:
        session_volume += bar.volume
        high_of_day = max(high_of_day, bar.high)
        max_minute_volume = max(max_minute_volume, bar.volume)

        if ref.avg_20d_volume:
            max_rel_volume = max(max_rel_volume, session_volume / ref.avg_20d_volume)
        if ref.prior_close:
            max_pct_change = max(
                max_pct_change, (bar.close / ref.prior_close - 1) * 100
            )

        window.append(bar)
        window = window[-1:]                     # 60-second velocity window
        low = min(b.low for b in window)
        if low:
            max_velocity = max(max_velocity, (bar.close / low - 1) * 100)

    return {
        "symbol": symbol,
        "bars": len(bars),
        "minute_volume_sum": session_volume,
        "avg_20d_volume": int(ref.avg_20d_volume),
        "max_rel_volume": round(max_rel_volume, 3),
        "max_pct_change": round(max_pct_change, 1),
        "max_velocity_pct": round(max_velocity, 1),
        "max_minute_volume": max_minute_volume,
    }


def report(rows: list[dict], daily: dict, cfg: dict) -> None:
    if not rows:
        print("No minute bars returned at all.")
        return

    g, v = cfg["gap"], cfg["velocity"]

    print(f"\n{len(rows)} symbols analysed. Thresholds vs best achieved:\n")
    print(f"{'condition':<28} {'threshold':>12} {'best seen':>12}   blocking?")
    print("-" * 70)

    checks = [
        ("gap: % change", g["min_pct_change"], max(r["max_pct_change"] for r in rows)),
        ("gap: relative volume", g["min_rel_volume"],
         max(r["max_rel_volume"] for r in rows)),
        ("gap: session volume", g["min_session_volume"],
         max(r["minute_volume_sum"] for r in rows)),
        ("velocity: % in window", v["min_pct_change"],
         max(r["max_velocity_pct"] for r in rows)),
        ("velocity: window volume", v["min_window_volume"],
         max(r["max_minute_volume"] for r in rows)),
        ("velocity: cumulative volume", v["min_cumulative_volume"],
         max(r["minute_volume_sum"] for r in rows)),
    ]
    for label, threshold, best in checks:
        blocked = "  <-- BLOCKS" if best < threshold else ""
        print(f"{label:<28} {threshold:>12,.1f} {best:>12,.1f}{blocked}")

    print(f"\nFeed consistency — daily bar volume vs sum of minute bars:\n")
    print(f"{'symbol':<8} {'daily bar':>14} {'minutes':>14} {'ratio':>8}")
    print("-" * 48)
    for r in sorted(rows, key=lambda x: -x["minute_volume_sum"])[:8]:
        day_volume = daily.get(r["symbol"], {}).get("volume", 0)
        ratio = r["minute_volume_sum"] / day_volume if day_volume else 0
        print(f"{r['symbol']:<8} {day_volume:>14,} "
              f"{r['minute_volume_sum']:>14,} {ratio:>8.2f}")
    print("\nRatios well under 1.00 mean the minute bars cover less volume than")
    print("the daily bar — different feeds, and every volume threshold is wrong.")

    print(f"\nTop movers by intraday velocity:\n")
    print(f"{'symbol':<8} {'%chg':>8} {'velocity':>10} {'rel vol':>9} {'minutes':>9}")
    print("-" * 48)
    for r in sorted(rows, key=lambda x: -x["max_velocity_pct"])[:8]:
        print(f"{r['symbol']:<8} {r['max_pct_change']:>8.1f} "
              f"{r['max_velocity_pct']:>10.1f} {r['max_rel_volume']:>9.2f} "
              f"{r['bars']:>9}")


def main() -> None:
    import argparse

    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient

    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True)
    p.add_argument("--feed", default="iex", choices=["sip", "iex"])
    p.add_argument("--config", default="config/scanner.yaml")
    p.add_argument("--limit", type=int, default=40,
                   help="how many pre-screened symbols to pull minutes for")
    args = p.parse_args()

    cfg = yaml.safe_load(pathlib.Path(args.config).read_text())
    refs = load_refs_for(None)
    feed = DataFeed.SIP if args.feed == "sip" else DataFeed.IEX

    key, secret = load_credentials()
    client = StockHistoricalDataClient(key, secret)
    day = date.fromisoformat(args.date)

    print(f"Fetching daily bars for {len(refs)} symbols...", file=sys.stderr)
    daily = fetch_daily(client, sorted(refs), day, feed)

    survivors = [s for s, bar in daily.items() if prescreen(bar, refs[s], cfg)]
    print(f"{len(survivors)} pre-screened; pulling minutes for the top "
          f"{args.limit} by volume", file=sys.stderr)

    survivors.sort(key=lambda s: -daily[s]["volume"])
    survivors = survivors[: args.limit]

    minutes = fetch_minutes(client, survivors, day, feed)
    rows = [analyse(s, bars, refs[s], cfg) for s, bars in minutes.items()]
    report(rows, daily, cfg)


if __name__ == "__main__":
    main()
