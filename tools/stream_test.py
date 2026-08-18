"""Is the websocket delivering? — a verbose isolation test.

    python -m tools.stream_test                 # a few liquid names
    python -m tools.stream_test --wildcard      # every symbol, as live.py does
    python -m tools.stream_test --quiet         # without the protocol log

Prints what the server actually says: authentication, subscription
confirmations, and errors. Silence from a websocket is ambiguous, and the
first version of this tool hid the one thing that would have explained it.

Closes the connection properly on exit. Alpaca allows exactly one concurrent
connection, and a session killed without a close handshake can hold that slot
for a while afterwards — which looks identical to "no data".
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from data.reference import load_credentials

ET = ZoneInfo("America/New_York")
DEFAULT_SYMBOLS = ["SPY", "AAPL", "TSLA", "NVDA", "AMD"]


def enable_protocol_log() -> None:
    """Surface alpaca-py's own messages: auth results, subscription
    acknowledgements, and disconnect reasons."""
    logging.basicConfig(
        level=logging.DEBUG,
        format="  %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    for noisy in ("asyncio", "urllib3", "websockets.client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def listen(key: str, secret: str, symbols, feed_name: str,
                 seconds: int) -> int:
    from alpaca.data.enums import DataFeed
    from alpaca.data.live import StockDataStream

    feed = DataFeed.SIP if feed_name == "sip" else DataFeed.IEX
    stream = StockDataStream(key, secret, feed=feed)
    received = {"count": 0}

    async def on_bar(bar):
        received["count"] += 1
        if received["count"] <= 10:
            ts = bar.timestamp.astimezone(ET)
            print(f"  BAR {ts:%H:%M:%S}  {bar.symbol:<6} {bar.close:>9.2f} "
                  f"vol {bar.volume:>10,}", flush=True)

    stream.subscribe_bars(on_bar, *symbols)
    task = asyncio.create_task(stream._run_forever())

    for elapsed in range(seconds):
        await asyncio.sleep(1)
        if elapsed and elapsed % 20 == 0:
            print(f"  [{elapsed}s] {received['count']} bars so far", flush=True)

    task.cancel()
    try:
        await stream.stop_ws()
    except Exception:                                     # noqa: BLE001
        pass
    await asyncio.sleep(1)                # let the close handshake complete
    return received["count"]


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--wildcard", action="store_true")
    p.add_argument("--feed", default="iex", choices=["sip", "iex"])
    p.add_argument("--seconds", type=int, default=90)
    p.add_argument("--quiet", action="store_true",
                   help="hide the protocol log")
    args = p.parse_args()

    if not args.quiet:
        enable_protocol_log()

    symbols = ["*"] if args.wildcard else DEFAULT_SYMBOLS
    now = datetime.now(ET)

    print(f"\nFeed       {args.feed.upper()}")
    print(f"Symbols    {'* (all)' if args.wildcard else ', '.join(symbols)}")
    print(f"Time       {now:%H:%M} ET")
    print(f"Listening for {args.seconds}s. Watch for 'authenticated' and a")
    print(f"subscription acknowledgement below.\n")

    key, secret = load_credentials()
    print(f"  key starts {key[:6]}..., {len(key)} chars\n")

    count = asyncio.run(listen(key, secret, symbols, args.feed, args.seconds))

    print(f"\n{count} bars received.")
    if count == 0:
        print("""
  Read the log above for which of these it is:

    "not authenticated" / 401     -> credentials. Paper keys work for data,
                                     but they must be the current pair.
    "connection limit exceeded"   -> another session holds the one slot.
                                     Run: pkill -f 'drivers.live|stream_test'
                                     then wait 30s and retry.
    "insufficient subscription"   -> asking for a feed the plan does not have.
    authenticated, subscribed,
    then nothing                  -> the connection is healthy and IEX simply
                                     has no prints for these symbols right
                                     now. Retry after 09:31.
""")
    else:
        print("  Streaming works. Silence in the live scanner is then a")
        print("  threshold question, not a plumbing one.\n")


if __name__ == "__main__":
    main()
