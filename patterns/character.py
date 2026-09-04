"""What kind of stock is this? Judged from its history, not today's chart.

Every one of the trader's logged passes on 2026-09-03 was about the name's
past behaviour rather than the setup in front of them:

    "has huge pump and dump price action previously"
    "have lost on it previously"
    "falls rapidly on dumps"
    "previous price action was unexpected"

None of that is visible in a five-minute chart of today, and the pattern
detector had no concept of it. A textbook ascending triangle on a stock that
has spiked and fully retraced six times this quarter is not the same trade as
the identical shape on a name that holds its gains — and the trader will not
touch the first one.

This scores three things from daily bars:

  PUMP AND DUMP  how often a large spike was fully given back within days.
                 A name that does this repeatedly will do it again.

  FALL SPEED     how violently it drops once it turns. "Falls rapidly on
                 dumps" is the difference between a stop that fills near its
                 level and one that gaps through it.

  FOLLOW THROUGH after a big up day, does it hold or fade? A stock that
                 fades every rally offers nothing to a momentum entry.

The output is advisory, not a veto — a low score should shrink position size
or raise the confluence bar rather than block the trade outright, because
these measures are noisy on thin history.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean


@dataclass
class Character:
    pump_dumps: int = 0            # spikes fully retraced within the window
    spikes: int = 0                # large up days seen at all
    avg_fall_pct: float = 0.0      # typical decline after a spike
    follow_through: float = 0.0    # share of spikes that held
    sessions: int = 0
    verdict: str = "unknown"
    reasons: list = None

    def __post_init__(self):
        if self.reasons is None:
            self.reasons = []

    @property
    def dump_rate(self) -> float:
        return self.pump_dumps / self.spikes if self.spikes else 0.0

    @property
    def tradeable(self) -> bool:
        return self.verdict in ("clean", "unknown")


def analyse(daily: list, spike_pct: float = 25.0,
            retrace_share: float = 0.8, window: int = 5) -> Character:
    """Score a stock's behaviour from its daily bars.

    A pump-and-dump is a day up spike_pct or more, where the following few
    sessions give back retrace_share of the gain. That is the shape the
    trader means: it runs, everyone who bought it late is trapped, and it
    returns to where it started.
    """
    if len(daily) < 20:
        return Character(sessions=len(daily), verdict="unknown",
                         reasons=["not enough history"])

    spikes = 0
    dumps = 0
    falls = []
    held = 0

    for i in range(1, len(daily) - window):
        prev_close = daily[i - 1]["c"]
        if prev_close <= 0:
            continue
        gain = (daily[i]["c"] / prev_close - 1) * 100
        if gain < spike_pct:
            continue

        spikes += 1
        base = prev_close
        peak = daily[i]["c"]
        after = daily[i + 1:i + 1 + window]
        trough = min(b["l"] for b in after)

        given_back = (peak - trough) / (peak - base) if peak > base else 0
        falls.append((trough / peak - 1) * 100)

        if given_back >= retrace_share:
            dumps += 1
        else:
            held += 1

    c = Character(
        pump_dumps=dumps, spikes=spikes,
        avg_fall_pct=mean(falls) if falls else 0.0,
        follow_through=held / spikes if spikes else 0.0,
        sessions=len(daily),
    )

    if spikes < 2:
        c.verdict = "unknown"
        c.reasons.append(f"only {spikes} spike(s) in {len(daily)} sessions")
        return c

    # Thresholds calibrated against the six names the trader passed on for
    # reasons of history on 2026-09-03. At the original 70% bar, AEHL and
    # DFNS (both 4 of 8 spikes retraced) and GSUN (2 of 5) all scored
    # "clean" — while the trader described AEHL and DFNS as having "huge
    # pump and dump price action previously".
    #
    # A stock that gives back half its spikes is one you lose on half the
    # time. 40% is the bar those three sit above, and "clean" now requires a
    # clear majority of spikes to have held rather than a bare half.
    if c.dump_rate >= 0.4:
        c.verdict = "pump and dump"
        c.reasons.append(f"{dumps} of {spikes} spikes fully retraced")
    elif c.avg_fall_pct <= -45:
        c.verdict = "falls hard"
        c.reasons.append(f"average fall after a spike {c.avg_fall_pct:.0f}%")
    elif c.follow_through >= 0.7:
        c.verdict = "clean"
        c.reasons.append(f"{held} of {spikes} spikes held their gains")
    else:
        c.verdict = "mixed"
        c.reasons.append(f"{dumps} of {spikes} retraced, "
                         f"average fall {c.avg_fall_pct:.0f}%")

    return c


def market_is_thin(movers: int, threshold: int = 10) -> bool:
    """Is anything actually moving today?

    The trader's own test: fewer than about ten names up 20% or more by
    mid-morning means conditions are thin, and the setups that need a live
    tape will not appear. Used to gate the dumpster-diving fallback, which
    should never run on a normal day.
    """
    return movers < threshold
