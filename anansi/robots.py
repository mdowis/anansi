"""Async robots.txt cache — one RobotFileParser per domain origin, 1-hour TTL."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser


class RobotsCache:
    """
    Fetches and caches robots.txt per domain. Thread-safe for async use.

    On fetch error or non-200 response, assumes all URLs are allowed so
    scraping is not silently blocked by transient network issues.

    A per-origin lock provides single-flight: the first burst of concurrent
    requests to a domain fetches robots.txt once, and the rest await that result
    instead of each issuing an identical fetch. The cache is bounded so a crawl
    over many hosts does not grow it without limit.
    """

    _MAX_ENTRIES = 512

    def __init__(self, user_agent: str = "*", ttl: float = 3600.0) -> None:
        self._cache: dict[str, tuple[RobotFileParser, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._ua = user_agent
        self._ttl = ttl

    def _origin_lock(self, origin: str) -> asyncio.Lock:
        lock = self._locks.get(origin)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[origin] = lock
        return lock

    def _store(self, origin: str, rp: RobotFileParser, now: float) -> None:
        self._cache[origin] = (rp, now + self._ttl)
        if len(self._cache) <= self._MAX_ENTRIES:
            return
        # Drop expired entries first, then the soonest-to-expire, until under cap.
        for key in [k for k, (_, exp) in self._cache.items() if exp <= now]:
            self._cache.pop(key, None)
            self._locks.pop(key, None)
        while len(self._cache) > self._MAX_ENTRIES:
            oldest = min(self._cache, key=lambda k: self._cache[k][1])
            self._cache.pop(oldest, None)
            self._locks.pop(oldest, None)

    async def allowed(self, url: str) -> bool:
        """Return True if robots.txt permits fetching *url*."""
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        now = time.monotonic()

        cached = self._cache.get(origin)
        if cached and now < cached[1]:
            return cached[0].can_fetch(self._ua, url)

        async with self._origin_lock(origin):
            # Re-check: another coroutine may have fetched while we waited.
            now = time.monotonic()
            cached = self._cache.get(origin)
            if cached and now < cached[1]:
                return cached[0].can_fetch(self._ua, url)

            robots_url = urljoin(origin, "/robots.txt")
            rp = RobotFileParser(robots_url)
            try:
                from anansi.fetchers.http import HTTPFetcher
                async with HTTPFetcher(timeout=10.0, max_retries=1) as f:
                    result = await f.fetch(robots_url)
                if result.status == 200:
                    rp.parse(result.html.splitlines())
            except Exception:
                pass  # assume allowed if robots.txt is unreachable

            self._store(origin, rp, now)
            return rp.can_fetch(self._ua, url)

    async def crawl_delay(self, url: str) -> float | None:
        """Return the Crawl-delay for url's domain from robots.txt, or None."""
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        now = time.monotonic()
        cached = self._cache.get(origin)
        if not (cached and now < cached[1]):
            await self.allowed(url)  # populates cache as side effect
            cached = self._cache.get(origin)
        if not cached:
            return None
        rp = cached[0]
        delay = rp.crawl_delay(self._ua)
        if delay is None:
            delay = rp.crawl_delay("*")  # fall back to wildcard agent
        return float(delay) if delay is not None else None
