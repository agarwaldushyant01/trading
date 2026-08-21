"""What actually happened today.

    python -m tools.day_report                 # today
    python -m tools.day_report --date 2026-08-20

Reads the trade journal, the approval log, and the broker's own fill history,
and reports the four things worth knowing: what was bought, what was sold,
what it cost, and what was passed over.

The broker's fills are the authority. The journal records what the bot
INTENDED; only Alpaca knows what actually executed, and on 2026-08-20 those
two diverged badly — the journal showed positions being opened while the
in-memory state that should have limited them had already been wiped by a
restart.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from data.reference import load_credentials

ET = ZoneInfo("America/New_York")


def load_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def fills(day: date) -> list:
    """Every execution the broker recorded. The authority on what happened."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import ActivityType

    key, secret = load_credentials()
    client = TradingClient(key, secret, paper=True)
    try:
        return client.get_account_activities(
            activity_types=[ActivityType.FILL], date=day)
    except Exception as exc:                              # noqa: BLE001
        print(f"  could not fetch fills: {exc}", file=sys.stderr)
        return []


def report(day: date) -> None:
    trades = [r for r in load_jsonl(pathlib.Path("data/mosquito/trades.jsonl"))
              if r.get("at", "").startswith(day.isoformat())]
    approvals = [r for r in load_jsonl(pathlib.Path("data/mosquito/approvals.jsonl"))
                 if r.get("at", "").startswith(day.isoformat())]

    print(f"\n{'=' * 66}\n  SESSION REPORT — {day}\n{'=' * 66}")

    # --- what the scanner and rules did ---------------------------------
    kinds = Counter(r["kind"] for r in trades)
    skips = Counter(r["reason"] for r in trades if r["kind"] == "skip")

    print(f"\nCANDIDATES")
    print(f"  {len(trades):>5} alerts reached the rules")
    print(f"  {kinds.get('skip', 0):>5} rejected by rules")
    print(f"  {kinds.get('awaiting_approval', 0):>5} sent to you for approval")
    print(f"  {kinds.get('entry', 0):>5} became entries")

    if skips:
        print(f"\n  why the rules rejected things:")
        for reason, n in skips.most_common(8):
            print(f"    {n:>4}  {reason}")

    # --- what you decided ------------------------------------------------
    if approvals:
        outcome = Counter(
            "approved" if r["approved"]
            else ("rejected" if r["resolved_by"] == "manual" else "expired")
            for r in approvals
        )
        print(f"\nYOUR DECISIONS  ({len(approvals)} requests)")
        for label in ("approved", "rejected", "expired"):
            print(f"  {outcome.get(label, 0):>5} {label}")

        expired = [r for r in approvals if not r["approved"]
                   and r["resolved_by"] == "timeout"]
        if expired:
            print(f"\n  expired unanswered — the ones you never got to decide:")
            for r in sorted(expired, key=lambda x: -x["pct_change"])[:6]:
                print(f"    {r['symbol']:<6} {r['pct_change']:>+7.1f}%  "
                      f"{r['shares']:>7,} sh @ ${r['price']:.4f}")

    # --- what the broker actually executed -------------------------------
    executed = fills(day)
    buys = [f for f in executed if f.side.value.startswith("buy")]
    sells = [f for f in executed if f.side.value.startswith("sell")]

    bought = sum(float(f.price) * float(f.qty) for f in buys)
    sold = sum(float(f.price) * float(f.qty) for f in sells)

    print(f"\nEXECUTED AT THE BROKER")
    print(f"  {len(buys):>5} buys   ${bought:>12,.2f}")
    print(f"  {len(sells):>5} sells  ${sold:>12,.2f}")

    # --- realised versus open --------------------------------------------
    from alpaca.trading.client import TradingClient
    key, secret = load_credentials()
    client = TradingClient(key, secret, paper=True)
    account = client.get_account()
    positions = client.get_all_positions()

    equity = float(account.equity)
    unrealised = sum(float(p.unrealized_pl) for p in positions)

    print(f"\nACCOUNT")
    print(f"  equity            ${equity:>12,.2f}")
    print(f"  open positions    {len(positions):>13}")
    if positions:
        print(f"  unrealised P&L    ${unrealised:>12,.2f}")
        worst = sorted(positions, key=lambda p: float(p.unrealized_plpc))[:5]
        print(f"\n  worst open:")
        for p in worst:
            print(f"    {p.symbol:<6} {float(p.unrealized_plpc)*100:>+7.1f}%  "
                  f"${float(p.unrealized_pl):>10,.2f}")

    # --- the check that would have caught today ---------------------------
    print(f"\nLIMIT CHECK")
    entries = [r for r in trades if r["kind"] == "entry"]
    print(f"  entries opened today       {len(entries)}")
    print(f"  configured max concurrent  3")
    if len(positions) > 3:
        print(f"  positions now open         {len(positions)}  <-- OVER LIMIT")
        print(f"\n  More positions are open than the limit allows. That means")
        print(f"  the limit was measured against something other than the")
        print(f"  broker's own list.")

    exits = [r for r in trades if r["kind"] == "exit"]
    print(f"\n  exits recorded             {len(exits)}")
    if entries and not exits:
        print(f"  Entries but no exits: nothing was stopped out, hit a target,")
        print(f"  or was flattened. Positions opened and were never managed.")

    print()


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--date", default=date.today().isoformat())
    args = p.parse_args()
    report(date.fromisoformat(args.date))


if __name__ == "__main__":
    main()
