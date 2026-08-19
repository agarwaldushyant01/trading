"""Parse NuntioBot mosquito scanner alerts from Discord.

Each alert is two lines:

    +  5.6 %  |  HAO  |  $  4.41  |  1m: 4,121    2m: 0    |
    5m:  6.1 k   1D:  2.0 M  |  F:  600 k  |  #  22

    -  34.2 %  |  FTRK  |  $  0.10  |  1m: 92,461    2m: 3,372  |
    5m: 95.8 k   1D:  6.4 M  |  F:  8.5 M  |  #  18  |  NLOD

Why this matters more than it looks: the feed already carries the two things
that were hardest to build. F is the real float, where our own pipeline only
had quarterly shares-outstanding from SEC filings — an approximation that
overstates float on exactly the low-float names being traded. And # is the
per-ticker alert count, which is the "seen it two or three times" rule
computed upstream.

Parsing is deliberately tolerant of whitespace: Discord renders these inside
code blocks and the column alignment shifts with value width.
"""

from __future__ import annotations

import re
from datetime import datetime

from engine.alerts import Alert

# Numbers arrive as 4,121 or 6.1 k or 27.1 M, and float may be blank (-----).
NUMBER = r"([\d,]+(?:\.\d+)?)\s*([kKmMbB])?"

LINE_ONE = re.compile(
    r"([+\-])\s*([\d.]+)\s*%"           # direction and percent
    r".*?\|\s*([A-Z]{1,6})\s*\|"        # ticker
    r"\s*\$\s*([\d.]+)"                 # price
    r".*?1m:\s*" + NUMBER +             # one-minute volume
    r".*?2m:\s*" + NUMBER,              # two-minute volume
    re.DOTALL,
)

LINE_TWO = re.compile(
    r"5m:\s*" + NUMBER +
    r".*?1D:\s*" + NUMBER +
    r".*?F:\s*(?:" + NUMBER + r"|(-+))"  # float, or dashes when unknown
    r".*?#\s*(\d+)"                      # alert count for this ticker
    r"(.*)$",                            # anything after is tags
    re.DOTALL,
)

SUFFIX = {None: 1, "k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6, "b": 1e9, "B": 1e9}


def _scale(value: str | None, suffix: str | None) -> float | None:
    if value is None:
        return None
    return float(value.replace(",", "")) * SUFFIX.get(suffix, 1)


def parse(text: str, received_at: datetime | None = None) -> Alert | None:
    """One alert block to a MosquitoAlert, or None if it does not match.

    Returns None rather than raising: the channel carries other messages,
    and a parser that crashes on a status line takes the whole feed down.
    """
    clean = text.replace("`", " ").replace("\u200b", "")

    one = LINE_ONE.search(clean)
    if not one:
        return None
    two = LINE_TWO.search(clean[one.end():])
    if not two:
        return None

    sign, pct, symbol, price, v1, s1, v2, s2 = one.groups()
    v5, s5, vd, sd, fl, sf, dashes, count, trailing = two.groups()

    tags = re.findall(r"\b([A-Z]{2,6})\b", trailing or "")

    return Alert(
        symbol=symbol,
        pct_change=float(pct) * (-1 if sign == "-" else 1),
        price=float(price),
        volume_1m=_scale(v1, s1) or 0.0,
        volume_2m=_scale(v2, s2) or 0.0,
        volume_5m=_scale(v5, s5) or 0.0,
        volume_1d=_scale(vd, sd) or 0.0,
        float_shares=None if dashes else _scale(fl, sf),
        alert_count=int(count),
        tags=tags,
        received_at=received_at,
    )


def parse_message(text: str, received_at: datetime | None = None) -> list:
    """A Discord message may hold several alert blocks. Return all of them.

    Blocks are split on the leading +/- percent marker, since the boxes have
    no other reliable delimiter once Discord's formatting is stripped.
    """
    blocks = re.split(r"\n(?=\s*[+\-]\s*\d)", text)
    found = [parse(block, received_at) for block in blocks]
    return [alert for alert in found if alert]
