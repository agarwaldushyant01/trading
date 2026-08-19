"""Phase 1 — collect everything the mosquito scanner emits.

    python -m engine.collect                    # listen live
    python -m engine.collect --backfill 30      # pull the last 30 days first

Records every alert to data/mosquito/YYYY-MM-DD.jsonl. Makes no trading
decisions and sends no notifications. The point is to accumulate the raw
feed so Phase 2 has something to be built against.

Why collect before deciding: the six-month Alpaca backtest could only ask
"what if you took every alert", and it could not see delisted names at all.
This feed has neither problem — it is the actual scanner output, recorded as
it arrives, with real float attached. A month of it is worth more than a
year of reconstructed history.

Setup:
  1. Create a Discord application at discord.com/developers/applications
  2. Bot tab -> Reset Token -> copy into .env as DISCORD_BOT_TOKEN
  3. Same page -> enable MESSAGE CONTENT INTENT (without it, messages
     arrive empty and this silently records nothing)
  4. OAuth2 -> URL Generator -> scope "bot", permissions "Read Messages"
     and "Read Message History" -> open the URL, add to your server
  5. Right-click your #mosquito channel -> Copy Channel ID
     (needs Developer Mode: Settings -> Advanced -> Developer Mode)
     -> put it in .env as MOSQUITO_CHANNEL_ID
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from engine.parser import parse_message

ET = ZoneInfo("America/New_York")
OUT_DIR = pathlib.Path("data/mosquito")


class Journal:
    """One file per session date, opened lazily and flushed per write.

    Flushing every alert costs nothing at this volume and means a crash or a
    dropped connection cannot lose the day.
    """

    def __init__(self) -> None:
        self.handle = None
        self.day = None
        self.written = 0
        self.symbols = Counter()

    def write(self, row: dict, when: datetime) -> None:
        day = when.astimezone(ET).date()
        if self.day != day:
            if self.handle:
                self.handle.close()
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            self.handle = (OUT_DIR / f"{day.isoformat()}.jsonl").open(
                "a", encoding="utf-8")
            self.day = day
        self.handle.write(json.dumps(row) + "\n")
        self.handle.flush()
        self.written += 1
        self.symbols[row["symbol"]] += 1

    def close(self) -> None:
        if self.handle:
            self.handle.close()


def load_env() -> tuple[str, int]:
    env = pathlib.Path(".env")
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel = os.environ.get("MOSQUITO_CHANNEL_ID")
    if not token or not channel:
        raise SystemExit(
            "Missing credentials. Add to .env:\n"
            "  DISCORD_BOT_TOKEN=your_bot_token\n"
            "  MOSQUITO_CHANNEL_ID=the_channel_id"
        )
    return token, int(channel)


def main() -> None:
    import argparse

    import discord

    p = argparse.ArgumentParser()
    p.add_argument("--backfill", type=int, default=0,
                   help="days of channel history to pull before listening")
    p.add_argument("--backfill-only", action="store_true")
    args = p.parse_args()

    token, channel_id = load_env()
    journal = Journal()

    # message_content is privileged and off by default. Without it every
    # message arrives with an empty body and this records nothing at all,
    # while appearing to work perfectly.
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    def record(message) -> int:
        alerts = parse_message(message.content, message.created_at)
        for alert in alerts:
            row = alert.to_row()
            row["message_id"] = str(message.id)
            journal.write(row, message.created_at)
        return len(alerts)

    @client.event
    async def on_ready():
        channel = client.get_channel(channel_id)
        if channel is None:
            print(f"Channel {channel_id} not visible. Check the ID, and that "
                  f"the bot has View Channel permission there.", file=sys.stderr)
            await client.close()
            return

        print(f"Connected as {client.user}")
        print(f"Watching #{channel.name} in {channel.guild.name}")
        print(f"Journal: {OUT_DIR}/\n")

        if args.backfill:
            since = datetime.now(timezone.utc) - timedelta(days=args.backfill)
            print(f"Backfilling {args.backfill} days...", flush=True)
            count = messages = 0
            async for message in channel.history(limit=None, after=since,
                                                 oldest_first=True):
                messages += 1
                count += record(message)
                if messages % 500 == 0:
                    print(f"  {messages:,} messages, {count:,} alerts",
                          flush=True)
            print(f"  done: {messages:,} messages, {count:,} alerts\n")

            if args.backfill_only:
                await client.close()
                return

        print("Listening. Ctrl-C to stop.\n", flush=True)

    @client.event
    async def on_message(message):
        if message.channel.id != channel_id:
            return
        found = record(message)
        if found and journal.written % 50 == 0:
            top = ", ".join(f"{s}({n})" for s, n in journal.symbols.most_common(3))
            print(f"  {datetime.now(ET):%H:%M}  {journal.written:,} alerts "
                  f"logged, most frequent: {top}", flush=True)

    try:
        client.run(token, log_handler=None)
    except KeyboardInterrupt:
        pass
    finally:
        print(f"\nStopped. {journal.written:,} alerts across "
              f"{len(journal.symbols)} symbols.")
        journal.close()


if __name__ == "__main__":
    main()
