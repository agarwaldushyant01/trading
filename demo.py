#!/usr/bin/env python3
"""Runnable demo: scanner -> sizer, end to end.

    python demo.py

Uses synthetic bars so it runs with no credentials and no network. The point
is to show the wiring you will reuse with real data: bars go in, candidates
come out, each candidate gets sized, and rejects are logged with a reason.

Replace `synthetic_bars()` with a real feed and nothing else changes.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from risk.sizing import RiskConfig, RiskManager
from scanner.scanner import Bar, Scanner, TickerRef

CONFIG = pathlib.Path(__file__).parent / "config" / "scanner.yaml"


def reference_data() -> dict[str, TickerRef]:
    """Static per-symbol data, normally refreshed once daily before the open.

    In production this comes from Alpaca's assets endpoint plus a daily bars
    pull. Hardcoded here so the demo runs offline.
    """
    return {
        # symbol   exchange  shares_out   avg_20d_vol  prior_close high  atr
        "LOWF": TickerRef("LOWF", "NASDAQ", 8_000_000, 1_200_000, 2.00, 2.60, 0.22),
        "MICR": TickerRef("MICR", "NASDAQ", 4_000_000, 220_000, 0.55, 0.72, 0.06),
        "THIN": TickerRef("THIN", "NASDAQ", 6_000_000, 90_000, 1.50, 1.80, 0.15),
        "BIGC": TickerRef("BIGC", "NYSE", 900_000_000, 40_000_000, 55.00, 58.0, 1.10),
        "PINK": TickerRef("PINK", "OTC", 5_000_000, 800_000, 1.10, 1.40, 0.12),
    }


def synthetic_bars():
    """Three symbols that should fire, one that should not.

    LOWF: quiet, then a vertical premarket move  -> velocity hit
    THIN: same move but too illiquid             -> filtered by universe
    BIGC: large cap drifting                     -> filtered by universe
    PINK: OTC name ramping                       -> filtered by exchange
    """
    day = datetime(2026, 3, 10)
    start = day.replace(hour=7, minute=0)

    # LOWF: five quiet bars, then +45% in one minute, then continuation
    prices = [2.00, 2.01, 2.00, 2.02, 2.01, 2.92, 3.05, 3.40, 3.20, 3.60]
    vols = [40_000] * 5 + [420_000, 300_000, 260_000, 180_000, 220_000]
    for i, (p, v) in enumerate(zip(prices, vols)):
        low = prices[i - 1] if i and p > prices[i - 1] else p
        yield Bar("LOWF", start + timedelta(minutes=i), p, p, low, p, v)

    # MICR: fires, but 250k ADV means 1% of ADV caps the position
    mprices = [0.55, 0.56, 0.55, 0.80, 0.83]
    mvols = [30_000, 30_000, 30_000, 180_000, 120_000]
    for i, (p, v) in enumerate(zip(mprices, mvols)):
        low = mprices[i - 1] if i and p > mprices[i - 1] else p
        yield Bar("MICR", start + timedelta(minutes=i), p, p, low, p, v)

    # THIN: identical shape, but the name trades 90k a day
    for i, (p, v) in enumerate(zip(prices, vols)):
        low = prices[i - 1] if i and p > prices[i - 1] else p
        yield Bar("THIN", start + timedelta(minutes=i), p, p, low, p, v // 10)

    # BIGC: a large cap doing nothing interesting
    for i in range(10):
        p = 55.0 + i * 0.1
        yield Bar("BIGC", start + timedelta(minutes=i), p, p, p - 0.1, p, 900_000)

    # PINK: an OTC name doubling — must never appear
    for i in range(10):
        p = 1.10 * (1 + i * 0.12)
        yield Bar("PINK", start + timedelta(minutes=i), p, p, p * 0.9, p, 500_000)


def main() -> None:
    cfg = yaml.safe_load(CONFIG.read_text())
    refs = reference_data()

    scanner = Scanner(cfg, refs)
    risk = RiskManager(RiskConfig(equity=50_000))
    risk.start_session()

    print(f"\nEquity ${risk.cfg.equity:,.0f}   "
          f"risk/trade ${risk.cfg.equity * risk.cfg.risk_per_trade_pct:,.0f}   "
          f"daily cap ${risk.daily_loss_limit:,.0f}\n")
    print(f"{'time':<6} {'sym':<5} {'mode':<9} {'price':>7} {'chg%':>7} "
          f"{'shares':>7} {'notional':>10} {'risk$':>7}  note")
    print("-" * 78)

    hits = bars = 0
    for bar in synthetic_bars():
        bars += 1
        candidate = scanner.on_bar(bar)
        if candidate is None:
            continue
        hits += 1

        ref = refs[candidate.symbol]
        # Stop placement is the strategy's job; this stands in for it.
        stop = candidate.price * 0.90

        sized = risk.size(
            entry_price=candidate.price,
            stop_price=stop,
            atr=ref.atr_14,
            avg_20d_volume=ref.avg_20d_volume,
        )

        t = candidate.timestamp.strftime("%H:%M")
        if sized.allowed:
            risk.record_fill()
            print(f"{t:<6} {candidate.symbol:<5} {candidate.mode.value:<9} "
                  f"{candidate.price:>7.2f} "
                  f"{candidate.pct_change_from_prior_close:>6.1f}% "
                  f"{sized.shares:>7,} {sized.notional:>10,.0f} "
                  f"{sized.risk_dollars:>7.0f}  capped by {sized.binding_cap}")
        else:
            print(f"{t:<6} {candidate.symbol:<5} {candidate.mode.value:<9} "
                  f"{candidate.price:>7.2f} "
                  f"{candidate.pct_change_from_prior_close:>6.1f}% "
                  f"{'—':>7} {'—':>10} {'—':>7}  SKIPPED: {sized.reject.value}")

    print("-" * 78)
    print(f"{hits} candidate(s) from {bars} bars across {len(refs)} symbols. "
          f"{risk.open_positions} position(s) opened.\n")

    print("Filtered out entirely (never reached sizing):")
    print("  THIN  90k avg volume, below the 200k universe floor")
    print("  BIGC  900M shares outstanding, above the 30M cap")
    print("  PINK  OTC exchange\n")


if __name__ == "__main__":
    main()
