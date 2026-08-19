"""One market alert, whatever produced it.

Shared by both paths: the Alpaca scanner converts its own candidates into
this shape, and the Discord parser produces it from a NuntioBot message. The
rules engine only ever sees this, so neither feed is special to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Alert:
    symbol: str
    pct_change: float                 # signed: -34.2 for a fall
    price: float
    volume_1m: float
    volume_2m: float
    volume_5m: float
    volume_1d: float
    float_shares: float | None        # None when the feed shows dashes
    alert_count: int                  # the # field — appearances so far
    tags: list = field(default_factory=list)
    received_at: datetime | None = None

    @property
    def rising(self) -> bool:
        return self.pct_change > 0

    @property
    def rel_volume_1m(self) -> float | None:
        """One-minute volume against the day's average minute.

        Rough, but it needs no reference data and no baseline history —
        everything comes from the message itself.
        """
        if not self.volume_1d:
            return None
        return self.volume_1m / (self.volume_1d / 390)

    @property
    def float_turnover(self) -> float | None:
        """Times the float has traded today. The number that separates a
        low-float name genuinely in play from one merely ticking up."""
        if not self.float_shares:
            return None
        return self.volume_1d / self.float_shares

    def to_row(self) -> dict:
        return {
            "symbol": self.symbol,
            "pct_change": self.pct_change,
            "price": self.price,
            "volume_1m": self.volume_1m,
            "volume_2m": self.volume_2m,
            "volume_5m": self.volume_5m,
            "volume_1d": self.volume_1d,
            "float_shares": self.float_shares,
            "alert_count": self.alert_count,
            "rel_volume_1m": self.rel_volume_1m,
            "float_turnover": self.float_turnover,
            "tags": ",".join(self.tags),
            "received_at": self.received_at.isoformat() if self.received_at else None,
        }
