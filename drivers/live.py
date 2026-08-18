"""Live scanner — streams real-time bars and alerts on candidates.

    python -m drivers.live                          # live session
    python -m drivers.live --test-date 2026-02-27   # replay a cached day

PLACES NO ORDERS. It watches, alerts, and writes a journal. Every trading
decision stays with you.

REQUIRES Algo Trader Plus ($99/month). The free plan's real-time feed is IEX
only, roughly 3% of market volume, and the scanner's relative-volume
thresholds are calibrated against full-market data. On IEX the best reading
in a six-month sample was 0.3 against a threshold of 3.0 — nothing would
ever fire, and it would fail silently rather than erroring.

The journal is JSON Lines, appended and flushed per alert, so a crash or a
lost connection cannot cost you the day's record.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
from dataclasses import asdict
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from alerts.notify import Notifier, format_candidate
from data.reference import load_credentials, load_refs_for
from drivers.replay import fetch_daily, fetch_minutes, prescreen
from risk.sizing import RiskConfig, RiskManager
from scanner.scanner import Bar, Scanner

ET = ZoneInfo("America/New_York")
JOURNAL_DIR = pathlib.Path("data/live")

# Reference stop used only to suggest a size in the alert. It is not a
# recommendation — no setup has passed a backtest yet.
REFERENCE_STOP_PCT = 10.0


class LiveScanner:
    def __init__(self, scanner: Scanner, refs: dict, risk: RiskManager,
                 notifier: Notifier, refs_date: date | None = None) -> None:
        self.scanner = scanner
        self.refs = refs
        self.risk = risk
        self.notifier = notifier
        self.refs_date = refs_date
        self.warned_stale_for = None
        self.last_heartbeat = None
        self.last_bar_at = None
        self.alerts_today = 0
        self.bars_seen = 0
        self.journal = None
        self.journal_date = None

    def _journal_for(self, day: date):
        """One file per session date, rolled when the date changes.

        Matters when the process runs overnight: started on a Sunday
        evening, a fixed filename would file Monday's premarket alerts under
        Sunday, and every subsequent day into the same file.
        """
        if self.journal_date != day:
            if self.journal:
                self.journal.close()
            JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
            self.journal = (JOURNAL_DIR / f"{day.isoformat()}.jsonl").open(
                "a", encoding="utf-8")
            self.journal_date = day
            self.alerts_today = 0
        return self.journal

    def _warn_if_stale(self, day: date) -> None:
        """Reference data carries the PRIOR session's close and 20-day
        volumes. Left running across days it silently computes percent
        change against a stale price, so say so loudly rather than
        reporting confident nonsense."""
        if self.refs_date is None or self.warned_stale_for == day:
            return
        if day > self.refs_date:
            self.warned_stale_for = day
            banner = "!" * 64
            message = (
                f"\n{banner}\n"
                f"  REFERENCE DATA IS FOR {self.refs_date}, TODAY IS {day}.\n"
                f"  Percent-change and relative-volume readings are being\n"
                f"  computed against a stale prior close. Stop, rebuild, and\n"
                f"  restart:\n"
                f"    python -m data.reference --date {day} --feed iex\n"
                f"{banner}\n"
            )
            print(message, file=sys.stderr, flush=True)
            self.notifier.send("SCANNER: stale reference data",
                               f"Refs are for {self.refs_date}, today is {day}. "
                               f"Rebuild and restart.", priority="high")

    def _heartbeat(self, now: datetime) -> None:
        """Print a liveness line every few minutes.

        Silence is ambiguous: a healthy stream on a quiet morning and a dead
        socket look exactly the same from the outside. This makes the
        difference visible without waiting for a candidate.
        """
        interval = 300                                    # seconds
        if self.last_heartbeat and (now - self.last_heartbeat).total_seconds() < interval:
            return
        self.last_heartbeat = now
        age = ("never" if self.last_bar_at is None
               else f"{(now - self.last_bar_at).total_seconds():.0f}s ago")
        print(f"  [{now:%H:%M}] {self.bars_seen:,} bars received, "
              f"last {age}, {self.alerts_today} alerts", flush=True)

    def handle(self, bar: Bar, push: bool = True) -> None:
        """push=False during backfill: the journal still records everything,
        but a mid-session start does not fire twenty stale notifications at
        once for moves that already happened."""
        self.bars_seen += 1

        self._warn_if_stale(bar.timestamp.date())

        candidate = self.scanner.on_bar(bar)
        if candidate is None:
            return

        ref = self.refs[bar.symbol]
        sizing = self.risk.size(
            entry_price=candidate.price,
            stop_price=candidate.price * (1 - REFERENCE_STOP_PCT / 100),
            atr=ref.atr_14,
            avg_20d_volume=ref.avg_20d_volume,
        )

        title, body = format_candidate(candidate, ref, sizing)
        if push:
            self.notifier.send(title, body)
        self.alerts_today += 1

        row = candidate.to_row()
        row["timestamp"] = candidate.timestamp.isoformat()
        row["ref_shares"] = sizing.shares if sizing.allowed else 0
        row["ref_stop"] = sizing.stop_price if sizing.allowed else None
        row["logged_at"] = datetime.now(ET).isoformat()
        journal = self._journal_for(candidate.timestamp.date())
        journal.write(json.dumps(row) + "\n")
        journal.flush()               # per alert: a crash must not lose the day

        print(f"  {'' if push else '(missed) '}"
              f"{candidate.timestamp:%H:%M}  {candidate.symbol:<6} "
              f"{candidate.price:>7.2f} {candidate.pct_change_from_prior_close:>+6.1f}% "
              f"relvol {candidate.rel_volume:>5.1f}  {candidate.mode.value}",
              flush=True)

    def close(self) -> None:
        if self.journal:
            self.journal.close()


def to_bar(raw) -> Bar | None:
    """Alpaca stream bar -> scanner Bar, in Eastern time.

    The scanner compares clock times against 04:00 / 09:30 / 16:00. Alpaca
    sends UTC, so skipping this conversion misclassifies every session.
    """
    try:
        return Bar(
            symbol=raw.symbol,
            timestamp=raw.timestamp.astimezone(ET),
            open=float(raw.open),
            high=float(raw.high),
            low=float(raw.low),
            close=float(raw.close),
            volume=int(raw.volume),
            vwap=float(raw.vwap) if getattr(raw, "vwap", None) else None,
        )
    except Exception:                                     # noqa: BLE001
        return None


def refs_date_of(path: pathlib.Path | None) -> date | None:
    """The session a reference file was built for, from its filename."""
    target = path
    if target is None:
        available = sorted(pathlib.Path("data/refs").glob("*.json"))
        if not available:
            return None
        target = available[-1]
    try:
        return date.fromisoformat(pathlib.Path(target).stem[:10])
    except ValueError:
        return None


def check_refs_are_current(refs_path: pathlib.Path | None) -> None:
    """Reference data carries yesterday's close and 20-day volumes. Stale
    refs mean wrong thresholds all day, silently."""
    available = sorted(pathlib.Path("data/refs").glob("*.json"))
    if not available:
        raise SystemExit(
            "No reference data. Build it before the session:\n"
            "  python -m data.reference --date "
            f"{date.today().isoformat()}"
        )
    # Filenames carry a feed suffix on non-SIP builds ("2026-08-17-iex"),
    # so parse the leading date rather than the whole stem.
    try:
        newest = date.fromisoformat(available[-1].stem[:10])
    except ValueError:
        print(f"  WARNING: cannot read a date from {available[-1].name}; "
              f"skipping the freshness check.", file=sys.stderr)
        return
    age = (date.today() - newest).days
    if age > 0:
        print(f"  WARNING: reference data is {age} day(s) old ({newest}).",
              file=sys.stderr)
        print(f"  Rebuild before the open: python -m data.reference "
              f"--date {date.today().isoformat()}\n", file=sys.stderr)


def backfill(live: LiveScanner, client, cfg: dict, feed, day: date) -> None:
    """Replay today's bars so far before going live.

    Without this a mid-session start is broken in a way that produces no
    error: the scanner has counted no volume yet, while the time-of-day
    curve expects a good share of the day already traded, so relative
    volume reads near zero and nothing ever fires.

    Candidates found here are journalled and printed but not pushed — they
    are moves that already happened.
    """
    print("Backfilling today's session...", flush=True)

    daily = fetch_daily(client, sorted(live.refs), day, feed)
    if not daily:
        print("  no bars yet today (market holiday, or before 04:00)\n")
        return

    survivors = [s for s, bar in daily.items() if prescreen(bar, live.refs[s], cfg)]
    print(f"  {len(daily)} traded, {len(survivors)} pre-screened", flush=True)
    if not survivors:
        print()
        return

    minutes = fetch_minutes(client, survivors, day, feed)
    stream = sorted((bar for bars in minutes.values() for bar in bars),
                    key=lambda b: b.timestamp)

    before = live.alerts_today
    for bar in stream:
        live.handle(bar, push=False)

    print(f"  {len(stream):,} bars, {live.alerts_today - before} candidate(s) "
          f"already logged\n", flush=True)


def run_test(live: LiveScanner, test_date: date) -> None:
    """Replay a cached day through the live code path.

    Same handler, same alerts, same journal — the only difference is where
    the bars come from. Use it to prove the chain works before a session
    you care about.
    """
    import pandas as pd

    path = pathlib.Path("data/bars") / f"{test_date.isoformat()}.parquet"
    if not path.exists():
        raise SystemExit(f"No cached bars for {test_date}. "
                         "Run tools.backtest over that date first.")

    frame = pd.read_parquet(path).sort_values("timestamp")
    print(f"Replaying {len(frame):,} bars from {test_date}\n")

    for row in frame.itertuples(index=False):
        if row.symbol not in live.refs:
            continue
        live.handle(Bar(row.symbol, row.timestamp.to_pydatetime().astimezone(ET),
                        row.open, row.high, row.low, row.close,
                        int(row.volume), row.vwap))

    print(f"\nReplay complete: {live.alerts_today} alerts from "
          f"{live.bars_seen:,} bars")


async def run_live(live: LiveScanner, key: str, secret: str,
                   feed_name: str = "sip") -> None:
    from alpaca.data.enums import DataFeed
    from alpaca.data.live import StockDataStream

    feed = DataFeed.SIP if feed_name == "sip" else DataFeed.IEX
    stream = StockDataStream(key, secret, feed=feed)

    async def on_bar(raw):
        bar = to_bar(raw)
        if bar and bar.symbol in live.refs:
            live.handle(bar)

    # Subscribe to every symbol's minute bars and filter in-process. The
    # universe is ~4,000 names and changes daily; a wildcard avoids
    # maintaining a subscription list and missing a name that gaps overnight.
    stream.subscribe_bars(on_bar, "*")

    print("Streaming. Ctrl-C to stop.")
    print("A heartbeat prints every 5 minutes once bars start arriving.")
    print("If you see no heartbeat during market hours, the stream is not "
          "delivering data.\n")

    async def watchdog():
        """Say something if nothing arrives at all — an idle socket during
        market hours is a failure, not a quiet market."""
        while True:
            await asyncio.sleep(600)
            if live.bars_seen == 0:
                now = datetime.now(ET)
                if time(9, 35) <= now.time() <= time(15, 55):
                    print(f"  [{now:%H:%M}] NO BARS RECEIVED since start. "
                          f"The subscription is not delivering. Restart, and "
                          f"check the Alpaca dashboard for an active session "
                          f"elsewhere — only one connection is allowed.",
                          file=sys.stderr, flush=True)

    watcher = asyncio.create_task(watchdog())
    try:
        await stream._run_forever()
    except asyncio.CancelledError:
        pass
    finally:
        watcher.cancel()
        # Close the socket before the loop tears down, or websockets raises
        # "no running event loop" from its own cleanup coroutine.
        try:
            await stream.stop_ws()
        except Exception:                                 # noqa: BLE001
            pass


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--test-date", default=None,
                   help="replay a cached day instead of streaming live")
    p.add_argument("--config", default="config/alerts.yaml")
    p.add_argument("--scanner-config", default="config/scanner.yaml")
    p.add_argument("--refs", default=None,
                   help="reference file; defaults to the newest in data/refs")
    p.add_argument("--feed", default="sip", choices=["sip", "iex"],
                   help="iex is free but ~3%% of market volume; use "
                        "config/scanner-iex.yaml and IEX-built refs with it")
    p.add_argument("--equity", type=float, default=50_000)
    p.add_argument("--no-backfill", action="store_true",
                   help="skip catching up on today's session. Only safe when "
                        "starting before 04:00 ET.")
    args = p.parse_args()

    cfg_path = pathlib.Path(args.config)
    alert_cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    scanner_cfg = yaml.safe_load(pathlib.Path(args.scanner_config).read_text())

    check_refs_are_current(args.refs)
    refs = load_refs_for(args.refs)
    print(f"\nReference     {args.refs or 'newest in data/refs'}")

    risk = RiskManager(RiskConfig(equity=args.equity))
    risk.start_session()

    notifier = Notifier(alert_cfg.get("alerts", {}))
    live = LiveScanner(Scanner(scanner_cfg, refs), refs, risk, notifier,
                       refs_date=refs_date_of(args.refs))

    print(f"Feed          {args.feed.upper()}"
          + ("   (~3% of market volume — thresholds must be IEX-calibrated)"
             if args.feed == "iex" else ""))
    print(f"Universe      {len(refs):,} symbols")
    print(f"Alerts        {notifier.channel}")
    print(f"Journal       {JOURNAL_DIR}/ (one file per session date)")
    print(f"Gap trigger   {scanner_cfg['gap']['min_pct_change']}% and "
          f"{scanner_cfg['gap']['min_rel_volume']}x relative volume")
    print(f"Velocity      {scanner_cfg['velocity']['min_pct_change']}% in "
          f"{scanner_cfg['velocity']['window_seconds']}s")
    print(f"Sessions      {', '.join(scanner_cfg['sessions']['tradeable'])}\n")

    # Push on start and stop, so silence is never ambiguous. A scanner that
    # was never running and a quiet market look identical from the phone —
    # that cost a full session once already.
    notifier.send(
        "Scanner started",
        f"{len(refs):,} symbols on {args.feed.upper()}\n"
        f"refs {refs_date_of(args.refs) or 'unknown'}\n"
        f"gap {scanner_cfg['gap']['min_pct_change']}% / "
        f"{scanner_cfg['gap']['min_rel_volume']}x, "
        f"velocity {scanner_cfg['velocity']['min_pct_change']}%",
        priority="low",
    )

    try:
        if args.test_date:
            run_test(live, date.fromisoformat(args.test_date))
        else:
            from alpaca.data.enums import DataFeed
            from alpaca.data.historical import StockHistoricalDataClient

            key, secret = load_credentials()
            feed = DataFeed.SIP if args.feed == "sip" else DataFeed.IEX

            now = datetime.now(ET)
            if not args.no_backfill and now.time() > time(4, 5):
                client = StockHistoricalDataClient(key, secret)
                backfill(live, client, scanner_cfg, feed, now.date())

            asyncio.run(run_live(live, key, secret, args.feed))
    except KeyboardInterrupt:
        pass                          # Ctrl-C is a normal way to stop this
    finally:
        print(f"\nStopped. {live.alerts_today} alerts logged this session.")
        notifier.send(
            "Scanner STOPPED",
            f"{live.alerts_today} alerts logged, "
            f"{live.bars_seen:,} bars seen.\n"
            f"Nothing is watching the market now.",
            priority="high",
        )
        live.close()

        # Exit without interpreter teardown. The websockets layer inside
        # alpaca-py runs a cleanup coroutine after the event loop is gone and
        # prints a long, harmless traceback. The journal is flushed on every
        # alert and closed above, so there is nothing left to lose by
        # skipping teardown — and a scary-looking traceback on every normal
        # stop trains you to ignore output that might one day matter.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


if __name__ == "__main__":
    main()
