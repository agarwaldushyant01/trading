"""Fit the detector's thresholds to the trader's own decisions.

    python -m tools.tune                 # search, report, change nothing
    python -m tools.tune --apply         # write the winner to config

Seven rounds of adjusting thresholds by hand moved agreement between 41% and
53% — chance — and taught us nothing, because roughly fifteen parameters
interact and no single change could be evaluated in isolation. This searches
the space instead.

HOW IT AVOIDS FOOLING US

  HOLDOUT. Parameters are chosen on one half of the labelled trades and
  scored on the other. A configuration that only works on the half it was
  chosen from has told us nothing, and with fifteen knobs and few examples
  that is the default outcome rather than a rare failure.

  SAMPLE FLOOR. Below about 100 labelled trades this reports the best it
  found and refuses to recommend it. Fitting fifteen parameters to 32
  examples finds noise every time, and the result would validate beautifully
  and lose money — the worst outcome available.

  BOTH ERRORS. Score counts winners found AND losers avoided. Optimising
  recall alone yields a detector that takes everything.

The search is coarse by design. A fine grid over fifteen parameters would
take days and overfit harder; the aim is to find the region the trader's
judgment lives in, not the exact point.
"""

from __future__ import annotations

import itertools
import json
import pathlib
import random
import sys
from dataclasses import dataclass
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

MIN_LABELLED = 100
CONFIG = pathlib.Path("config/patterns.yaml")


@dataclass
class Params:
    touch_tolerance: float = 2.0
    flat_tolerance: float = 3.0
    min_confluences: int = 2
    wedge_convergence: float = 0.67
    volume_expansion: float = 1.5
    retest_tolerance: float = 3.0
    trend_lookback: int = 10
    allow_low_breaks: int = 1
    swing_lookback: int = 2

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


# Coarse grid. Each axis spans the plausible range rather than sampling it
# finely: with this many parameters, resolution buys overfitting.
GRID = {
    "touch_tolerance": [1.5, 2.0, 3.0],
    "flat_tolerance": [2.0, 3.0, 4.0],
    "min_confluences": [1, 2, 3],
    "wedge_convergence": [0.5, 0.67, 0.85],
    "volume_expansion": [1.0, 1.5, 2.5],
    "retest_tolerance": [2.0, 3.0, 5.0],
    "trend_lookback": [5, 10, 20],
    "allow_low_breaks": [1, 2],
    "swing_lookback": [1, 2, 3],
}


def load_labelled() -> tuple[list, list]:
    """Trades taken (with outcomes) and names explicitly passed."""
    path = pathlib.Path("data/manual/trades.jsonl")
    if not path.exists():
        return ([], [])

    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    taken = [r for r in rows if r["kind"] == "trade" and r.get("exit")]
    passed = [r for r in rows if r["kind"] == "pass"]
    return (taken, passed)


def score(params: Params, winners: list, losers: list, passed: list,
          bars_for) -> dict:
    """Agreement with the trader under one parameter set.

    Counts winners found and losers avoided equally. A detector that finds
    every winner by taking everything scores no better than one that takes
    nothing.
    """
    from patterns import detect as detect_mod

    _apply(params)

    found = avoided = evaluated = 0

    for row in winners:
        bars, daily, levels = bars_for(row["symbol"], row["date"])
        if not bars:
            continue
        evaluated += 1
        setups = detect_mod.detect(bars, daily=daily, levels=levels,
                                   min_confluences=params.min_confluences)
        if any(not s.rejected for s in setups):
            found += 1

    for row in losers + passed:
        bars, daily, levels = bars_for(row["symbol"], row["date"])
        if not bars:
            continue
        evaluated += 1
        setups = detect_mod.detect(bars, daily=daily, levels=levels,
                                   min_confluences=params.min_confluences)
        if not any(not s.rejected for s in setups):
            avoided += 1

    total = len(winners) + len(losers) + len(passed)
    return {"found": found, "avoided": avoided, "evaluated": evaluated,
            "agreement": (found + avoided) / total if total else 0,
            "recall": found / len(winners) if winners else 0,
            "precision": avoided / (len(losers) + len(passed))
            if (losers or passed) else 0}


def _apply(params: Params) -> None:
    """Push parameters into the modules that read them.

    Done by assignment rather than by threading arguments through every call,
    because the alternative is a signature change in six places for each new
    knob — and the search needs to vary them all.
    """
    from patterns import confluence, detect, geometry

    geometry.DEFAULT_TOUCH_TOLERANCE = params.touch_tolerance
    geometry.DEFAULT_SWING_LOOKBACK = params.swing_lookback
    detect.DEFAULT_FLAT_TOLERANCE = params.flat_tolerance
    detect.DEFAULT_WEDGE_CONVERGENCE = params.wedge_convergence
    detect.DEFAULT_VOLUME_EXPANSION = params.volume_expansion
    detect.DEFAULT_RETEST_TOLERANCE = params.retest_tolerance
    detect.DEFAULT_TREND_LOOKBACK = params.trend_lookback
    detect.DEFAULT_ALLOW_LOW_BREAKS = params.allow_low_breaks


def split(rows: list, seed: int = 7) -> tuple[list, list]:
    """Split by DATE, not by row.

    Splitting rows at random would put trades from the same session on both
    sides, and sessions are correlated — the holdout would leak.
    """
    days = sorted({r["date"] for r in rows})
    rng = random.Random(seed)
    rng.shuffle(days)
    half = len(days) // 2
    fit_days = set(days[:half])
    return ([r for r in rows if r["date"] in fit_days],
            [r for r in rows if r["date"] not in fit_days])


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="write the winning parameters to config")
    p.add_argument("--max-configs", type=int, default=400)
    args = p.parse_args()

    taken, passed = load_labelled()
    winners = [r for r in taken if r["exit"] > r["entry"]]
    losers = [r for r in taken if r["exit"] <= r["entry"]]

    print(f"\n{'=' * 68}")
    print(f"  PARAMETER SEARCH")
    print(f"{'=' * 68}\n")
    print(f"  {len(winners)} winners, {len(losers)} losers, "
          f"{len(passed)} passes")

    total = len(taken)
    if total < MIN_LABELLED:
        print(f"\n  Only {total} labelled trades. The search needs "
              f"{MIN_LABELLED}+ before")
        print(f"  a fitted result means anything — with nine parameters and")
        print(f"  this few examples it would fit noise, validate well, and")
        print(f"  lose money.")
        print(f"\n  Log trades daily with:  python3 -m tools.log SYM 1.23 1.45")
        print(f"  and passes with:        python3 -m tools.log --passed SYM")
        print(f"\n  {MIN_LABELLED - total} more to go.\n")
        return

    from tools.validate import daily_bars, five_minute_bars, levels_from
    from alpaca.data.historical import StockHistoricalDataClient
    from data.reference import load_credentials

    key, secret = load_credentials()
    client = StockHistoricalDataClient(key, secret)
    cache: dict = {}

    def bars_for(symbol: str, day_s: str):
        if (symbol, day_s) in cache:
            return cache[(symbol, day_s)]
        day = date.fromisoformat(day_s)
        bars = five_minute_bars(client, symbol, day)
        daily = daily_bars(client, symbol, day)
        out = (bars, daily, levels_from(daily))
        cache[(symbol, day_s)] = out
        return out

    fit_w, test_w = split(winners)
    fit_l, test_l = split(losers)
    fit_p, test_p = split(passed)

    keys = list(GRID)
    combos = list(itertools.product(*(GRID[k] for k in keys)))
    if len(combos) > args.max_configs:
        combos = random.Random(11).sample(combos, args.max_configs)

    print(f"  Searching {len(combos)} configurations on "
          f"{len(fit_w) + len(fit_l) + len(fit_p)} fitting examples...\n")

    results = []
    for n, combo in enumerate(combos, 1):
        params = Params(**dict(zip(keys, combo)))
        s = score(params, fit_w, fit_l, fit_p, bars_for)
        results.append((s["agreement"], params, s))
        if n % 50 == 0:
            print(f"    {n}/{len(combos)}", flush=True)

    results.sort(key=lambda r: -r[0])
    best_fit, best_params, best_stats = results[0]

    print(f"\n  Best on the fitting half: {best_fit * 100:.0f}% agreement")
    for k, v in best_params.as_dict().items():
        print(f"    {k:<20} {v}")

    held = score(best_params, test_w, test_l, test_p, bars_for)
    print(f"\n  ON THE HOLDOUT (the number that counts)")
    print(f"    agreement {held['agreement'] * 100:.0f}%   "
          f"recall {held['recall'] * 100:.0f}%   "
          f"precision {held['precision'] * 100:.0f}%")

    if held["agreement"] < 0.6:
        print(f"\n  Below 60% out of sample — the fitting result was noise.")
    elif held["agreement"] < best_fit - 0.15:
        print(f"\n  Large drop from fitting to holdout: overfitted.")
    else:
        print(f"\n  Holds up. Worth applying and confirming forward.")
        if args.apply:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            import yaml
            CONFIG.write_text(yaml.safe_dump(best_params.as_dict(),
                                             sort_keys=False))
            print(f"  Written to {CONFIG}")
    print()


if __name__ == "__main__":
    main()
