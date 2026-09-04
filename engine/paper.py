"""Paper-trade the mosquito feed. No real money, from the first alert.

    python -m mosquito.paper                # live
    python -m mosquito.paper --dry-run      # decide and log, place no orders

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

# Open positions, written to disk the moment one opens and removed when it
# closes. Reconciliation alone was not enough: it could adopt a position, but
# the stop and target the bot originally chose died with the process, so the
# adopted position got a default stop rather than its real one. This file is
# how a restart recovers the actual decision.
STATE_FILE = pathlib.Path("data/mosquito/open_positions.json")
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
        self.halted_for_day = False
        self._overexposure_warned = False
        # Symbols with a close order already submitted. A queued sell does not
        # remove the position at the broker, so without this the exit sweep
        # sees it again, re-adopts it, and closes it again — every 20 seconds,
        # forever. Cleared when the position actually disappears.
        self.closing: set = set()
        self._load_state()
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

    def _halted_for_overexposure(self) -> bool:
        """Hard stop when live positions exceed the configured limit.

        Not the same as the ordinary max-concurrent check, which merely
        declines the next entry. This fires when the account is ALREADY past
        the limit, which means something has gone wrong, and continuing to
        add risk on top of an unexplained state is the worst response.
        """
        limit = self.cfg["risk"]["max_concurrent"]
        held = self._broker_position_count()
        if held <= limit:
            self._overexposure_warned = False
            return False

        if not getattr(self, "_overexposure_warned", False):
            self._overexposure_warned = True
            message = (f"{held} positions open against a limit of {limit}. "
                       f"No new entries until this is resolved.")
            print(f"  HALTED: {message}", file=sys.stderr, flush=True)
            self.notifier.send("TRADING HALTED — too many positions",
                               message, priority="urgent")
            self._log("halted_overexposure", {"held": held, "limit": limit})
        return True

    def _save_state(self) -> None:
        """Persist open positions. Called on every open and close.

        Cheap — a few hundred bytes — and it is what makes a restart
        recoverable rather than merely survivable.
        """
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(self.open_positions, indent=1))
        except Exception as exc:                          # noqa: BLE001
            print(f"  could not save state: {exc}", file=sys.stderr)

    def _load_state(self) -> None:
        """Restore open positions written by a previous process.

        Only positions the broker still holds are kept; anything closed while
        this process was down is dropped rather than resurrected.
        """
        if not STATE_FILE.exists():
            return
        try:
            saved = json.loads(STATE_FILE.read_text())
        except Exception as exc:                          # noqa: BLE001
            print(f"  could not read saved state: {exc}", file=sys.stderr)
            return

        try:
            held = {p.symbol for p in self.client.get_all_positions()}
        except Exception:                                 # noqa: BLE001
            held = set(saved)                             # cannot verify; trust it

        self.open_positions = {s: r for s, r in saved.items() if s in held}
        dropped = len(saved) - len(self.open_positions)
        if self.open_positions:
            print(f"  restored {len(self.open_positions)} position(s) with "
                  f"their original stops and targets", flush=True)
            for symbol, r in self.open_positions.items():
                print(f"    {symbol:<6} entry {r['signal_price']:.4f}  "
                      f"stop {r['stop']:.4f}  target {r['target']:.4f}  "
                      f"[{r['setup']}]", flush=True)
        if dropped:
            print(f"  {dropped} saved position(s) no longer held; discarded",
                  flush=True)

    def _broker_position_count(self) -> int:
        try:
            return len(self.client.get_all_positions())
        except Exception:                                 # noqa: BLE001
            # Fail closed: if the broker cannot be reached, assume we are at
            # the limit rather than opening something we cannot see.
            return self.cfg["risk"]["max_concurrent"]

    def adopt(self, position) -> dict:
        """Take responsibility for a broker position this process did not open.

        Happens after any restart. Without it the position has no stop, no
        target and no time exit — it simply runs, which is how a -54% loss
        sat open all day with a 12% stop configured.

        The original stop and target are gone with the previous process, so
        they are rebuilt from the entry price using the configured defaults.
        Approximate management beats none.
        """
        entry = float(position.avg_entry_price)
        stop_pct = self.cfg["execution"].get("adopted_stop_pct", 12.0)
        target_pct = self.cfg["execution"].get("adopted_target_pct", 25.0)

        record = {
            "symbol": position.symbol,
            "shares": int(float(position.qty)),
            "signal_price": entry,
            "stop": round(entry * (1 - stop_pct / 100), 4),
            "target": round(entry * (1 + target_pct / 100), 4),
            "setup": "adopted",
            "reason": "position found at startup, not opened by this process",
            "opened_at": datetime.now(ET).isoformat(),
        }
        self.open_positions[position.symbol] = record
        self._save_state()
        self._log("adopted", record)
        return record

    def reconcile(self) -> int:
        """Adopt every broker position this process is not already managing."""
        try:
            positions = self.client.get_all_positions()
        except Exception as exc:                          # noqa: BLE001
            print(f"  reconcile failed: {exc}", file=sys.stderr)
            return 0

        adopted = 0
        for position in positions:
            if position.symbol not in self.open_positions:
                record = self.adopt(position)
                pnl = float(position.unrealized_plpc) * 100
                print(f"  adopted {position.symbol} x{record['shares']} "
                      f"@ {record['signal_price']:.4f} ({pnl:+.1f}%) "
                      f"stop {record['stop']:.4f}", flush=True)
                adopted += 1

        if adopted:
            self.notifier.send(
                f"Adopted {adopted} existing position(s)",
                "Positions were open at the broker but untracked. They now "
                "have stops and will be managed.",
                priority="high",
            )
        return adopted

    def _log(self, kind: str, payload: dict) -> None:
        TRADE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with TRADE_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": kind,
                                     "at": datetime.now(ET).isoformat(),
                                     **payload}) + "\n")

    # -------------------------------------------------------------- entries

    def consider_with_stop(self, alert, stop: float, setup: str,
                           reason: str) -> None:
        """Enter using a stop the pattern determined.

        The ordinary path sizes from a percentage stop in config. Chart
        patterns place the stop below the structure that justified the entry
        — the low of the pullback, not a fixed distance — so the size follows
        from that instead.
        """
        now = datetime.now(ET)
        self._roll_session(now)
        self.seen += 1

        if self.halted_for_day or self._halted_for_overexposure():
            return
        if alert.symbol in self.taken_today:
            return
        if self._broker_position_count() >= self.cfg["risk"]["max_concurrent"]:
            return
        if stop <= 0 or stop >= alert.price:
            return

        # A structural stop can land very close to the entry — MSTZ came in
        # at 1.86% on 2026-09-03 and was stopped by ordinary noise. The
        # six-month study put median adverse excursion near -5%, so anything
        # tighter than the floor is widened rather than obeyed.
        floor_pct = self.cfg["execution"].get("min_stop_pct", 5.0)
        stop = min(stop, alert.price * (1 - floor_pct / 100))

        stop_pct = (1 - stop / alert.price) * 100
        shares = self._size(alert.price, stop_pct)
        if shares <= 0:
            return

        entry = {
            "symbol": alert.symbol, "shares": shares,
            "signal_price": alert.price, "stop": round(stop, 4),
            "target": None, "setup": setup, "reason": reason,
            "opened_at": now.isoformat(),
        }

        if self.dry_run:
            self._log("dry_run_entry", entry)
            print(f"  {now:%H:%M}  WOULD BUY {alert.symbol} x{shares} "
                  f"@ {alert.price:.4f} stop {stop:.4f} [{setup}]", flush=True)
            return

        if not self._submit_buy(alert, shares):
            return

        self.taken_today.add(alert.symbol)
        self.open_positions[alert.symbol] = entry
        self._save_state()
        self.filled += 1
        self._log("entry", entry)

        print(f"  {now:%H:%M}  BUY {alert.symbol} x{shares} @ "
              f"{alert.price:.4f} stop {stop:.4f} ({stop_pct:.1f}%) "
              f"[{setup}]", flush=True)
        self.notifier.send(
            f"PAPER BUY {alert.symbol} [{setup}]",
            f"{shares:,} sh @ ~${alert.price:.4f}\n"
            f"stop {stop:.4f} ({stop_pct:.1f}%)\n{reason}",
        )

    def consider(self, alert) -> None:
        now = datetime.now(ET)
        self._roll_session(now)
        self.seen += 1

        # GUARD 1 — stop entirely if the broker holds more than the limit.
        # On 2026-08-20 the limit was checked against in-memory state that a
        # restart had wiped, so the bot opened 11 positions against a cap of
        # 3. Checking the broker and refusing outright is the backstop.
        if self.halted_for_day:
            return
        if self._halted_for_overexposure():
            return

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
        # Count what the BROKER holds, not what this process remembers. A
        # restart wipes in-memory state while the positions live on, and on
        # 2026-08-20 that let one session accumulate 11 positions against a
        # limit of 3 — each restart believing it held none.
        if self._broker_position_count() >= self.cfg["risk"]["max_concurrent"]:
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
        self._save_state()
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

            if self._broker_position_count() >= self.cfg["risk"]["max_concurrent"]:
                self._log("rejected_late", {**entry, "reason": "max positions"})
                continue

            if self._submit_buy(alert, request.shares):
                self.taken_today.add(request.symbol)
                self.open_positions[request.symbol] = entry
                self._save_state()
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
            if self.dry_run:
                continue
            if getattr(self, "halted_for_day", False):
                continue
            if self.check_daily_loss():
                continue
            if not self.open_positions:
                continue
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._check_positions)
            except Exception as exc:                      # noqa: BLE001
                print(f"  position check failed: {exc}", file=sys.stderr)

    def check_daily_loss(self) -> bool:
        """Flatten and stop if the day's loss exceeds the cap.

        Counts UNREALISED losses too. The original cap only counted realised
        ones, so on a day when nothing ever closed it never fired — the
        account fell 10% with the limit set at 2% and the check silently
        satisfied, because zero trades had been closed.
        """
        # Only during the session. Outside it, Alpaca's last_equity rolls to
        # the new day's baseline while equity still reflects the previous
        # close, so the comparison shows a drawdown that never happened. That
        # fired at 00:17 on 2026-09-04 reporting -3.8%, and on 2026-08-24
        # reporting -2.05% on a day with no trades at all — each time
        # flattening the book and halting on a phantom loss.
        now = datetime.now(ET)
        if now.weekday() >= 5 or not (time(4, 0) <= now.time() <= time(20, 0)):
            return False

        cap_pct = self.cfg["risk"].get("max_daily_loss_pct", 2.0)
        try:
            account = self.client.get_account()
            equity = float(account.equity)
            start = float(account.last_equity)
        except Exception:                                 # noqa: BLE001
            return False

        if start <= 0:
            return False
        drawdown = (equity / start - 1) * 100
        if drawdown > -cap_pct:
            return False

        print(f"  DAILY LOSS LIMIT: {drawdown:+.1f}% vs cap {-cap_pct:.1f}% — "
              f"flattening", file=sys.stderr, flush=True)
        self.notifier.send(
            "DAILY LOSS LIMIT HIT",
            f"Down {drawdown:.1f}% today (cap {cap_pct:.1f}%). Closing "
            f"everything and stopping for the day.",
            priority="urgent",
        )
        try:
            self.client.close_all_positions(cancel_orders=True)
        except Exception as exc:                          # noqa: BLE001
            print(f"  flatten failed: {exc}", file=sys.stderr)
        self._log("daily_loss_halt", {"drawdown_pct": round(drawdown, 2),
                                      "cap_pct": cap_pct})
        self.halted_for_day = True
        return True

    def _trail_stop(self, entry: dict, price: float) -> float:
        """Raise the stop as the position makes new highs.

        The 32 logged manual trades averaged +47.4% on winners against a
        -11.9% average loser — a 4:1 payoff at a 50% hit rate, +17.8%
        expectancy. Running those same entries through a fixed 15% target and
        8% stop collapses expectancy to +3.9%, so the tight exits alone were
        costing about 14 points a trade. Nine of the sixteen losers also went
        past -8%, meaning the stop was inside the range these names normally
        travel.

        A trail keeps the position while the move continues and still caps
        the loss if it does not. The cost is giving back part of the peak on
        every winner, which is the price of staying in the ones that run.
        """
        cfg = self.cfg["execution"]
        trail_pct = cfg.get("trail_pct")
        if not trail_pct:
            return entry["stop"]

        peak = max(entry.get("peak", entry["signal_price"]), price)
        entry["peak"] = peak

        # Only trail once the trade is meaningfully up. Before that the trail
        # sits inside normal noise and stops out trades that go on to work.
        arm_at = cfg.get("trail_arms_at_pct", 10.0)
        if peak < entry["signal_price"] * (1 + arm_at / 100):
            return entry["stop"]

        # Two floors under the trailing stop.
        #
        # A percentage trail alone is wider than most moves: SDST ran from
        # 0.28 to 0.3202 on 2026-09-03 — up 14.4% — and a 12% trail put the
        # stop at 0.2818, exiting at exactly 0.0%. The whole gain was given
        # back because the trail was wider than the gain.
        #
        # So the stop also keeps a share of whatever the trade has actually
        # made. Giving back half the peak gain still lets a runner run, but a
        # +14% move now locks in +7% instead of nothing.
        entry_price = entry["signal_price"]
        keep = self.cfg["execution"].get("keep_gain_fraction", 0.5)
        gain = peak - entry_price
        retained = entry_price + gain * keep

        trailed = max(round(peak * (1 - trail_pct / 100), 4),
                      round(retained, 4),
                      round(entry_price * 1.001, 4))

        if trailed > entry["stop"]:
            entry["stop"] = trailed
            self._save_state()
        return entry["stop"]

    def _check_positions(self) -> None:
        """Sweep everything the BROKER holds, not everything we remember.

        Iterating the in-memory dict leaves any position opened by a previous
        process completely unmanaged. Driving from the broker's list means a
        position cannot be orphaned by a restart.
        """
        live = {p.symbol: p for p in self.client.get_all_positions()}
        now = datetime.now(ET)
        hard_exit = time.fromisoformat(self.cfg["execution"]["hard_exit_time"])

        # Drop anything that has finished closing.
        self.closing &= set(live)

        for symbol in live:
            if symbol in self.closing:
                continue                      # close already submitted
            if symbol not in self.open_positions:
                self.adopt(live[symbol])

        for symbol, entry in list(self.open_positions.items()):
            if symbol in self.closing:
                continue                      # waiting on a submitted close
            position = live.get(symbol)
            if position is None:
                # Closed at the broker, by us or otherwise.
                self.open_positions.pop(symbol, None)
                self._save_state()
                continue

            price = float(position.current_price)
            pnl_pct = float(position.unrealized_plpc) * 100

            stop = self._trail_stop(entry, price)

            reason = None
            if price <= stop:
                trailing = entry.get("peak", 0) > entry["signal_price"] * 1.05
                reason = "trail" if trailing else "stop"
            elif entry.get("target") and price >= entry["target"]:
                reason = "target"
            elif now.time() >= hard_exit:
                reason = "time"

            if reason:
                self._close(symbol, entry, price, pnl_pct, reason)

    def _close(self, symbol: str, entry: dict, price: float,
               pnl_pct: float, reason: str) -> None:
        # Mark before submitting. Whether the order is accepted or rejected,
        # retrying every 20 seconds helps nothing and floods the log.
        self.closing.add(symbol)
        try:
            self.client.close_position(symbol)
        except Exception as exc:                          # noqa: BLE001
            print(f"  close failed for {symbol}: {exc}", file=sys.stderr)
            return

        self.open_positions.pop(symbol, None)
        self._save_state()
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
    p.add_argument("--config", default="config/mosquito.yaml")
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
