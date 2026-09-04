"""Alert delivery.

Default is ntfy.sh: no account, no API key. Pick an unguessable topic name,
subscribe to it in the ntfy app on your phone, and anything posted to that
topic arrives as a push notification.

The topic name is the only secret, so make it long and random. Anyone who
knows it can read your alerts.
"""

from __future__ import annotations

import json
import sys

import requests

def _latin1_safe(text: str) -> str:
    """Return text that can go in an HTTP header.

    ntfy puts the title and message in headers, which are Latin-1 only. A
    single em-dash silently killed every notification on 2026-09-04.
    """
    if not isinstance(text, str):
        return text
    swaps = {"\u2014": "-", "\u2013": "-", "\u2019": "'", "\u2018": "'",
             "\u201c": '"', "\u201d": '"', "\u2026": "...",
             "\u00b7": "-", "\u2192": "->"}
    for bad, good in swaps.items():
        text = text.replace(bad, good)
    return text.encode("latin-1", "ignore").decode("latin-1")


class Notifier:
    def __init__(self, cfg: dict) -> None:
        self.channel = cfg.get("channel", "console")
        self.ntfy_topic = cfg.get("ntfy_topic", "")
        self.webhook_url = cfg.get("webhook_url", "")
        self.timeout = cfg.get("timeout_seconds", 5)

    def send(self, title: str, body: str, priority: str = "default") -> bool:
        """Never raises. A failed notification must not stop the scanner —
        the journal is the record of truth, the push is a convenience."""

        title = _latin1_safe(title)

        body = _latin1_safe(body)
        try:
            if self.channel == "ntfy" and self.ntfy_topic:
                requests.post(
                    f"https://ntfy.sh/{self.ntfy_topic}",
                    data=body.encode("utf-8"),
                    headers={"Title": title, "Priority": priority,
                             "Tags": "chart_with_upwards_trend"},
                    timeout=self.timeout,
                )
            elif self.channel == "webhook" and self.webhook_url:
                requests.post(
                    self.webhook_url,
                    json={"title": title, "body": body, "priority": priority},
                    timeout=self.timeout,
                )
            else:
                print(f"\n[{title}]\n{body}", flush=True)
            return True
        except Exception as exc:                          # noqa: BLE001
            print(f"  alert delivery failed: {exc}", file=sys.stderr)
            return False


def format_candidate(candidate, ref, sizing=None) -> tuple[str, str]:
    """What the phone shows. Short enough to read on a lock screen."""
    direction = "PREMKT" if candidate.session.value == "premarket" else "OPEN"
    title = (f"{candidate.symbol}  {candidate.pct_change_from_prior_close:+.0f}%  "
             f"{candidate.mode.value.upper()}")

    lines = [
        f"${candidate.price:.2f}   {direction} {candidate.timestamp:%H:%M}",
        f"rel vol {candidate.rel_volume:.1f}x   "
        f"{'above' if candidate.above_vwap else 'below'} VWAP",
        f"{candidate.pct_off_high:+.0f}% off HOD",
        f"float ~{ref.shares_outstanding / 1e6:.1f}M   "
        f"ADV {ref.avg_20d_volume / 1e6:.1f}M",
    ]
    if candidate.appearances_10d > 1:
        lines.append(f"seen {candidate.appearances_10d}x in 10 sessions")

    if sizing and sizing.allowed:
        lines.append(
            f"ref size {sizing.shares:,} sh (${sizing.notional:,.0f}) "
            f"stop {sizing.stop_price:.2f}"
        )

    return title, "\n".join(lines)
