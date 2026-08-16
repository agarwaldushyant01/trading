"""What happened after the scanner fired? — Phase 3 analysis.

    python -m tools.analyze

Reads every Parquet file in data/candidates/ and reports forward returns,
hit rates and excursions, sliced by the things that might carry an edge:
scan mode, session, time of day, and how many times a name has appeared.

Reading the output:

  MAE percentiles set your stops. If the 25th percentile of MAE is -8%, then
  a quarter of all candidates dipped at least 8% before doing anything else.
  A 5% stop would have removed those trades regardless of how they ended.

  Hit rate alone means nothing. A 35% hit rate with 3:1 winners beats a 65%
  hit rate with 1:3. Expectancy is the number that matters.

  Median, not mean. One 400% runner drags a mean upward and tells you
  nothing about the trade you will actually take tomorrow.

Every segment here is a hypothesis, not a conclusion. A segment that looks
good across five days of data is noise. Come back when you have months.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

CANDIDATE_DIR = pathlib.Path("data/candidates")
MIN_SEGMENT = 20          # below this, do not report — it is noise


def load() -> "pd.DataFrame":
    import pandas as pd

    files = sorted(CANDIDATE_DIR.glob("*.parquet"))
    if not files:
        raise SystemExit(
            "No candidates. Run the replay first:\n"
            "  python -m drivers.replay --start ... --end ... --feed sip"
        )
    frame = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    frame["date"] = frame["timestamp"].dt.date
    frame["hour"] = frame["timestamp"].dt.hour
    return frame


def summarize(frame, label: str) -> dict | None:
    """One row of statistics for a slice of candidates."""
    if len(frame) < MIN_SEGMENT:
        return None

    complete = frame[frame["fwd_30m_pct"].notna()]
    if len(complete) < MIN_SEGMENT:
        return None

    returns = complete["fwd_30m_pct"]
    wins = returns[returns > 0]
    losses = returns[returns <= 0]
    hit_rate = len(wins) / len(returns)

    # Expectancy per trade, ignoring stops and targets: what the raw signal
    # is worth before any strategy logic is applied to it.
    expectancy = hit_rate * wins.mean() + (1 - hit_rate) * (
        losses.mean() if len(losses) else 0
    )

    return {
        "segment": label,
        "n": len(complete),
        "hit_30m": round(hit_rate * 100, 1),
        "med_5m": round(complete["fwd_5m_pct"].median(), 2),
        "med_30m": round(returns.median(), 2),
        "med_60m": round(complete["fwd_60m_pct"].median(), 2)
        if complete["fwd_60m_pct"].notna().any() else None,
        "expectancy": round(expectancy, 2),
        "mae_p25": round(complete["mae_pct"].quantile(0.25), 1),
        "mae_med": round(complete["mae_pct"].median(), 1),
        "mfe_med": round(complete["mfe_pct"].median(), 1),
    }


def table(rows: list[dict], title: str) -> None:
    rows = [r for r in rows if r]
    if not rows:
        print(f"\n{title}\n  (no segment had enough data)")
        return

    print(f"\n{title}")
    header = (f"{'segment':<22} {'n':>5} {'hit%':>6} {'5m':>7} {'30m':>7} "
              f"{'60m':>7} {'exp':>7} {'MAE p25':>8} {'MAE med':>8} {'MFE med':>8}")
    print(header)
    print("-" * len(header))
    for r in rows:
        sixty = f"{r['med_60m']:>7.2f}" if r["med_60m"] is not None else f"{'—':>7}"
        print(f"{r['segment']:<22} {r['n']:>5} {r['hit_30m']:>6.1f} "
              f"{r['med_5m']:>7.2f} {r['med_30m']:>7.2f} {sixty} "
              f"{r['expectancy']:>7.2f} {r['mae_p25']:>8.1f} "
              f"{r['mae_med']:>8.1f} {r['mfe_med']:>8.1f}")


def main() -> None:
    frame = load()

    days = frame["date"].nunique()
    print(f"\n{len(frame):,} candidates across {days} sessions "
          f"({frame['date'].min()} to {frame['date'].max()})")
    print(f"{len(frame) / days:.0f} per session, "
          f"{frame['symbol'].nunique()} distinct symbols")

    incomplete = frame["fwd_30m_pct"].isna().sum()
    if incomplete:
        print(f"{incomplete} fired too late for a 30-minute forward return "
              f"and are excluded from the tables below")

    table([summarize(frame, "ALL CANDIDATES")], "OVERALL")

    table(
        [summarize(g, f"mode: {name}") for name, g in frame.groupby("mode")],
        "BY SCAN MODE — is the vertical spike different from the gap?",
    )

    table(
        [summarize(g, f"session: {name}") for name, g in frame.groupby("session")],
        "BY SESSION — premarket setups behave differently from intraday ones",
    )

    table(
        [summarize(g, f"{h:02d}:00") for h, g in frame.groupby("hour")],
        "BY HOUR — where in the day do candidates cluster?",
    )

    frame["appearances"] = frame["appearances_10d"].clip(upper=4)
    table(
        [summarize(g, f"seen {int(n)}x in 10d") for n, g in
         frame.groupby("appearances")],
        "BY APPEARANCE COUNT — does your 'seen it 2-3 times' filter work?",
    )

    above = frame[frame["above_vwap"]]
    below = frame[~frame["above_vwap"]]
    table(
        [summarize(above, "above VWAP"), summarize(below, "below VWAP")],
        "BY VWAP POSITION — the core of your reclaim setup",
    )

    print("\nMAE DISTRIBUTION — how far candidates went against you first")
    complete = frame[frame["mae_pct"].notna()]
    print("  " + "  ".join(
        f"p{int(q * 100)}: {complete['mae_pct'].quantile(q):>6.1f}%"
        for q in (0.10, 0.25, 0.50, 0.75, 0.90)
    ))
    print("\n  A stop tighter than the p25 figure removes at least a quarter of")
    print("  all candidates before they have a chance to work.")

    print("\nMFE DISTRIBUTION — how far they ran in your favour")
    print("  " + "  ".join(
        f"p{int(q * 100)}: {complete['mfe_pct'].quantile(q):>6.1f}%"
        for q in (0.10, 0.25, 0.50, 0.75, 0.90)
    ))

    print(f"\n{days} sessions is far too little to conclude anything. These")
    print("numbers show the pipeline works, not that a setup does.\n")


if __name__ == "__main__":
    main()
