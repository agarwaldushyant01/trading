# Trading bot

Phase 2 of 5. Scanner and risk sizing are built and tested. There is no live
data feed and no broker connection yet, so nothing here places orders.

## Layout

```
config/scanner.yaml     every scanner threshold — tune here, not in code
scanner/scanner.py      universe filter, gap + velocity detection, dedup
risk/sizing.py          position size and account-level risk gates
tests/                  31 tests
demo.py                 runnable end-to-end example on synthetic bars
strategy-specs.md       the six setups as numbers — source of truth
```

## Run it

```bash
pip install pyyaml
python demo.py
```

No credentials, no network. Synthetic bars in, sized candidates out.

## Run the tests

```bash
pip install pytest
python -m pytest tests/ -v
```

Run these after changing any threshold. They encode intent, so a failure tells
you whether an edit did what you meant.

## The two modules

### `Scanner` — what to look at

Consumes `Bar` objects one at a time, returns a `Candidate` or `None`.

```python
scanner = Scanner(config, reference_data)
for bar in bars:
    candidate = scanner.on_bar(bar)
```

It never calls a data vendor. Whatever produces the bars decides whether
they come from a live websocket or a Parquet file — which is what lets the
same code be backtested and traded.

`Candidate` carries a wide feature snapshot (VWAP, relative volume, distance
off high of day, appearance counts, ATR). Anything not captured at trigger
time cannot be studied in Phase 3, so it over-captures on purpose.

### `RiskManager` — whether and how much

```python
risk = RiskManager(RiskConfig(equity=50_000))
risk.start_session()

sized = risk.size(entry_price=2.92, stop_price=2.63,
                  atr=0.22, avg_20d_volume=1_200_000)

if sized.allowed:
    place_order(candidate.symbol, sized.shares)   # not built yet
    risk.record_fill()
else:
    log(sized.reject.value)
```

Call `record_close(realized_pnl)` when a position exits, `end_session()` at
the close. The manager tracks the daily budget and the losing-day streak
across sessions.

No strategy computes its own share count. All sizing goes through here.

## What is missing

1. **Reference data loader** — `TickerRef` per symbol (exchange, shares
   outstanding, 20-day average volume, prior close, ATR), refreshed daily.
   The scanner cannot run without it.
2. **Replay driver** — reads historical bars, pumps them through `Scanner`,
   writes candidates to Parquet. This is what makes Phase 3 possible.
3. **Live driver** — same, from an Alpaca websocket.
4. **Strategies** — entry and exit logic per setup. `demo.py` uses a flat 10%
   stop as a placeholder; real stops come from the specs.
5. **Execution** — order placement, idempotency keys, position reconciliation.

Order matters. (1) and (2) unlock the backtest, which decides whether the
rest is worth building.
