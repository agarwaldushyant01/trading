"""The buy/sell rules, applied to mosquito alerts.

Every threshold lives in config/mosquito.yaml. These are a first pass built
from how you described trading — they are meant to be argued with and
changed, not trusted.

What the feed gives us that nothing else did:

  Real float, not quarterly shares-outstanding. The SEC number overstates
  float badly on exactly these names.

  Volume at 1m/2m/5m/1D. Velocity without reconstructing minute bars.

  The # counter — appearances so far, which is the "seen it two or three
  times" rule already computed.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.alerts import Alert


@dataclass(frozen=True)
class Decision:
    take: bool
    setup: str = ""
    reason: str = ""
    stop_pct: float = 0.0
    target_pct: float = 0.0


def _universe_ok(alert: Alert, cfg: dict) -> str | None:
    """Returns a rejection reason, or None if the name is tradeable."""
    u = cfg["universe"]

    # Ceiling on how far the move has already run. Over 146 live candidates,
    # winners had a LOWER median percent change at alert than losers (13.4%
    # vs 18.9%) and lower relative volume (68x vs 89x). Buying a name already
    # up 50-150% is buying the end of the move, and the data says so.
    #
    # Two sessions is not enough to be sure of this. It is a hypothesis being
    # tested forward, and the nightly study measures both sides of it.
    if u.get("max_pct_change") and alert.pct_change > u["max_pct_change"]:
        return f"already up {alert.pct_change:.0f}%"
    if (u.get("max_rel_volume_1m") and alert.rel_volume_1m
            and alert.rel_volume_1m > u["max_rel_volume_1m"]):
        return f"rel volume {alert.rel_volume_1m:.0f}x"

    if not (u["min_price"] <= alert.price <= u["max_price"]):
        return f"price {alert.price}"
    # Unknown float is common and not disqualifying — most foreign filers
    # have none in SEC quarterly data. Only reject a float we can see and
    # that is too large.
    if alert.float_shares and alert.float_shares > u["max_float"]:
        return f"float {alert.float_shares / 1e6:.1f}M"
    if alert.volume_1d < u["min_daily_volume"]:
        return f"daily volume {alert.volume_1d / 1e6:.2f}M"
    return None


def decide(alert: Alert, cfg: dict) -> Decision:
    """One alert in, one decision out. Setups are checked most-specific first.

    Deliberately conservative: an alert that matches nothing is skipped, and
    the skip reason is journalled. Reading the rejection reasons after a week
    of paper trading is how these thresholds get fixed.
    """
    rejected = _universe_ok(alert, cfg)
    if rejected:
        return Decision(False, reason=rejected)

    if not alert.rising:
        return Decision(False, reason="falling")

    turnover = alert.float_turnover or 0
    rel_1m = alert.rel_volume_1m or 0

    # --- Setup A: vertical move on heavy one-minute volume ----------------
    # Your premarket spike. Not the cumulative gain but the rate: a minute
    # trading many multiples of normal, on a name whose whole float is
    # turning over.
    # When float is unknown, turnover cannot be computed. Require it only
    # where it is available rather than rejecting the whole setup.
    spike = cfg["spike"]
    turnover_ok = lambda need: alert.float_shares is None or turnover >= need

    if (alert.pct_change >= spike["min_pct_change"]
            and rel_1m >= spike["min_rel_volume_1m"]
            and turnover_ok(spike["min_float_turnover"])):
        return Decision(
            True, "spike",
            f"{alert.pct_change:+.1f}%, {rel_1m:.0f}x minute volume, "
            f"float turned {turnover:.1f}x",
            spike["stop_pct"], spike["target_pct"],
        )

    # --- Setup B: a name that keeps coming back ---------------------------
    # Your "seen it two or three times" rule, using the feed's own counter.
    repeat = cfg["repeat"]
    if (alert.alert_count >= repeat["min_alert_count"]
            and alert.pct_change >= repeat["min_pct_change"]
            and turnover_ok(repeat["min_float_turnover"])):
        return Decision(
            True, "repeat",
            f"appearance #{alert.alert_count}, {alert.pct_change:+.1f}%, "
            f"float turned {turnover:.1f}x",
            repeat["stop_pct"], repeat["target_pct"],
        )

    # --- Setup C: new session high with volume behind it ------------------
    # NSH is the feed's own tag. Breaking to a new high on volume is a
    # different event from drifting up.
    high = cfg["new_high"]
    if ("NSH" in alert.tags
            and alert.pct_change >= high["min_pct_change"]
            and rel_1m >= high["min_rel_volume_1m"]):
        return Decision(
            True, "new_high",
            f"new session high, {alert.pct_change:+.1f}%, "
            f"{rel_1m:.0f}x minute volume",
            high["stop_pct"], high["target_pct"],
        )

    return Decision(False, reason="no setup matched")
