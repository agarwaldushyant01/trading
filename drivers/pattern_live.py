"""Trade the chart patterns, live, on five-minute bars.

    python -m drivers.pattern_live --dry-run
    python -m drivers.pattern_live

Replaces drivers/paper_live.py as the trading path. That one ran the
spike/repeat/new_high rules, which were invented for the bot rather than
taken from the trader, and lost money across every session it ran.

This one runs patterns/detect.py: falling wedge, bullish pennant, ascending
triangle and retests, with double tops and rising wedges refused, scored on
four confluences, confirmed by a close rather than a wick, with structural
stops.

WHAT IT SHARES WITH THE OLD PATH

Everything below the entry decision: PaperTrader for sizing and orders,
broker reconciliation, position persistence, the trailing exit, the 15:50
flatten, the 2% daily loss cap and the half-hourly phone report. None of that
was ever the problem and none of it changes.

WHAT IS HONESTLY UNPROVEN

The detector agrees with the trader on 53% of their logged decisions —
chance. It is expected to lose paper money until the parameter search has
enough labelled trades to fit it properly, which is why this runs on paper
and why every decision is journalled: the comparison between what the bot
took and what the trader took is the entire point of running it at all.

Five-minute bars are built here from the one-minute stream rather than
subscribed separately, because the scanner already holds the only permitted
websocket connection.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from alerts.notify import Notifier
from data.reference import load_credentials, load_refs_for
from patterns.detect import detect

ET = ZoneInfo("America/New_York")
BAR_MINUTES = 5


class FiveMinuteBuilder:
    """Aggregate one-minute bars into five-minute bars per symbol.

    The detector is drawn on five-minute charts, which is the timeframe the
    trader uses. Subscribing separately is not an option: the data plan
    allows one websocket connection and the scanner holds it.
    """

    def __init__(self) -> None:
        self.partial: dict = {}
        self.completed: dict = defaultdict(list)

    def add(self, symbol: str, when: datetime, o: float, h: float,
            l: float, c: float, v: float) -> dict | None:
        """Feed a one-minute bar. Returns a five-minute bar when one closes."""
        bucket = when.replace(minute=(when.minute // BAR_MINUTES) * BAR_MINUTES,
                              second=0, microsecond=0)
        cur = self.partial.get(symbol)

        if cur and cur["bucket"] != bucket:
            done = cur["bar"]
            self.completed[symbol].append(done)
            # A session is 78 five-minute bars; keeping a day and a half is
            # ample and bounds memory across 13,000 symbols.
            if len(self.completed[symbol]) > 120:
                self.completed[symbol] = self.completed[symbol][-120:]
            self.partial[symbol] = {"bucket": bucket, "bar": _new_bar(
                bucket, o, h, l, c, v)}
            return done

        if cur is None:
            self.partial[symbol] = {"bucket": bucket,
                                    "bar": _new_bar(bucket, o, h, l, c, v)}
            return None

        bar = cur["bar"]
        bar["h"] = max(bar["h"], h)
        bar["l"] = min(bar["l"], l)
        bar["c"] = c
        bar["v"] += v
        return None

    def history(self, symbol: str) -> list:
        """Completed bars plus the one in progress."""
        out = list(self.completed[symbol])
        cur = self.partial.get(symbol)
        if cur:
            out.append(cur["bar"])
        return out


def _new_bar(bucket: datetime, o: float, h: float, l: float,
             c: float, v: float) -> dict:
    return {"t": bucket.isoformat(), "o": o, "h": h, "l": l, "c": c, "v": v}


async def run(builder, refs, trader, notifier, key, secret, feed_name,
              min_confluences, dry_run):
    from alpaca.data.enums import DataFeed
    from alpaca.data.live import StockDataStream
    from engine.alerts import Alert

    feed = DataFeed.SIP if feed_name == "sip" else DataFeed.IEX
    stream = StockDataStream(key, secret, feed=feed)
    state = {"bars": 0, "five": 0, "signals": 0, "last_beat": None}

    async def on_bar(raw):
        symbol = raw.symbol
        if symbol not in refs:
            return
        state["bars"] += 1

        when = raw.timestamp.astimezone(ET)
        closed = builder.add(symbol, when, float(raw.open), float(raw.high),
                             float(raw.low), float(raw.close),
                             float(raw.volume))

        now = datetime.now(ET)
        if state["last_beat"] is None or \
                (now - state["last_beat"]).total_seconds() > 300:
            state["last_beat"] = now
            print(f"  [{now:%H:%M}] {state['bars']:,} bars, "
                  f"{state['five']:,} 5m, {state['signals']} signals, "
                  f"{trader.filled} entries, "
                  f"{len(trader.open_positions)} open", flush=True)

        if closed is None:
            return
        state["five"] += 1

        bars = builder.history(symbol)
        if len(bars) < 20:
            return

        ref = refs[symbol]
        setups = [s for s in detect(bars, daily=None, levels=[],
                                    min_confluences=min_confluences)
                  if not s.rejected]
        if not setups:
            return

        setup = setups[-1]
        if setup.index < len(bars) - 2:
            return                      # stale: fired on an earlier bar

        state["signals"] += 1
        print(f"  {now:%H:%M}  SIGNAL {symbol} {setup.kind} {setup.grade} "
              f"@ {setup.entry:.4f} stop {setup.stop:.4f}", flush=True)

        session_volume = sum(b["v"] for b in bars)
        alert = Alert(
            symbol=symbol,
            pct_change=((setup.entry / ref.prior_close - 1) * 100
                        if ref.prior_close else 0.0),
            price=setup.entry,
            volume_1m=bars[-1]["v"], volume_2m=0.0, volume_5m=bars[-1]["v"],
            volume_1d=session_volume,
            float_shares=ref.shares_outstanding or None,
            alert_count=1,
            tags=[setup.kind, setup.grade],
            received_at=now,
        )
        # The structural stop comes from the pattern, not from a percentage.
        trader.consider_with_stop(alert, setup.stop, setup.kind,
                                  ", ".join(setup.confluences.detail))

    stream.subscribe_bars(on_bar, "*")
    asyncio.create_task(trader.monitor())
    print("Listening.\n", flush=True)

    attempt = 0
    while True:
        try:
            await stream._run_forever()
        except asyncio.CancelledError:
            break
        except Exception as exc:                          # noqa: BLE001
            attempt += 1
            print(f"  [{datetime.now(ET):%H:%M}] stream error ({exc}); "
                  f"reconnect #{attempt}", file=sys.stderr, flush=True)
            if attempt in (3, 10, 30):
                notifier.send("Scanner reconnecting",
                              f"Stream dropped {attempt} times.",
                              priority="high")
        try:
            await stream.stop_ws()
        except Exception:                                 # noqa: BLE001
            pass
        await asyncio.sleep(min(5 * attempt, 60) or 5)
        stream = StockDataStream(key, secret, feed=feed)
        stream.subscribe_bars(on_bar, "*")


def main() -> None:
    import argparse

    from alpaca.trading.client import TradingClient

    from engine.paper import PaperTrader

    p = argparse.ArgumentParser()
    p.add_argument("--feed", default="sip", choices=["sip", "iex"])
    p.add_argument("--rules-config", default="config/rules.yaml")
    p.add_argument("--min-confluences", type=int, default=2)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    rules_cfg = yaml.safe_load(pathlib.Path(args.rules_config).read_text())
    alert_path = pathlib.Path("config/alerts.yaml")
    alert_cfg = yaml.safe_load(alert_path.read_text()) if alert_path.exists() else {}

    refs = load_refs_for(None)
    key, secret = load_credentials()
    trading = TradingClient(key, secret, paper=True)
    notifier = Notifier(alert_cfg.get("alerts", {}))
    trader = PaperTrader(trading, rules_cfg, notifier, dry_run=args.dry_run)

    adopted = trader.reconcile()
    account = trading.get_account()

    print(f"\nPaper equity   ${float(account.equity):,.0f}")
    print(f"Feed           {args.feed.upper()}")
    print(f"Universe       {len(refs):,} symbols")
    print(f"Strategy       chart patterns on {BAR_MINUTES}-minute bars")
    print(f"Confluences    {args.min_confluences} minimum")
    print(f"Mode           {'DRY RUN' if args.dry_run else 'PAPER ORDERS'}")
    if adopted:
        print(f"Adopted        {adopted} existing position(s)")
    print()

    notifier.send("Pattern trader started",
                  f"{len(refs):,} symbols, {BAR_MINUTES}-minute patterns, "
                  f"{args.min_confluences}+ confluences.", priority="low")

    builder = FiveMinuteBuilder()
    try:
        asyncio.run(run(builder, refs, trader, notifier, key, secret,
                        args.feed, args.min_confluences, args.dry_run))
    except KeyboardInterrupt:
        pass
    finally:
        print(f"\nStopped. {trader.filled} entries.")
        os._exit(0)


if __name__ == "__main__":
    main()
