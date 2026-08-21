"""Paper-trade the Alpaca scanner, using the mosquito rules engine.

    python -m drivers.paper_live --feed iex --scanner-config config/scanner-iex.yaml
    python -m drivers.paper_live --dry-run

Exists so paper trading can start now rather than waiting on a Nuntio quote.
The scanner already runs, already finds candidates, and already streams. What
was missing was the decision and execution layer — which is written, just
pointed at a Discord feed that does not exist yet.

The adapter below translates a scanner Candidate into the shape the rules
expect. When Nuntio arrives, swap the source and the rules, sizing, exits and
journal are untouched.

Two fields are worse here than they will be from Nuntio, and it matters:

  float is shares-outstanding from SEC filings — quarterly, and it
  overstates true float on exactly these names. Nuntio publishes the real
  number.

  alert_count only counts today. Nuntio's # counter runs across sessions,
  which is closer to the rule it stands in for.

So treat results from this as a rehearsal, not a verdict.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from alerts.notify import Notifier
from data.reference import load_credentials, load_refs_for
from drivers.live import backfill, refs_date_of, to_bar
from engine.alerts import Alert
from engine.approval import ApprovalQueue
from engine.paper import PaperTrader
from scanner.scanner import Scanner

ET = ZoneInfo("America/New_York")


def to_alert(candidate, ref) -> Alert:
    """Scanner Candidate -> the shape the rules engine reads.

    The derived properties (float turnover, one-minute relative volume) then
    compute exactly as they would from a real mosquito message.
    """
    tags = []
    if candidate.above_vwap:
        tags.append("AVWAP")
    if candidate.pct_off_high > -1.0:
        tags.append("NSH")               # at or very near the session high

    return Alert(
        symbol=candidate.symbol,
        pct_change=candidate.pct_change_from_prior_close,
        price=candidate.price,
        volume_1m=float(candidate.window_volume),
        volume_2m=0.0,                   # scanner keeps a 1-minute window only
        volume_5m=0.0,
        volume_1d=float(candidate.session_volume),
        # 0 means the SEC had no count; None is how the rules read 'unknown'.
        float_shares=ref.shares_outstanding or None,
        alert_count=candidate.appearances_10d,
        tags=tags,
        received_at=candidate.timestamp,
    )


async def run(scanner, refs, trader, key, secret, feed_name, cfg):
    from alpaca.data.enums import DataFeed
    from alpaca.data.live import StockDataStream

    feed = DataFeed.SIP if feed_name == "sip" else DataFeed.IEX
    stream = StockDataStream(key, secret, feed=feed)
    state = {"bars": 0, "last_beat": None}
    # Reconnects build a fresh stream; alpaca-py cannot restart a closed one.

    async def on_bar(raw):
        bar = to_bar(raw)
        if bar is None or bar.symbol not in refs:
            return

        state["bars"] += 1
        now = datetime.now(ET)
        if state["last_beat"] is None or (now - state["last_beat"]).total_seconds() > 300:
            state["last_beat"] = now
            print(f"  [{now:%H:%M}] {state['bars']:,} bars, "
                  f"{trader.seen} alerts seen, {trader.filled} entries, "
                  f"{len(trader.open_positions)} open", flush=True)

        candidate = scanner.on_bar(bar)
        if candidate is not None:
            trader.consider(to_alert(candidate, refs[bar.symbol]))

    asyncio.create_task(trader.monitor())
    print("Listening. Ctrl-C to stop.\n", flush=True)

    # Reconnect rather than exit. The first live session died at 03:30 because
    # an unhandled websocket error propagated out of asyncio.run and hit the
    # os._exit in the caller's finally block — the process vanished with no
    # message, mid-premarket, and nothing noticed until the 08:20 health check
    # would have. A dropped socket is routine; it must never end the session.
    attempt = 0
    while True:
        try:
            stream.subscribe_bars(on_bar, "*")
            await stream._run_forever()
            print(f"  [{datetime.now(ET):%H:%M}] stream ended cleanly, "
                  f"reconnecting", file=sys.stderr, flush=True)
        except asyncio.CancelledError:
            break
        except Exception as exc:                          # noqa: BLE001
            attempt += 1
            print(f"  [{datetime.now(ET):%H:%M}] stream error ({exc}); "
                  f"reconnect #{attempt}", file=sys.stderr, flush=True)
            if attempt in (3, 10, 30):
                trader.notifier.send(
                    "Scanner reconnecting",
                    f"Data stream has dropped {attempt} times. Still trying, "
                    f"but the connection is unstable.",
                    priority="high",
                )

        try:
            await stream.stop_ws()
        except Exception:                                 # noqa: BLE001
            pass

        # Back off gently, capped, so a persistent outage does not hammer the
        # endpoint and does not give up either.
        await asyncio.sleep(min(5 * attempt, 60) or 5)
        stream = StockDataStream(key, secret, feed=feed)

    try:
        await stream.stop_ws()
    except Exception:                                     # noqa: BLE001
        pass


def main() -> None:
    import argparse

    from alpaca.trading.client import TradingClient

    p = argparse.ArgumentParser()
    # SIP by default now that Algo Trader Plus is active. IEX remains
    # available but is blind before 08:00 and sees ~3% of volume, so it needs
    # config/scanner-iex.yaml with its separately scaled thresholds.
    p.add_argument("--feed", default="sip", choices=["sip", "iex"])
    p.add_argument("--scanner-config", default="config/scanner.yaml")
    p.add_argument("--rules-config", default="config/rules.yaml")
    p.add_argument("--refs", default=None)
    p.add_argument("--dry-run", action="store_true",
                   help="decide and log, place no orders")
    args = p.parse_args()

    scanner_cfg = yaml.safe_load(pathlib.Path(args.scanner_config).read_text())
    rules_cfg = yaml.safe_load(pathlib.Path(args.rules_config).read_text())
    alert_path = pathlib.Path("config/alerts.yaml")
    alert_cfg = yaml.safe_load(alert_path.read_text()) if alert_path.exists() else {}

    refs = load_refs_for(args.refs)
    key, secret = load_credentials()

    trading = TradingClient(key, secret, paper=True)
    notifier = Notifier(alert_cfg.get("alerts", {}))

    approvals = None
    approval_cfg = rules_cfg.get("approval", {})
    if approval_cfg.get("enabled") and not args.dry_run:
        approvals = ApprovalQueue(approval_cfg)
        approval_url = approvals.start_server()
    else:
        approval_url = None

    trader = PaperTrader(trading, rules_cfg, notifier, dry_run=args.dry_run,
                         approvals=approvals)
    scanner = Scanner(scanner_cfg, refs)

    account = trading.get_account()
    print(f"\nPaper equity   ${float(account.equity):,.0f}")

    # Adopt anything the broker already holds. A restart used to orphan open
    # positions — they kept running with no stop, no target and no time exit.
    adopted = trader.reconcile()
    if adopted:
        print(f"Adopted        {adopted} existing position(s) — now managed")
    print(f"Feed           {args.feed.upper()}")
    print(f"Universe       {len(refs):,} symbols")
    print(f"Reference      {refs_date_of(args.refs) or 'newest in data/refs'}")
    print(f"Mode           {'DRY RUN — no orders' if args.dry_run else 'PAPER ORDERS'}")
    print(f"Risk           {rules_cfg['risk']['risk_per_trade_pct']}% per trade, "
          f"max {rules_cfg['risk']['max_concurrent']} positions")
    print(f"Journal        data/mosquito/trades.jsonl")
    if approval_url:
        print(f"Approvals      {approval_url}   <- open this on your phone")
    print()

    notifier.send("Paper trader started",
                  f"{len(refs):,} symbols on {args.feed.upper()}, "
                  f"{'dry run' if args.dry_run else 'placing paper orders'}",
                  priority="low")

    try:
        asyncio.run(run(scanner, refs, trader, key, secret, args.feed, rules_cfg))
    except KeyboardInterrupt:
        pass
    finally:
        print(f"\nStopped. {trader.seen} alerts, {trader.filled} entries, "
              f"{trader.skipped} skipped.")
        notifier.send("Paper trader STOPPED",
                      f"{trader.filled} entries, {trader.skipped} skipped.",
                      priority="high")
        os._exit(0)


if __name__ == "__main__":
    main()
