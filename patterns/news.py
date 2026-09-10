"""Fresh-news lookup for the pattern detector.

News catalysts are the trader's highest-expectancy setup — five logged
catalyst trades averaged +57% — and the detector had no representation for
them at all. This adds the one thing the detector can honestly know: whether
a symbol has a headline in the recent past. It does not read the headline or
judge whether it matters. A fresh catalyst is wired in as a fifth confluence
that raises a qualifying pattern's grade; it never turns a near-miss into a
trade on its own (see patterns/confluence.py).

WHY THERE IS A CACHE

The scanner watches ~13,000 symbols and re-runs the detector on every
five-minute bar. A news request per symbol per bar would be tens of thousands
of calls an hour. Each symbol's news is fetched at most once per
`cache_seconds` — under one bar by default — and reused. The driver only asks
about a symbol once a pattern has actually formed on it, so in practice the
number of requests per session is small.

THE CLOCK IS INJECTED

`fresh(symbol)` with no argument asks "is there news as of now"; replay and
tests pass their own `as_of`. Every "last N minutes" is measured against the
injected clock, never `datetime.now()` — the same rule as PaperTrader.clock,
and for the same reason: the bugs in this project have been about what the
code does at 04:00 and at midnight, not about its inputs. Overnight news is
exactly the catalyst case, so nothing here depends on market hours.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


class NewsFeed:
    """Recent headlines per symbol, cached, from Alpaca's news endpoint.

    The endpoint is part of the market-data API and needs no subscription
    beyond the one already in use. A failure to reach it is not fatal: the
    lookup returns "no news" and the pattern is graded without the fifth
    confluence, exactly as it was before this existed.
    """

    def __init__(self, client=None, *, key: str | None = None,
                 secret: str | None = None, clock=None,
                 lookback_minutes: float = 120.0,
                 cache_seconds: float = 180.0) -> None:
        self._client = client
        self._key = key
        self._secret = secret
        self.clock = clock or (lambda: datetime.now(ET))
        self.lookback = timedelta(minutes=lookback_minutes)
        self.cache_seconds = cache_seconds
        # symbol -> (fetched_at, [article, ...])
        self._cache: dict[str, tuple[datetime, list]] = {}
        self._warned = False

    # -- lazy client, so importing this module needs no credentials --------

    def _get_client(self):
        if self._client is None:
            from alpaca.data.historical.news import NewsClient

            key, secret = self._key, self._secret
            if not (key and secret):
                from data.reference import load_credentials

                key, secret = load_credentials()
            self._client = NewsClient(key, secret)
        return self._client

    # -- fetch + cache ---------------------------------------------------

    def _fetch(self, symbol: str) -> list:
        """Articles for one symbol over the lookback window, newest first.

        Returns [] on any failure — a news outage must never stop trading,
        because news only ever raises a grade here, it never gates.
        """
        from alpaca.data.requests import NewsRequest

        now = self._utc(self.clock())
        try:
            resp = self._get_client().get_news(NewsRequest(
                symbols=symbol,
                start=now - self.lookback,
                end=now,
                sort="desc",
                limit=50,
                exclude_contentless=False,
            ))
            articles = [
                {
                    "headline": a.headline,
                    "created_at": self._utc(a.created_at),
                    "source": a.source,
                    "url": a.url,
                }
                for a in resp.data.get("news", [])
            ]
            articles.sort(key=lambda a: a["created_at"], reverse=True)
            return articles
        except Exception as exc:                          # noqa: BLE001
            if not self._warned:
                print(f"  news lookup failed ({exc}); patterns will grade "
                      f"without it", file=sys.stderr, flush=True)
                self._warned = True
            return []

    def _cached(self, symbol: str) -> list:
        now = self.clock()
        hit = self._cache.get(symbol)
        if hit is not None:
            fetched_at, articles = hit
            if 0 <= (now - fetched_at).total_seconds() < self.cache_seconds:
                return articles
        articles = self._fetch(symbol)
        self._cache[symbol] = (now, articles)
        return articles

    # -- public --------------------------------------------------------

    def prime(self, symbols) -> None:
        """Warm the cache for a known short-list, one request per symbol.

        Optional. The driver relies on the lazy per-symbol path; this is for
        a caller that already knows which handful of names it cares about.
        """
        for symbol in symbols:
            self._cached(symbol)

    def headlines(self, symbol: str, as_of: datetime | None = None) -> list:
        """Articles for `symbol` in the `lookback` window ending at `as_of`
        (default: now).

        The upper bound matters in replay: walking the session bar by bar,
        an article published later in the day must not read as "fresh" at an
        earlier bar. Live, `as_of` is now and the bound is a no-op.

        Each article is a dict with headline, created_at (tz-aware UTC),
        source, url.
        """
        end = self._utc(as_of or self.clock())
        cutoff = end - self.lookback
        return [a for a in self._cached(symbol)
                if cutoff <= a["created_at"] <= end]

    def fresh(self, symbol: str, as_of: datetime | None = None) -> bool:
        """Is there at least one headline in the last N minutes as of `as_of`?"""
        return bool(self.headlines(symbol, as_of))

    # -- helpers ------------------------------------------------------

    @staticmethod
    def _utc(dt: datetime) -> datetime:
        """Normalise to tz-aware UTC. Alpaca returns UTC; a naive value is
        assumed UTC rather than guessed at."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
