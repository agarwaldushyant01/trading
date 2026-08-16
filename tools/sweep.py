"""Sweep strategy parameters against the cached bars.

    python -m tools.sweep --split 2026-01-01

Runs every combination below and reports each one twice: on the data before
the split date, and on the data after. Seconds per combination, because the
bars are already on disk.

Why the split matters more than the sweep:

  Trying 30 parameter sets against six months of data and keeping the best
  one is not research, it is curve fitting. Some combination always looks
  good on any fixed sample. The only question that counts is whether the
  winner on the first period is still a winner on a period it never saw.

  If the top in-sample combination collapses out-of-sample, the setup does
  not work and no amount of further tuning will change that. Read the
  out-of-sample column first; treat the in-sample column as a candidate
  list, not a result.
"""

from __future__ import annotations

import copy
import itertools
import pathlib
import sys
from datetime import date

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backtest.engine import Engine, summarize
from data.reference import load_refs_for
from risk.sizing import RiskConfig, RiskManager
from scanner.scanner import Bar, Scanner
from strategies.bounce import Bounce
from strategies.vwap_reclaim import VwapReclaim
from tools.backtest import BAR_CACHE, run_day

STRATEGIES = {"vwap_reclaim": VwapReclaim, "bounce": Bounce}
NEEDS_HISTORY = {"bounce"}

GRIDS = {
    "vwap_reclaim": {
        "stop_mode": ["bar_low", "vwap", "pct"],
        "stop_pct": [8.0, 12.0],
        "exit_on_vwap_loss": [True, False],
        "target_pct": [10.0, 15.0, 25.0],
    },
    "bounce": {
        # Stop placement sank the reclaim, and 7% sits near the median
        # adverse excursion, so this is the axis most likely to matter.
        "stop_mode": ["bar_low", "session_low", "pct"],
        "stop_pct": [7.0, 10.0, 14.0],
        # Only 5 of 109 targets were hit at 25%. Test whether taking less
        # more often beats waiting for a level that rarely arrives.
        "target_pct": [10.0, 15.0, 25.0],
        "min_decline_pct": [40.0, 60.0],
    },
}


def load_day(day: date) -> dict[str, list[Bar]]:
    import pandas as pd

    path = BAR_CACHE / f"{day.isoformat()}.parquet"
    if not path.exists():
        return {}
    frame = pd.read_parquet(path)
    out: dict[str, list[Bar]] = {}
    for row in frame.itertuples(index=False):
        out.setdefault(row.symbol, []).append(
            Bar(row.symbol, row.timestamp, row.open, row.high, row.low,
                row.close, int(row.volume), row.vwap)
        )
    return out


def run(days, name, scanner_cfg, strategy_cfg, refs, history, equity=50_000):
    scanner = Scanner(scanner_cfg, refs)
    strategy = (STRATEGIES[name](strategy_cfg, refs, history)
                if name in NEEDS_HISTORY else STRATEGIES[name](strategy_cfg, refs))
    risk = RiskManager(RiskConfig(equity=equity))
    engine = Engine(risk, strategy_cfg["engine"])

    for day, minutes in days:
        risk.start_session()
        if minutes:
            run_day(minutes, scanner, strategy, engine)
    return summarize(engine.closed)


def combinations(grid: dict):
    """stop_pct only means anything under stop_mode=pct, so collapse the
    duplicate runs rather than reporting one result under three labels."""
    keys = list(grid)
    seen = set()
    for values in itertools.product(*(grid[k] for k in keys)):
        params = dict(zip(keys, values))
        effective = dict(params)
        if params.get("stop_mode") != "pct" and "stop_pct" in effective:
            effective["stop_pct"] = None
        key = tuple(sorted(effective.items()))
        if key not in seen:
            seen.add(key)
            yield params


def label(params: dict) -> str:
    parts = []
    for key, value in params.items():
        if key == "stop_pct" and params.get("stop_mode") != "pct":
            continue
        parts.append(f"{key.replace('_pct', '').replace('_mode', '')}={value}")
    return " ".join(parts)


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--strategy", default="bounce", choices=list(STRATEGIES))
    p.add_argument("--split", required=True,
                   help="dates before this are in-sample, after are held out")
    args = p.parse_args()

    scanner_cfg = yaml.safe_load(pathlib.Path("config/scanner.yaml").read_text())
    base_cfg = yaml.safe_load(pathlib.Path("config/strategies.yaml").read_text())
    refs = load_refs_for(None)

    history = None
    if args.strategy in NEEDS_HISTORY:
        from data.daily_history import History
        history = History.load()

    files = sorted(BAR_CACHE.glob("*.parquet"))
    if not files:
        raise SystemExit("No cached bars. Run tools.backtest first.")

    split = date.fromisoformat(args.split)
    all_days = [(date.fromisoformat(f.stem), load_day(date.fromisoformat(f.stem)))
                for f in files]
    train = [d for d in all_days if d[0] < split]
    test = [d for d in all_days if d[0] >= split]

    print(f"\nin-sample:      {len(train)} sessions before {split}")
    print(f"out-of-sample:  {len(test)} sessions from {split}\n")

    header = (f"{'parameters':<50} {'IS n':>5} {'IS R':>7} "
              f"{'OOS n':>6} {'OOS R':>7} {'+/-':>6}")
    print(header)
    print("-" * len(header))

    results = []
    for params in combinations(GRIDS[args.strategy]):
        cfg = copy.deepcopy(base_cfg)
        cfg[args.strategy].update(params)

        is_stats = run(train, args.strategy, scanner_cfg, cfg, refs, history)
        oos_stats = run(test, args.strategy, scanner_cfg, cfg, refs, history)
        results.append((params, is_stats, oos_stats))

        is_r = is_stats.get("expectancy_r") or 0
        oos_r = oos_stats.get("expectancy_r") or 0
        se = oos_stats.get("stderr_r")
        print(f"{label(params):<50} {is_stats.get('trades', 0):>5} {is_r:>7.3f} "
              f"{oos_stats.get('trades', 0):>6} {oos_r:>7.3f} "
              f"{se if se is not None else 0:>6.3f}")

    ranked = sorted(
        results,
        key=lambda r: r[1].get("expectancy_r") or -99,
        reverse=True,
    )
    best_params, best_is, best_oos = ranked[0]
    oos_r = best_oos.get("expectancy_r") or 0
    se = best_oos.get("stderr_r") or 0

    print(f"\nBest in-sample: {label(best_params)}")
    print(f"  in-sample R      {best_is.get('expectancy_r')} "
          f"on {best_is.get('trades', 0)} trades")
    print(f"  out-of-sample R  {oos_r} +/- {se} "
          f"on {best_oos.get('trades', 0)} trades")

    if se and abs(oos_r) < 2 * se:
        print("\n  Inside the noise band: not a pass and not a rejection. At")
        print("  this sample size the answer is more history, not more")
        print("  tuning.\n")
    elif oos_r < 0:
        print("\n  The in-sample winner loses out-of-sample. That is a setup")
        print("  that does not work, not parameters that need tuning.\n")
    else:
        print("\n  Holds up out-of-sample and clears the noise band. Worth")
        print("  carrying forward — on one split, on one period.\n")


if __name__ == "__main__":
    main()
