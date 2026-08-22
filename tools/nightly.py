"""Nightly study. Runs after the close, decides nothing.

    python -m tools.nightly              # today
    python -m tools.nightly --date ...   # a specific session

Replays every candidate the rules produced today against a fixed grid of
configurations, appends the results to data/study/history.jsonl, and pushes a
short summary to your phone.

Two rules govern this file:

  THE GRID IS FIXED. Defined once, below, and not changed to chase results.
  A grid that grows whenever something looks promising is a search for noise,
  and it will always find some.

  IT NEVER EDITS CONFIG. It says what the evidence shows and what it would
  take to act on it. Adoption stays human — not because a human decides
  better, but because the decision needs a sample size and an out-of-sample
  confirmation, and no amount of automation manufactures either.

The bar for calling anything real is deliberately high: 200 trades in a
configuration, positive expectancy, and the improvement larger than two
standard errors. Most weeks the answer will be "keep collecting", and that
answer is correct.
"""

from __future__ import annotations

import json
import math
import pathlib
import sys
from collections import defaultdict
from datetime import date, datetime
from statistics import mean, stdev
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.exit_sweep import bars_for, collect, simulate

ET = ZoneInfo("America/New_York")
HISTORY = pathlib.Path("data/study/history.jsonl")

# The fixed grid. Adding cells because one looks promising is how a study
# becomes a fishing expedition — the more configurations tested, the more
# likely one looks good by chance alone.
GRID = [
    {"name": "current",        "stop": 12.0, "target": 25.0, "max_pct": None},
    {"name": "tight",          "stop": 8.0,  "target": 15.0, "max_pct": None},
    {"name": "tightest",       "stop": 5.0,  "target": 10.0, "max_pct": None},
    {"name": "tight_small",    "stop": 8.0,  "target": 15.0, "max_pct": 20.0},
    {"name": "tightest_small", "stop": 5.0,  "target": 10.0, "max_pct": 20.0},
    {"name": "tight_tiny",     "stop": 8.0,  "target": 15.0, "max_pct": 12.0},
]

MIN_TRADES = 200          # before any configuration is called real
SIGNIFICANCE = 2.0        # standard errors above the incumbent


def features_for(symbol: str, at: str) -> dict:
    """Feature values recorded when the candidate fired."""
    for name in ("approvals.jsonl", "trades.jsonl"):
        path = pathlib.Path(f"data/mosquito/{name}")
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if not line.strip() or symbol not in line:
                continue
            r = json.loads(line)
            if r.get("symbol") == symbol and r.get("at", "")[:16] == at[:16]:
                return r
    return {}


def study(day: date) -> dict:
    """Replay the day against every configuration in the grid."""
    candidates = collect(day.isoformat())
    rows = []
    for c in candidates:
        bars = bars_for(c)
        if len(bars) > 3:
            f = features_for(c.symbol, c.at.isoformat())
            rows.append((c, bars, f.get("pct_change")))

    if not rows:
        return {"date": day.isoformat(), "candidates": 0, "results": {}}

    results = {}
    for cfg in GRID:
        outcomes = []
        for c, bars, pct in rows:
            if cfg["max_pct"] is not None:
                if pct is None or pct > cfg["max_pct"]:
                    continue          # filtered out by this configuration
            out = simulate(bars, c.entry, cfg["stop"], cfg["target"], None)
            if out:
                outcomes.append(out[1])
        if outcomes:
            results[cfg["name"]] = {
                "trades": len(outcomes),
                "avg": round(mean(outcomes), 3),
                "wins": sum(1 for x in outcomes if x > 0),
                "returns": [round(x, 2) for x in outcomes],
            }

    return {"date": day.isoformat(), "candidates": len(rows),
            "results": results}


def cumulative() -> dict:
    """Every session's results pooled, per configuration."""
    if not HISTORY.exists():
        return {}
    pooled = defaultdict(list)
    for line in HISTORY.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        for name, r in row.get("results", {}).items():
            pooled[name].extend(r.get("returns", []))
    return dict(pooled)


def verdict(pooled: dict) -> tuple[str, list[str]]:
    """What the accumulated evidence supports. Deliberately conservative."""
    lines = []
    if not pooled:
        return ("collecting", ["No history yet."])

    stats = {}
    for name, returns in pooled.items():
        if len(returns) < 10:
            continue
        avg = mean(returns)
        se = (stdev(returns) / math.sqrt(len(returns))) if len(returns) > 1 else 0
        stats[name] = (avg, se, len(returns))

    if not stats:
        return ("collecting", ["Too few trades to say anything."])

    for name, (avg, se, n) in sorted(stats.items(), key=lambda kv: -kv[1][0]):
        wins = sum(1 for r in pooled[name] if r > 0)
        lines.append(f"  {name:<16} {n:>5} trades  {avg:>+7.2f}% "
                     f"±{se:.2f}  {wins / n * 100:>3.0f}% win")

    best = max(stats.items(), key=lambda kv: kv[1][0])
    name, (avg, se, n) = best
    current = stats.get("current", (0, 0, 0))

    if avg <= 0:
        return ("no_edge", lines + [
            "", f"  Nothing is profitable yet. Best is {name} at {avg:+.2f}%.",
            "  Keep collecting — or accept there may be no edge here."])

    if n < MIN_TRADES:
        return ("promising", lines + [
            "", f"  {name} is positive ({avg:+.2f}%) but only {n} trades.",
            f"  Need {MIN_TRADES} before this means anything. "
            f"About {(MIN_TRADES - n) // 75 + 1} more sessions."])

    gap = avg - current[0]
    if gap < SIGNIFICANCE * se:
        return ("inconclusive", lines + [
            "", f"  {name} leads but not by enough ({gap:+.2f} vs "
            f"±{se:.2f} noise).", "  Keep collecting."])

    return ("actionable", lines + [
        "", f"  {name} beats current by {gap:+.2f}% over {n} trades.",
        f"  That clears the bar. Worth adopting — then confirm it forward",
        f"  on sessions it was not chosen from."])


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--date", default=date.today().isoformat())
    p.add_argument("--quiet", action="store_true", help="no phone push")
    args = p.parse_args()

    day = date.fromisoformat(args.date)
    today = study(day)

    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if HISTORY.exists():
        existing = [l for l in HISTORY.read_text().splitlines()
                    if l.strip() and json.loads(l)["date"] != day.isoformat()]
    existing.append(json.dumps(today))
    HISTORY.write_text("\n".join(existing) + "\n")

    print(f"\n{'=' * 62}")
    print(f"  NIGHTLY STUDY — {day}")
    print(f"{'=' * 62}\n")
    print(f"  {today['candidates']} candidates today\n")

    if today["results"]:
        for name, r in today["results"].items():
            print(f"  {name:<16} {r['trades']:>4} trades  {r['avg']:>+7.2f}%  "
                  f"{r['wins']}/{r['trades']} won")

    pooled = cumulative()
    state, lines = verdict(pooled)

    print(f"\n  ALL SESSIONS POOLED\n")
    for line in lines:
        print(line)
    print()

    if not args.quiet:
        try:
            import yaml
            from alerts.notify import Notifier
            cfg_path = pathlib.Path("config/alerts.yaml")
            cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
            notifier = Notifier(cfg.get("alerts", {}))

            total = sum(len(v) for v in pooled.values()) // max(len(pooled), 1)
            headline = {
                "collecting": "Still collecting",
                "no_edge": "Nothing profitable yet",
                "promising": "Something positive, too few trades",
                "inconclusive": "Leader not yet significant",
                "actionable": "A change clears the bar",
            }[state]

            notifier.send(
                f"Study: {headline}",
                f"{today['candidates']} candidates today, ~{total} pooled.\n"
                + ("Worth 10 minutes when you have them."
                   if state == "actionable" else "Nothing to do."),
                priority="default" if state != "actionable" else "high",
            )
        except Exception:                                 # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
