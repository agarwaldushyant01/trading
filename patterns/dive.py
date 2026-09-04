"""Dumpster diving: the mini-bounce inside a collapse.

    A LAST RESORT, NOT A STRATEGY.

The trader used this on 2026-09-03 — four of eighteen trades, their most-used
setup that day — and was explicit about why: nothing was moving. Most stocks
were falling fast and there was no live tape to trade. On a normal day they
would not touch it.

So it is gated on market condition. Fewer than about ten names up 20% or more
by mid-morning means conditions are thin enough for this to run; otherwise it
stays off, because a setup built for a dead tape will lose money on a live one
where better trades exist.

THE SETUP

A stock down 50-70% on the day is falling hard enough that a bounce is
mechanical rather than fundamental: shorts take profit, bargain hunters step
in, and it retraces some of the fall before the final leg down. The trade is
that bounce — target around 20% — and nothing more. It is explicitly not a
reversal: the expectation is that it continues lower afterwards.

Which makes the exit unusually important. There is no trailing here and no
letting it run, because the move is expected to fail. Take the bounce and
leave.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DiveConfig:
    # The fall that qualifies. The trader's words: "if a stock has fallen by
    # 50% or 70% within a day that means that's a huge downfall."
    min_fall_pct: float = 45.0
    max_fall_pct: float = 90.0     # beyond this it may be halted or delisting

    # The bounce being caught.
    target_pct: float = 20.0
    stop_pct: float = 10.0

    # Confirmation that the fall has paused. Without this the entry is a
    # falling knife: down 60% is not a signal, down 60% and stabilising is.
    min_stall_bars: int = 3
    max_stall_range_pct: float = 8.0

    # Time limit. This is a bounce, not a hold — if it has not worked within
    # the hour the thesis is wrong and the final leg is coming.
    give_up_minutes: int = 60

    min_price: float = 0.10
    min_session_volume: float = 500_000


def fall_from_high(bars: list, index: int) -> float:
    """How far below the session high price has come, as a percentage."""
    high = max(b["h"] for b in bars[:index + 1])
    if high <= 0:
        return 0.0
    return (bars[index]["c"] / high - 1) * 100


def has_stalled(bars: list, index: int, cfg: DiveConfig) -> bool:
    """Has the decline paused?

    Measured as a tight range over the last few bars. A stock still making
    new lows every bar has not stopped falling, and buying it is catching the
    knife rather than the bounce.
    """
    if index < cfg.min_stall_bars:
        return False
    recent = bars[index - cfg.min_stall_bars + 1:index + 1]
    high = max(b["h"] for b in recent)
    low = min(b["l"] for b in recent)
    if low <= 0:
        return False

    if (high - low) / low * 100 > cfg.max_stall_range_pct:
        return False

    # And the current bar must not be the lowest — something has to have
    # bought it.
    return bars[index]["l"] > low * 0.999


def detect_dive(bars: list, index: int, cfg: DiveConfig | None = None
                ) -> dict | None:
    """A qualifying bounce entry, or None.

    Deliberately narrow. This runs only when the market is thin, and on those
    days almost everything is falling — without tight conditions it would fire
    on every red stock in the universe.
    """
    cfg = cfg or DiveConfig()
    if index < 10 or index >= len(bars):
        return None

    bar = bars[index]
    if bar["c"] < cfg.min_price:
        return None

    session_volume = sum(b["v"] for b in bars[:index + 1])
    if session_volume < cfg.min_session_volume:
        return None

    fall = fall_from_high(bars, index)
    if not (-cfg.max_fall_pct <= fall <= -cfg.min_fall_pct):
        return None

    if not has_stalled(bars, index, cfg):
        return None

    # The entry bar itself must be green — buyers present, not just absent
    # sellers.
    if bar["c"] <= bar["o"]:
        return None

    entry = bar["c"]
    return {
        "entry": entry,
        "stop": round(entry * (1 - cfg.stop_pct / 100), 4),
        "target": round(entry * (1 + cfg.target_pct / 100), 4),
        "fall_pct": round(fall, 1),
        "reason": f"down {abs(fall):.0f}% from the high, stalled, "
                  f"green bar",
    }


def simulate(bars: list, index: int, cfg: DiveConfig | None = None
             ) -> tuple[str, float] | None:
    """Outcome of a dive entry — for backtesting only.

    No trailing stop: the move is expected to fail, so the target is taken
    and the position closed.
    """
    cfg = cfg or DiveConfig()
    signal = detect_dive(bars, index, cfg)
    if signal is None:
        return None

    entry = signal["entry"]
    for i, b in enumerate(bars[index + 1:], start=1):
        if b["l"] <= signal["stop"]:
            return ("stop", -cfg.stop_pct)
        if b["h"] >= signal["target"]:
            return ("target", cfg.target_pct)
        if i * 5 >= cfg.give_up_minutes:
            return ("gave_up", (b["c"] / entry - 1) * 100)

    return ("close", (bars[-1]["c"] / entry - 1) * 100)
