"""Paper-trade the mosquito feed. No real money, from the first alert.

    python -m engine.paper                # live
    python -m engine.paper --dry-run      # decide and log, place no orders

Listens to the Discord channel, applies the rules, and places paper orders
on Alpaca. Exits are managed from Alpaca's own position data — which is part
of the trading API, not the market data plan, so this needs no data
subscription at all.

Every alert is journalled whether it was taken or skipped, with the reason.
The skips are as useful as the fills: a week of rejection reasons tells you
which threshold is wrong, and that is not something a backtest could show.

Needs, in .env:
  DISCORD_BOT_TOKEN, MOSQUITO_CHANNEL_ID, ALPACA_API_KEY, ALPACA_SECRET_KEY
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
from datetime import datetime, time
from zoneinfo import ZoneInfo

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from alerts.notify import Notifier
from engine.approval import ApprovalQueue
from engine.collect import Journal, load_env
from engine.parser import parse_message
from engine.rules import decide

ET = ZoneInfo("America/New_York")
TRADE_LOG = pathlib.Path("data/mosquito/trades.jsonl")
POSITION_POLL_SECONDS = 20


class PaperTrader:
    def __init__(self, client, cfg: dict, notifier: Notifier,
                 dry_run: bool = False, approvals: ApprovalQueue | None = None
                 ) -> None:
        self.client = client
        self.cfg = cfg
        self.notifier = notifier
        self.dry_run = dry_run
        self.approvals = approvals
        self.awaiting: list = []

        self.open_positions: dict[str, dict] = {}
        self.taken_today: set = set()
        self.seen = self.filled = self.skipped = 0
        self.session_date = None

    # ------------------------------------------------------------- helpers

    def _roll_session(self, now: datetime) -> None:
        if self.session_date != now.date():
            self.session_date = now.date()
            self.taken_today.clear()

    def _equity(self) -> float:
        try:
            return float(self.client.get_account().equity)
        except Exception:                                 # noqa: BLE001
            return self.cfg["risk"]["fallback_equity"]

    def _size(self, price: float, stop_pct: float) -> int:
        """Shares such that hitting the stop costs the per-trade risk budget.

        Capped by position size so a very tight stop cannot imply an
        enormous position.
        """
        risk = self.cfg["risk"]
        risk_dollars = self._equity() * risk["risk_per_trade_pct"] / 100
        per_share = price * stop_pct / 100
        if per_share <= 0:
            return 0
        shares = int(risk_dollars / per_share)
        max_shares = int(self._equity() * risk["max_position_pct"] / 100 / price)
        return max(0, min(shares, max_shares))

    def _log(self, kind: str, payload: dict) -> None:
        TRADE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with TRADE_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": kind,
                                     "at": datetime.now(ET).isoformat(),
                                     **payload}) + "\n")

    # -------------------------------------------------------------- entries

    def consider(self, alert) -> None:
        now = datetime.now(ET)
        self._roll_session(now)
        self.seen += 1

        verdict = decide(alert, self.cfg)
        row = alert.to_row()
        row.update({"take": verdict.take, "setup": verdict.setup,
                    "reason": verdict.reason})

        if not verdict.take:
            self.skipped += 1
            self._log("skip", row)
            return

        # One position per symbol per session. The feed re-alerts the same
        # ticker constantly; without this a single runner becomes twenty
        # correlated positions.
        if alert.symbol in self.taken_today:
            self._log("skip", {**row, "reason": "already traded today"})
            return
        if len(self.open_positions) >= self.cfg["risk"]["max_concurrent"]:
            self._log("skip", {**row, "reason": "max concurrent positions"})
            return

        shares = self._size(alert.price, verdict.stop_pct)
        if shares <= 0:
            self._log("skip", {**row, "reason": "size rounded to zero"})
            return

        stop = round(alert.price * (1 - verdict.stop_pct / 100), 4)
        target = round(alert.price * (1 + verdict.target_pct / 100), 4)

        entry = {
            "symbol": alert.symbol, "shares": shares,
            "signal_price": alert.price, "stop": stop, "target": target,
            "setup": verdict.setup, "reason": verdict.reason,
            "opened_at": now.isoformat(),
        }

        if self.dry_run:
            self._log("dry_run_entry", entry)
            print(f"  {now:%H:%M}  WOULD BUY {alert.symbol} x{shares} "
                  f"@ {alert.price:.2f}  [{verdict.setup}] {verdict.reason}",
                  flush=True)
            return

        # Gate entries behind human approval when configured. Exits are never
        # gated — see mosquito/approval.py.
        gated = self.cfg.get("approval", {}).get("require_for", [])
        if self.approvals and verdict.setup in gated:
            request = self.approvals.submit(
                symbol=alert.symbol, setup=verdict.setup, price=alert.price,
                shares=shares, stop=stop, target=target, reason=verdict.reason,
                features={"pct_change": alert.pct_change,
                          "float_turnover": alert.float_turnover,
                          "rel_volume_1m": alert.rel_volume_1m,
                          "alert_count": alert.alert_count},
            )
            self.awaiting.append((request, alert, entry))
            self._log("awaiting_approval", entry)
            print(f"  {now:%H:%M}  ASK  {alert.symbol} x{shares} "
                  f"@ {alert.price:.2f}  [{verdict.setup}]", flush=True)
            self.notifier.send(
                f"APPROVE? {alert.symbol} {alert.pct_change:+.0f}%",
                f"{shares:,} sh @ ${alert.price:.2f} (${shares * alert.price:,.0f})\n"
                f"stop {stop:.2f}  target {target:.2f}\n{verdict.reason}\n"
                f"Expires in {self.approvals.timeout}s",
                priority="high",
            )
            return

        if not self._submit_buy(alert, shares):
            return

        self.taken_today.add(alert.symbol)
        self.open_positions[alert.symbol] = entry
        self.filled += 1
        self._log("entry", entry)

        print(f"  {now:%H:%M}  BUY {alert.symbol} x{shares} @ ~{alert.price:.2f} "
              f"stop {stop:.2f} target {target:.2f}  [{verdict.setup}]",
              flush=True)
        self.notifier.send(
            f"PAPER BUY {alert.symbol} {alert.pct_change:+.0f}%",
            f"{shares} sh @ ~${alert.price:.2f}\n"
            f"stop {stop:.2f}  target {target:.2f}\n{verdict.reason}",
        )

    def _submit_buy(self, alert, shares: int) -> bool:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        now = datetime.now(ET).time()
        extended = now < time(9, 30) or now >= time(16, 0)
        slip = self.cfg["execution"]["entry_slippage_pct"] / 100

        try:
            self.client.submit_order(LimitOrderRequest(
                symbol=alert.symbol,
                qty=shares,
                side=OrderSide.BUY,
                # Extended-hours orders must be limit + DAY on Alpaca.
                time_in_force=TimeInForce.DAY,
                limit_price=round(alert.price * (1 + slip), 2),
                extended_hours=extended,
            ))
            return True
        except Exception as exc:                          # noqa: BLE001
            print(f"  order rejected for {alert.symbol}: {exc}",
                  file=sys.stderr, flush=True)
            self._log("rejected", {"symbol": alert.symbol, "error": str(exc)})
            return False

    # --------------------------------------------------------------- exits

    def check_approvals(self) -> None:
        """Place approved entries; drop rejected and expired ones.

        Polled rather than awaited so one undecided trade cannot stall the
        alert handler and back the feed up behind it.
        """
        if not self.approvals:
            return

        still_waiting = []
        for request, alert, entry in self.awaiting:
            if not self.approvals.is_settled(request):
                still_waiting.append((request, alert, entry))
                continue

            if not self.approvals.resolve(request):
                self._log("rejected_by_human", {**entry,
                                                "seconds": round(request.age, 1)})
                print(f"  {datetime.now(ET):%H:%M}  SKIP {request.symbol} "
                      f"(not approved)", flush=True)
                continue

            if len(self.open_positions) >= self.cfg["risk"]["max_concurrent"]:
                self._log("rejected_late", {**entry, "reason": "max positions"})
                continue

            if self._submit_buy(alert, request.shares):
                self.taken_today.add(request.symbol)
                self.open_positions[request.symbol] = entry
                self.filled += 1
                self._log("entry", entry)
                print(f"  {datetime.now(ET):%H:%M}  BUY  {request.symbol} "
                      f"x{request.shares} (approved)", flush=True)
                self.notifier.send(
                    f"PAPER BUY {request.symbol}",
                    f"{request.shares:,} sh @ ~${request.price:.2f}\n"
                    f"stop {entry['stop']:.2f}  target {entry['target']:.2f}",
                )

        self.awaiting = still_waiting
        self.approvals.cleanup()

    async def monitor(self) -> None:
        """Watch open positions and exit on stop, target or time.

        Prices come from Alpaca's positions endpoint, which is part of the
        trading API rather than the market data plan — so this works on the
        free tier with no subscription.
        """
        while True:
            await asyncio.sleep(POSITION_POLL_SECONDS)
            self.check_approvals()
            if self.dry_run or not self.open_positions:
                continue
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._check_positions)
            except Exception as exc:                      # noqa: BLE001
                print(f"  position check failed: {exc}", file=sys.stderr)

    def _check_positions(self) -> None:
        live = {p.symbol: p for p in self.client.get_all_positions()}
        now = datetime.now(ET)
        hard_exit = time.fromisoformat(self.cfg["execution"]["hard_exit_time"])

        for symbol, entry in list(self.open_positions.items()):
            position = live.get(symbol)
            if position is None:
                # Filled and closed elsewhere, or never filled.
                self.open_positions.pop(symbol, None)
                continue

            price = float(position.current_price)
            pnl_pct = float(position.unrealized_plpc) * 100

            reason = None
            if price <= entry["stop"]:
                reason = "stop"
            elif price >= entry["target"]:
                reason = "target"
            elif now.time() >= hard_exit:
                reason = "time"

            if reason:
                self._close(symbol, entry, price, pnl_pct, reason)

    def _close(self, symbol: str, entry: dict, price: float,
               pnl_pct: float, reason: str) -> None:
        try:
            self.client.close_position(symbol)
        except Exception as exc:                          # noqa: BLE001
            print(f"  close failed for {symbol}: {exc}", file=sys.stderr)
            return

        self.open_positions.pop(symbol, None)
        record = {**entry, "exit_price": price, "pnl_pct": round(pnl_pct, 2),
                  "exit_reason": reason}
        self._log("exit", record)

        print(f"  {datetime.now(ET):%H:%M}  SELL {symbol} @ {price:.2f} "
              f"({pnl_pct:+.1f}%) — {reason}", flush=True)
        self.notifier.send(
            f"PAPER SELL {symbol} {pnl_pct:+.1f}%",
            f"exit {price:.2f} on {reason}\nentry was {entry['signal_price']:.2f}",
        )


def main() -> None:
    import argparse

    import discord
    from alpaca.trading.client import TradingClient

    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="decide and log, but place no orders")
    p.add_argument("--config", default="config/rules.yaml")
    args = p.parse_args()

    cfg = yaml.safe_load(pathlib.Path(args.config).read_text())
    token, channel_id = load_env()

    key, secret = os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
    client = TradingClient(key, secret, paper=True)

    alert_cfg_path = pathlib.Path("config/alerts.yaml")
    alert_cfg = yaml.safe_load(alert_cfg_path.read_text()) if alert_cfg_path.exists() else {}
    notifier = Notifier(alert_cfg.get("alerts", {}))

    approvals = None
    approval_cfg = cfg.get("approval", {})
    if approval_cfg.get("enabled") and not args.dry_run:
        approvals = ApprovalQueue(approval_cfg)
        url = approvals.start_server()
        print(f"\nApprovals      {url}   <- open this on your phone")

    trader = PaperTrader(client, cfg, notifier, dry_run=args.dry_run,
                         approvals=approvals)
    journal = Journal()

    intents = discord.Intents.default()
    intents.message_content = True
    discord_client = discord.Client(intents=intents)

    @discord_client.event
    async def on_ready():
        channel = discord_client.get_channel(channel_id)
        if channel is None:
            print(f"Channel {channel_id} not visible to the bot.", file=sys.stderr)
            await discord_client.close()
            return

        account = client.get_account()
        print(f"\nPaper equity   ${float(account.equity):,.0f}")
        print(f"Watching       #{channel.name}")
        print(f"Mode           {'DRY RUN — no orders' if args.dry_run else 'PAPER ORDERS'}")
        print(f"Risk           {cfg['risk']['risk_per_trade_pct']}% per trade, "
              f"max {cfg['risk']['max_concurrent']} positions")
        print(f"Journal        {TRADE_LOG}\n")

        discord_client.loop.create_task(trader.monitor())
        print("Listening.\n", flush=True)

    @discord_client.event
    async def on_message(message):
        if message.channel.id != channel_id:
            return
        for alert in parse_message(message.content, message.created_at):
            journal.write(alert.to_row(), message.created_at)
            trader.consider(alert)

    try:
        discord_client.run(token, log_handler=None)
    except KeyboardInterrupt:
        pass
    finally:
        print(f"\nStopped. {trader.seen:,} alerts, {trader.filled} entries, "
              f"{trader.skipped:,} skipped.")
        journal.close()


if __name__ == "__main__":
    main()
