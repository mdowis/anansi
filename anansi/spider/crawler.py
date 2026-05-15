"""
Crawler — orchestrates concurrent spider sessions with pause/resume.

Architecture:
- One asyncio.Semaphore gates concurrency across all worker tasks
- A pause Event lets any external caller freeze the crawl cleanly
- SQLiteQueue persists URL state so crawls survive process restarts
- Proxy rotation is delegated to ProxyManager
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import inspect
import io
import json
import logging
import random
import time
import uuid
from collections import defaultdict, deque
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlparse

from anansi.core import Item, Request, Response
from anansi.db import DATA_DIR, crawl_db
from anansi.fetchers.base import BaseFetcher, FetchResult
from anansi.fetchers.http import HTTPFetcher
from anansi import security
from anansi.security import (
    UnsafeURLError,
    escape_csv_cell,
    is_url_safe_for_public_fetch,
    same_registrable_domain,
)
from anansi.spider.queue import SQLiteQueue

logger = logging.getLogger(__name__)

# Hard upper bound on a site-supplied robots.txt Crawl-delay value. A hostile
# (or misconfigured) robots.txt could otherwise stall a worker for hours.
_MAX_ROBOTS_CRAWL_DELAY = 300.0


class _LRUDefault(dict):
    """A defaultdict-with-LRU-eviction. Missing-key reads insert the default
    factory's value; every access promotes the entry; the dict caps at
    ``max_entries`` and drops the least-recently-used.
    """

    def __init__(self, default_factory, *, max_entries: int) -> None:
        super().__init__()
        self._factory = default_factory
        self._max = max_entries
        self._order: deque = deque()

    def __missing__(self, key):
        value = self._factory()
        # Insert via __setitem__ so eviction logic runs.
        self.__setitem__(key, value)
        return value

    def __getitem__(self, key):
        if super().__contains__(key):
            # Promote to most-recently-used.
            try:
                self._order.remove(key)
            except ValueError:
                pass
            self._order.append(key)
            return super().__getitem__(key)
        return self.__missing__(key)

    def __setitem__(self, key, value) -> None:
        existed = super().__contains__(key)
        super().__setitem__(key, value)
        if existed:
            try:
                self._order.remove(key)
            except ValueError:
                pass
        self._order.append(key)
        while len(self._order) > self._max:
            oldest = self._order.popleft()
            super().pop(oldest, None)

    def get(self, key, default=None):  # type: ignore[override]
        if super().__contains__(key):
            return self.__getitem__(key)
        return default


def _url_passes_domain_scope(url: str, spider: Any) -> bool:
    """Return False if url is blocked by the spider's allowed_domains or deny_patterns."""
    netloc = urlparse(url).netloc
    allowed = getattr(spider, "allowed_domains", [])
    deny = getattr(spider, "deny_patterns", [])
    if allowed and not any(netloc == d or netloc.endswith(f".{d}") for d in allowed):
        return False
    if deny:
        import re as _re
        if any(_re.search(pat, url) for pat in deny):
            return False
    return True


class _AdaptiveDomainThrottle:
    """Per-domain rate limiter with sliding-window error tracking and circuit breaker.

    Algorithm:
    - Tracks the last 20 results per domain (bool: is_error).
    - 429  → gap doubles immediately (cap 60 s) + 30 s circuit-breaker pause.
    - 5xx  → if error rate > 30% in window, gap × 1.5.
    - Clean → if error rate < 5% and window is full, gap × 0.95 toward base.

    The per-domain state dicts are LRU-bounded so a crawl that hits 10M unique
    hosts (potentially driven by an attacker steering links/sitemap entries)
    does not leak memory in proportion to the host count.
    """

    _WINDOW = 20
    _MAX_GAP = 60.0
    _CB_SLEEP = 30.0
    _ERROR_THRESHOLD = 0.30
    _CLEAN_THRESHOLD = 0.05
    # Maximum number of distinct domains tracked in any of the per-domain
    # state maps before the least-recently-used entry is evicted.
    _MAX_DOMAINS = 4096

    def __init__(self, base_gap: float = 1.0, enabled: bool = True) -> None:
        self._base_gap = base_gap
        self._enabled = enabled
        # LRU-bounded defaultdicts: missing-key reads return the default and
        # also insert it, but the maps cap entries at _MAX_DOMAINS so a crawl
        # against millions of unique hosts cannot leak memory in proportion.
        self._gaps: _LRUDefault[str, float] = _LRUDefault(
            lambda: base_gap, max_entries=self._MAX_DOMAINS
        )
        self._last: _LRUDefault[str, float] = _LRUDefault(
            lambda: 0.0, max_entries=self._MAX_DOMAINS
        )
        self._locks: _LRUDefault[str, asyncio.Lock] = _LRUDefault(
            asyncio.Lock, max_entries=self._MAX_DOMAINS
        )
        self._windows: _LRUDefault[str, deque] = _LRUDefault(
            lambda: deque(maxlen=self._WINDOW), max_entries=self._MAX_DOMAINS
        )
        self._cb_until: _LRUDefault[str, float] = _LRUDefault(
            lambda: 0.0, max_entries=self._MAX_DOMAINS
        )

    def _lock(self, domain: str) -> asyncio.Lock:
        return self._locks[domain]

    def _window(self, domain: str) -> deque:
        return self._windows[domain]

    async def wait(self, url: str) -> None:
        if not self._enabled:
            return
        domain = urlparse(url).netloc
        async with self._lock(domain):
            remaining_cb = self._cb_until[domain] - time.monotonic()
            if remaining_cb > 0:
                logger.debug("Circuit breaker active for %s — sleeping %.1fs", domain, remaining_cb)
                await asyncio.sleep(remaining_cb)
            gap = self._gaps[domain]
            elapsed = time.monotonic() - self._last[domain]
            if elapsed < gap:
                await asyncio.sleep(gap - elapsed)
            self._last[domain] = time.monotonic()

    async def record_result(self, url: str, status: int) -> None:
        """Record a fetch outcome and adjust the per-domain gap."""
        if not self._enabled:
            return
        domain = urlparse(url).netloc
        async with self._lock(domain):
            window = self._window(domain)
            window.append(status >= 500 or status == 0)

            if status == 429:
                new_gap = min(self._gaps[domain] * 2, self._MAX_GAP)
                logger.info(
                    "429 on %s — gap %.1fs → %.1fs + circuit breaker %.0fs",
                    domain, self._gaps[domain], new_gap, self._CB_SLEEP,
                )
                self._gaps[domain] = new_gap
                self._cb_until[domain] = time.monotonic() + self._CB_SLEEP
                return

            if len(window) < self._WINDOW:
                return

            error_rate = sum(window) / len(window)
            if status >= 500 and error_rate > self._ERROR_THRESHOLD:
                new_gap = min(self._gaps[domain] * 1.5, self._MAX_GAP)
                logger.info(
                    "High error rate on %s (%.0f%%) — gap %.1fs → %.1fs",
                    domain, error_rate * 100, self._gaps[domain], new_gap,
                )
                self._gaps[domain] = new_gap
            elif error_rate < self._CLEAN_THRESHOLD:
                new_gap = max(self._base_gap, self._gaps[domain] * 0.95)
                if new_gap < self._gaps[domain]:
                    logger.debug("Clean window on %s — gap %.2fs → %.2fs", domain, self._gaps[domain], new_gap)
                self._gaps[domain] = new_gap


class _DomainCircuitBreaker:
    """Per-domain circuit breaker: opens after N consecutive failures, recovers after a cooldown."""

    _FAIL_THRESHOLD = 5
    _OPEN_DURATION = 300.0   # seconds before half-open probe
    _HALF_OPEN_PROBE = 1     # allow this many probes before requiring success

    def __init__(self) -> None:
        self._streak: dict[str, int] = defaultdict(int)
        self._opened_at: dict[str, float] = {}
        self._probes: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    def _domain(self, url: str) -> str:
        return urlparse(url).netloc

    async def is_open(self, url: str) -> bool:
        domain = self._domain(url)
        async with self._lock:
            opened = self._opened_at.get(domain)
            if opened is None:
                return False
            age = time.monotonic() - opened
            if age >= self._OPEN_DURATION:
                # Half-open: allow one probe
                if self._probes[domain] < self._HALF_OPEN_PROBE:
                    self._probes[domain] += 1
                    return False
            return True

    async def record_success(self, url: str) -> None:
        domain = self._domain(url)
        async with self._lock:
            self._streak[domain] = 0
            self._opened_at.pop(domain, None)
            self._probes[domain] = 0

    async def record_failure(self, url: str) -> None:
        domain = self._domain(url)
        async with self._lock:
            self._streak[domain] += 1
            if self._streak[domain] >= self._FAIL_THRESHOLD and domain not in self._opened_at:
                logger.warning(
                    "Circuit breaker opened for %s after %d consecutive failures",
                    domain, self._streak[domain],
                )
                self._opened_at[domain] = time.monotonic()


class CrawlState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    ERROR = "error"


class Crawler:
    """
    Multi-session web crawler with pause/resume and proxy rotation.

    Example::

        crawler = Crawler(
            MySpider,
            concurrency=10,
            delay=0.5,
            max_pages=500,
        )
        async for item in crawler.run():
            print(item.data)

        # Pause from another coroutine:
        crawler.pause()

        # Later resume:
        async for item in crawler.resume():
            print(item.data)
    """

    def __init__(
        self,
        spider_class: type,
        *,
        concurrency: int = 5,
        delay: float = 1.0,
        delay_jitter: float = 0.5,
        max_pages: int | None = None,
        max_depth: int | None = None,
        max_depth_per_domain: int | None = None,
        max_duration_seconds: float | None = None,
        fetcher: BaseFetcher | None = None,
        proxy_manager: Any | None = None,
        crawl_id: str | None = None,
        db_path: Path | None = None,
        max_url_retries: int = 3,
        domain_delay: float = 1.0,
        respect_robots: bool = True,
        cookies: dict[str, str] | None = None,
        auth_headers: dict[str, str] | None = None,
        credential_scope_host: str | None = None,
        deduplicate_content: bool = False,
        adaptive_rate_limiting: bool = True,
        conditional_get: bool = True,
        auto_browser: bool = True,
        canonicalize_urls: bool = True,
        worker_timeout: float = 120.0,
        impersonate: str | None = None,
    ) -> None:
        self._impersonate = impersonate
        self._spider_cls = spider_class
        self._concurrency = concurrency
        self._delay = delay
        self._delay_jitter = delay_jitter
        self._max_pages = max_pages
        self._max_depth = max_depth
        self._max_depth_per_domain = max_depth_per_domain
        self._max_duration_seconds = max_duration_seconds
        self._fetcher = fetcher
        self._proxy_manager = proxy_manager
        self._crawl_id = crawl_id or str(uuid.uuid4())
        self._db_path = db_path or DATA_DIR / "crawls.db"
        self._max_url_retries = max_url_retries
        self._domain_throttle = _AdaptiveDomainThrottle(domain_delay, enabled=adaptive_rate_limiting)
        self._circuit_breaker = _DomainCircuitBreaker()
        self._cookies = cookies or {}
        self._auth_headers = auth_headers or {}
        # When set, cookies/auth_headers are only attached to requests whose
        # host shares this registrable domain. ``None`` means forward to all
        # hosts (legacy behaviour; used by library callers who handle their
        # own scoping).
        self._credential_scope_host = credential_scope_host
        self._deduplicate_content = deduplicate_content
        self._conditional_get = conditional_get
        self._auto_browser = auto_browser
        self._canonicalize_urls = canonicalize_urls
        self._worker_timeout = worker_timeout
        self._unchanged_pages = 0
        # LRU-bounded per-domain caches. See _AdaptiveDomainThrottle for the
        # same pattern — a crawl that touches millions of unique hosts cannot
        # be allowed to leak memory proportional to the host count.
        from collections import OrderedDict
        self._domain_needs_browser: OrderedDict[str, bool] = OrderedDict()
        self._domain_crawl_delays: OrderedDict[str, float] = OrderedDict()
        # One HTTPFetcher per host so its cookie jar (Akamai _abck / bm_sz /
        # ak_bmsc, etc.) survives across requests in the crawl. A fresh fetcher
        # per request would make every request "cold" and behaviorally
        # blocked. LRU-bounded like the other per-domain caches.
        self._host_fetchers: OrderedDict[str, Any] = OrderedDict()
        self._max_tracked_domains = 4096
        self._valid_items = 0
        self._invalid_items = 0
        # Adaptive concurrency: sliding window of last 50 outcomes (True = error)
        self._outcome_window: deque = deque(maxlen=50)
        self._current_concurrency = concurrency
        self._robots: Any | None = None
        if respect_robots:
            from anansi.robots import RobotsCache
            self._robots = RobotsCache()

        self._semaphore: asyncio.Semaphore | None = None
        self._pause_event = asyncio.Event()   # set = paused
        self._stop_event = asyncio.Event()
        self._cancel_event = asyncio.Event()  # permanent stop (cancel_crawl)
        self._finished = False
        self._items_count = 0
        self._pages_fetched = 0
        self._start_time: float = 0.0

    @property
    def crawl_id(self) -> str:
        return self._crawl_id

    @property
    def state(self) -> CrawlState:
        if self._cancel_event.is_set():
            return CrawlState.CANCELLED
        if self._finished:
            return CrawlState.FINISHED
        if self._stop_event.is_set():
            return CrawlState.FINISHED
        if self._pause_event.is_set():
            return CrawlState.PAUSED
        return CrawlState.RUNNING

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def cancel(self) -> None:
        """Permanently cancel the crawl (unlike stop(), marks state as cancelled)."""
        self._cancel_event.set()
        self._stop_event.set()
        logger.info("Crawl %s cancelled", self._crawl_id)

    async def run(self) -> AsyncIterator[Item]:
        """Start the crawl and yield items as they are extracted."""
        spider = self._spider_cls()
        queue = SQLiteQueue(self._crawl_id, self._db_path, canonicalize=self._canonicalize_urls)
        self._semaphore = asyncio.Semaphore(self._concurrency)

        # Crawl row must exist before any url_queue inserts (FK constraint)
        await self._upsert_crawl(spider.name, CrawlState.RUNNING)

        # Recover any URLs stuck in 'processing' from a previous run
        recovered = await queue.requeue_stale()
        if recovered:
            logger.info("Requeued %d stale URLs from previous session", recovered)

        # Seed initial URLs
        pending = await queue.pending_count()
        if pending == 0:
            async for req in spider.start_requests(db_path=self._db_path):
                await queue.push(req.url, callback=req.callback or "parse", meta=req.meta)

        item_queue: asyncio.Queue[Item | None] = asyncio.Queue()
        worker_tasks: list[asyncio.Task] = []
        self._start_time = time.monotonic()

        async def dispatcher():
            while not self._stop_event.is_set():
                if self._pause_event.is_set():
                    await asyncio.sleep(0.5)
                    continue

                if self._max_pages and self._pages_fetched >= self._max_pages:
                    break

                if self._max_duration_seconds and (
                    time.monotonic() - self._start_time > self._max_duration_seconds
                ):
                    logger.info(
                        "Crawl %s reached max_duration_seconds=%.1f — stopping",
                        self._crawl_id, self._max_duration_seconds,
                    )
                    break

                entry = await queue.pop()
                if entry is None:
                    # Check if workers are still running before giving up
                    active = sum(1 for t in worker_tasks if not t.done())
                    if active == 0:
                        break
                    await asyncio.sleep(0.5)
                    continue

                url, callback, meta = entry

                if await queue.is_visited(url):
                    await queue.mark_done(url)
                    continue

                task = asyncio.create_task(
                    asyncio.wait_for(
                        self._fetch_and_parse(
                            spider, queue, item_queue, url, callback, meta
                        ),
                        timeout=self._worker_timeout,
                    )
                )
                worker_tasks.append(task)
                # Prune completed tasks to prevent unbounded list growth
                if len(worker_tasks) > self._concurrency * 4:
                    worker_tasks[:] = [t for t in worker_tasks if not t.done()]

            # Signal no more items
            await item_queue.put(None)

        dispatcher_task = asyncio.create_task(dispatcher())

        try:
            while True:
                item = await item_queue.get()
                if item is None:
                    break
                self._items_count += 1
                yield item
        finally:
            self._finished = True
            dispatcher_task.cancel()
            # Drain in-flight workers (graceful shutdown: wait up to 30 s)
            if worker_tasks:
                done, pending = await asyncio.wait(
                    worker_tasks, timeout=30.0, return_when=asyncio.ALL_COMPLETED
                )
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.gather(dispatcher_task, return_exceptions=True)

            # Close per-host fetchers (and their httpx clients).
            for _hf in self._host_fetchers.values():
                try:
                    await _hf.close()
                except Exception:
                    pass
            self._host_fetchers.clear()

            # Requeue any URLs left in 'processing' state after abnormal exit
            queue2 = SQLiteQueue(self._crawl_id, self._db_path, canonicalize=self._canonicalize_urls)
            await queue2.requeue_stale()

            final_state = CrawlState.CANCELLED if self._cancel_event.is_set() else CrawlState.FINISHED
            await self._upsert_crawl(spider.name, final_state)
            logger.info(
                "Crawl %s %s: %d pages, %d items",
                self._crawl_id,
                final_state.value,
                self._pages_fetched,
                self._items_count,
            )

    def pause(self) -> None:
        """Pause the crawl after the current in-flight requests finish."""
        self._pause_event.set()
        logger.info("Crawl %s paused", self._crawl_id)

    def resume_in_place(self) -> None:
        """Unpause a previously paused crawl (same process)."""
        self._pause_event.clear()
        logger.info("Crawl %s resumed", self._crawl_id)

    def stop(self) -> None:
        """Signal the crawl to stop; in-flight requests finish before shutdown."""
        self._stop_event.set()

    @classmethod
    async def resume(
        cls,
        crawl_id: str,
        spider_class: type,
        *,
        db_path: Path | None = None,
        **kwargs: Any,
    ) -> "Crawler":
        """
        Create a Crawler that continues an existing crawl from its saved state.

        The SQLite queue already holds the pending/visited URLs, so the new
        instance picks up exactly where it left off.
        """
        inst = cls(spider_class, crawl_id=crawl_id, db_path=db_path, **kwargs)
        await inst._upsert_crawl(spider_class.name, CrawlState.RUNNING)
        return inst

    # ── Worker ────────────────────────────────────────────────────────────────

    async def _fetch_and_parse(
        self,
        spider: Any,
        queue: SQLiteQueue,
        item_queue: asyncio.Queue,
        url: str,
        callback: str,
        meta: dict[str, Any],
    ) -> None:
        assert self._semaphore is not None
        current_depth = meta.get("depth", 0)
        proxy: str | None = None

        # Depth gate: determine whether outgoing links should be followed
        _follow_links = self._max_depth is None or current_depth < self._max_depth
        if _follow_links and self._max_depth_per_domain is not None:
            _follow_links = current_depth < self._max_depth_per_domain

        async with self._semaphore:
            # Refuse to fetch URLs that target a non-public address. The MCP
            # entry validated start_url once; here we catch URLs steered into
            # the queue by links in attacker-controlled response HTML or by
            # entries pulled from sitemaps.
            try:
                is_url_safe_for_public_fetch(
                    url, allow_private=security.ALLOW_PRIVATE_NETWORKS
                )
            except UnsafeURLError as exc:
                logger.warning("refusing unsafe URL %s: %s", url, exc)
                await queue.mark_failed(url)
                return

            # Circuit breaker: skip domains that are persistently failing
            if await self._circuit_breaker.is_open(url):
                logger.warning("Circuit breaker open for %s — skipping", url)
                await queue.increment_retry(url, self._max_url_retries)
                return

            # Check robots.txt before spending time on this URL
            if self._robots and not await self._robots.allowed(url):
                logger.debug("robots.txt disallows %s — skipping", url)
                await queue.mark_done(url)
                return

            # Apply robots.txt Crawl-delay to domain throttle gap (once per domain)
            domain = urlparse(url).netloc
            if self._robots and domain not in self._domain_crawl_delays:
                robots_delay = await self._robots.crawl_delay(url)
                if robots_delay is not None and robots_delay > _MAX_ROBOTS_CRAWL_DELAY:
                    logger.warning(
                        "robots.txt Crawl-delay %.1fs for %s exceeds cap — clamping to %.1fs",
                        robots_delay, domain, _MAX_ROBOTS_CRAWL_DELAY,
                    )
                    robots_delay = _MAX_ROBOTS_CRAWL_DELAY
                self._domain_crawl_delays[domain] = robots_delay or 0.0
                while len(self._domain_crawl_delays) > self._max_tracked_domains:
                    self._domain_crawl_delays.popitem(last=False)
                if robots_delay and robots_delay > self._domain_throttle._gaps.get(domain, 0.0):
                    logger.info(
                        "robots.txt Crawl-delay %.1fs for %s — overriding domain gap",
                        robots_delay, domain,
                    )
                    self._domain_throttle._gaps[domain] = robots_delay

            try:
                # Per-domain rate limiting + jittered polite delay
                await self._domain_throttle.wait(url)
                jitter = random.uniform(0, self._delay_jitter)
                await asyncio.sleep(self._delay + jitter)

                proxy = self._proxy_manager.next() if self._proxy_manager else None
                result = await self._do_fetch(url, proxy=proxy, meta=meta)

                # 401/403 = IP-level auth block — rotate proxy but don't penalise it
                if result.status in (401, 403) and self._proxy_manager and proxy:
                    logger.info(
                        "Auth failure %d at %s — rotating proxy without penalty", result.status, url
                    )
                    await queue.increment_retry(url, self._max_url_retries)
                    await self._domain_throttle.record_result(url, result.status)
                    return

                if proxy and result.ok:
                    self._proxy_manager.report_success(proxy)
                elif proxy and result.status >= 500:
                    self._proxy_manager.report_failure(proxy)

                # Circuit breaker accounting
                if result.ok or result.status in (301, 302, 304):
                    await self._circuit_breaker.record_success(url)
                elif result.status >= 500 or result.status == 0:
                    await self._circuit_breaker.record_failure(url)

                # Adaptive concurrency: track error rate over last 50 fetches
                is_error = not result.ok and result.status not in (301, 302, 304)
                self._outcome_window.append(is_error)
                if len(self._outcome_window) >= 20:
                    error_rate = sum(self._outcome_window) / len(self._outcome_window)
                    if error_rate > 0.40 and self._current_concurrency > 1:
                        self._current_concurrency -= 1
                        logger.info(
                            "Adaptive concurrency: high error rate %.0f%% — reducing to %d",
                            error_rate * 100, self._current_concurrency,
                        )
                    elif error_rate < 0.10 and self._current_concurrency < self._concurrency:
                        self._current_concurrency += 1
                        logger.debug(
                            "Adaptive concurrency: low error rate %.0f%% — restoring to %d",
                            error_rate * 100, self._current_concurrency,
                        )

                # Adaptive rate limiting: record outcome for this domain (E2)
                await self._domain_throttle.record_result(url, result.status)

                # 304 Not Modified — server confirmed page unchanged (E4)
                if result.status == 304:
                    self._unchanged_pages += 1
                    logger.debug("304 Not Modified: %s (unchanged_pages=%d)", url, self._unchanged_pages)
                    await queue.mark_visited(url)
                    await queue.mark_done(url)
                    self._pages_fetched += 1
                    return

                # Content hash check for 200 responses with no ETag support (E4)
                if self._conditional_get and result.ok:
                    new_hash = hashlib.md5(result.html.encode()).hexdigest()
                    cached = await self._get_url_cache(url)
                    if cached and cached.get("content_hash") == new_hash:
                        self._unchanged_pages += 1
                        logger.debug("Content hash unchanged: %s", url)
                        await self._update_url_cache(
                            url,
                            etag=result.headers.get("etag"),
                            last_modified=result.headers.get("last-modified"),
                            content_hash=new_hash,
                        )
                        await queue.mark_visited(url)
                        await queue.mark_done(url)
                        self._pages_fetched += 1
                        return
                    if result.ok:
                        await self._update_url_cache(
                            url,
                            etag=result.headers.get("etag"),
                            last_modified=result.headers.get("last-modified"),
                            content_hash=hashlib.md5(result.html.encode()).hexdigest(),
                        )

                # Content deduplication (opt-in)
                if self._deduplicate_content:
                    content_hash = hashlib.md5(result.html.encode()).hexdigest()
                    if await queue.is_content_seen(content_hash):
                        logger.debug("Duplicate content at %s — skipping", url)
                        await queue.mark_visited(url)
                        await queue.mark_done(url)
                        return
                    await queue.mark_visited(url)
                    await queue.mark_content_seen(url, content_hash)
                else:
                    await queue.mark_visited(url)
                await queue.mark_done(url)
                self._pages_fetched += 1

                response = Response(
                    url=result.url,
                    status=result.status,
                    html=result.html,
                    headers=result.headers,
                    meta=meta,
                    elapsed=result.elapsed,
                    via_browser=result.via_browser,
                    spa_state=result.spa_state,
                )

                handler = getattr(spider, callback, None)
                if handler is None:
                    logger.warning("Spider has no callback '%s'", callback)
                    return

                gen = handler(response)
                if inspect.isasyncgen(gen):
                    async for obj in gen:
                        if isinstance(obj, Item):
                            obj = self._validate_item(obj, spider)
                            await item_queue.put(obj)
                            await self._persist_item(obj)
                        elif isinstance(obj, Request):
                            if not _url_passes_domain_scope(obj.url, spider):
                                logger.debug("Skipping out-of-scope URL from parse(): %s", obj.url)
                                continue
                            if _follow_links and not await queue.is_visited(obj.url):
                                child_meta = {
                                    **(obj.meta or {}),
                                    "depth": current_depth + 1,
                                    "referer": url,
                                }
                                await queue.push(
                                    obj.url,
                                    callback=obj.callback or "parse",
                                    priority=obj.priority,
                                    meta=child_meta,
                                )

                # Also apply @rule link following
                if _follow_links:
                    for req in spider.follow_links(response):
                        if not await queue.is_visited(req.url):
                            child_meta = {
                                **(req.meta or {}),
                                "depth": current_depth + 1,
                                "referer": url,
                            }
                            await queue.push(req.url, callback=req.callback or "parse", meta=child_meta)

            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.warning("Worker timed out after %.0fs for %s", self._worker_timeout, url)
                await queue.increment_retry(url, self._max_url_retries)
                await self._circuit_breaker.record_failure(url)
                self._outcome_window.append(True)
                if self._proxy_manager and proxy:
                    self._proxy_manager.report_failure(proxy)
            except Exception as exc:
                logger.exception("Error fetching %s: %s", url, exc)
                await queue.increment_retry(url, self._max_url_retries)
                await self._circuit_breaker.record_failure(url)
                self._outcome_window.append(True)
                if self._proxy_manager and proxy:
                    self._proxy_manager.report_failure(proxy)

    async def _get_host_fetcher(self, url: str, cookies: dict[str, str]):
        """Return the per-host HTTPFetcher, creating (and warming up) it on
        first contact so the cookie jar persists across the crawl."""
        from urllib.parse import urlparse

        host = urlparse(url).netloc
        existing = self._host_fetchers.get(host)
        if existing is not None:
            self._host_fetchers.move_to_end(host)
            return existing

        fetcher = HTTPFetcher(cookies=cookies, impersonate=self._impersonate)
        self._host_fetchers[host] = fetcher
        # LRU-evict, closing the evicted fetcher's client.
        while len(self._host_fetchers) > self._max_tracked_domains:
            _, evicted = self._host_fetchers.popitem(last=False)
            try:
                await evicted.close()
            except Exception:
                pass

        # Origin warm-up: a first GET to scheme://host/ to mint behavioral
        # cookies (_abck / bm_sz / ak_bmsc) before the real request. Evasion
        # behavior — skipped under the operator kill-switch. Best-effort: a
        # failed warm-up must not fail the real fetch.
        if not security.DISABLE_ANTIBOT:
            parsed = urlparse(url)
            origin = f"{parsed.scheme}://{parsed.netloc}/"
            try:
                is_url_safe_for_public_fetch(
                    origin, allow_private=security.ALLOW_PRIVATE_NETWORKS
                )
                await fetcher.fetch(origin)
                logger.debug("Warmed up session for host %s", host)
            except Exception as exc:
                logger.debug("Warm-up GET for %s failed (ignored): %s", origin, exc)
        return fetcher

    def _handoff_browser_cookies(self, url: str, result: FetchResult) -> None:
        """Merge cookies a browser fetch earned (e.g. a validated Akamai
        ``_abck``) into the per-host HTTP fetcher so subsequent fast HTTP
        requests inherit the solved session. Evasion behavior — gated."""
        if security.DISABLE_ANTIBOT or self._fetcher is not None:
            return
        cookies = getattr(result, "cookies", None)
        if not cookies:
            return
        from urllib.parse import urlparse

        host = urlparse(url).netloc
        fetcher = self._host_fetchers.get(host)
        if fetcher is None:
            fetcher = HTTPFetcher(
                cookies=self._cookies, impersonate=self._impersonate
            )
            self._host_fetchers[host] = fetcher
        fetcher._session_cookies.update(cookies)

    async def _do_fetch(
        self, url: str, *, proxy: str | None, meta: dict[str, Any]
    ) -> FetchResult:
        use_browser = meta.get("use_browser", False)

        # Explicit browser flag — skip all auto-detection logic
        if use_browser:
            from anansi.fetchers.browser import BrowserFetcher
            fetcher = self._fetcher if isinstance(self._fetcher, BrowserFetcher) else BrowserFetcher()
            result = await fetcher.fetch(url, proxy=proxy)
            self._handoff_browser_cookies(url, result)
            return result

        domain = urlparse(url).netloc

        # Auto-browser: domain previously identified as JS-rendered (E1)
        if self._auto_browser and self._domain_needs_browser.get(domain):
            from anansi.fetchers.browser import BrowserFetcher
            logger.debug("Domain %s cached as JS-rendered — using BrowserFetcher", domain)
            bf = BrowserFetcher()
            result = await bf.fetch(url, proxy=proxy)
            self._handoff_browser_cookies(url, result)
            return result

        # Credential scoping: strip cookies/auth_headers when the request host
        # does not share the configured scope host's registrable domain.
        target_host = urlparse(url).hostname or ""
        in_scope = (
            self._credential_scope_host is None
            or same_registrable_domain(target_host, self._credential_scope_host)
        )

        # Build conditional GET headers from cache (E4)
        extra_headers: dict[str, str] = (
            dict(self._auth_headers) if self._auth_headers and in_scope else {}
        )
        if self._conditional_get:
            cached = await self._get_url_cache(url)
            if cached:
                if cached.get("etag"):
                    extra_headers["If-None-Match"] = cached["etag"]
                if cached.get("last_modified"):
                    extra_headers["If-Modified-Since"] = cached["last_modified"]

        cookies_for_request = self._cookies if in_scope else {}
        if self._fetcher is not None:
            fetcher = self._fetcher
        else:
            fetcher = await self._get_host_fetcher(url, cookies_for_request)
        # Referer continuity: the link-graph parent (set when the child was
        # enqueued) — server/crawler-derived, not client free-text.
        referer = meta.get("referer") if in_scope else None
        result = await fetcher.fetch(
            url, proxy=proxy, headers=extra_headers or None, referer=referer
        )

        # Graduated Akamai escalation (rung 1: impersonated retry through a
        # freshly-warmed per-host session; rung 2: headless browser, which
        # can run the sensor JS). Detection runs even under DISABLE_ANTIBOT
        # but escalation does not. Proxy rotation remains the crawler's
        # existing last rung (the 401/403 handler upstream).
        from anansi.fetchers.escalate import DEFAULT_IMPERSONATE, escalate_akamai
        from anansi.fetchers.smart import detect_akamai_block

        if detect_akamai_block(result.html, result.status, result.headers):
            async def _retry_impersonated():
                # Clear the stale per-host jar so the retry re-warms and
                # re-mints behavioral cookies under an impersonated session.
                if self._fetcher is None:
                    old = self._host_fetchers.pop(domain, None)
                    if old is not None:
                        try:
                            await old.close()
                        except Exception:
                            pass
                    target = self._impersonate or DEFAULT_IMPERSONATE
                    f2 = HTTPFetcher(
                        cookies=cookies_for_request, impersonate=target
                    )
                    self._host_fetchers[domain] = f2
                    if not security.DISABLE_ANTIBOT:
                        from urllib.parse import urlparse as _up
                        p = _up(url)
                        origin = f"{p.scheme}://{p.netloc}/"
                        try:
                            is_url_safe_for_public_fetch(
                                origin,
                                allow_private=security.ALLOW_PRIVATE_NETWORKS,
                            )
                            await f2.fetch(origin)
                        except Exception:
                            pass
                else:
                    f2 = fetcher
                return await f2.fetch(
                    url, proxy=proxy, headers=extra_headers or None,
                    referer=referer,
                )

            async def _browser_fetch():
                from anansi.fetchers.browser import BrowserFetcher
                bf = BrowserFetcher()
                return await bf.fetch(url, proxy=proxy)

            result = await escalate_akamai(
                url=url,
                initial=result,
                retry_impersonated=_retry_impersonated,
                browser_fetch=_browser_fetch,
                disable_antibot=security.DISABLE_ANTIBOT,
            )
            if result.via_browser:
                self._handoff_browser_cookies(url, result)
            return result

        # Auto-browser: detect JS shell and retry with browser (E1)
        if self._auto_browser and result.ok and not self._domain_needs_browser.get(domain):
            from anansi.fetchers.smart import needs_browser
            if needs_browser(result.html):
                logger.info(
                    "JS shell detected at %s — upgrading domain '%s' to BrowserFetcher",
                    url, domain,
                )
                self._domain_needs_browser[domain] = True
                while len(self._domain_needs_browser) > self._max_tracked_domains:
                    self._domain_needs_browser.popitem(last=False)
                from anansi.fetchers.browser import BrowserFetcher
                bf = BrowserFetcher()
                browser_result = await bf.fetch(url, proxy=proxy)
                self._handoff_browser_cookies(url, browser_result)
                return browser_result

        return result

    # ── DB helpers ────────────────────────────────────────────────────────────

    async def _upsert_crawl(self, spider_name: str, state: CrawlState) -> None:
        async with crawl_db(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO crawls (crawl_id, spider_name, state, items_count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(crawl_id) DO UPDATE SET
                    state       = excluded.state,
                    items_count = excluded.items_count,
                    updated_at  = datetime('now')
                """,
                (self._crawl_id, spider_name, state.value, self._items_count),
            )
            await db.commit()

    @staticmethod
    async def list_crawls(db_path: Path | None = None) -> list[dict[str, Any]]:
        async with crawl_db(db_path or DATA_DIR / "crawls.db") as db:
            rows = await db.execute_fetchall(
                """
                SELECT crawl_id, spider_name, state, items_count, created_at, updated_at
                FROM crawls
                ORDER BY updated_at DESC
                """
            )
        return [dict(r) for r in rows]

    async def _persist_item(self, item: Item) -> None:
        async with crawl_db(self._db_path) as db:
            await db.execute(
                "INSERT INTO items (crawl_id, source_url, spider_name, data) VALUES (?,?,?,?)",
                (self._crawl_id, item.source_url, item.spider_name, json.dumps(item.data)),
            )
            await db.commit()

    async def _get_url_cache(self, url: str) -> dict[str, Any] | None:
        """Return cached ETag/Last-Modified/content_hash for *url*, or None."""
        async with crawl_db(self._db_path) as db:
            rows = await db.execute_fetchall(
                "SELECT etag, last_modified, content_hash FROM url_cache WHERE url = ?",
                (url,),
            )
        return dict(rows[0]) if rows else None

    async def _update_url_cache(
        self,
        url: str,
        *,
        etag: str | None,
        last_modified: str | None,
        content_hash: str,
    ) -> None:
        """Upsert url_cache row for *url*."""
        async with crawl_db(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO url_cache (url, etag, last_modified, content_hash, last_fetched)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    etag          = excluded.etag,
                    last_modified = excluded.last_modified,
                    content_hash  = excluded.content_hash,
                    last_fetched  = excluded.last_fetched
                """,
                (url, etag, last_modified, content_hash, time.time()),
            )
            await db.commit()

    def _validate_item(self, item: Item, spider: Any) -> Item:
        """Validate and coerce *item* against the spider's item_schema (if any).

        On success, item.data is replaced with the schema-coerced dict (e.g.
        str "49.99" becomes float 49.99). On failure, a ``_validation_errors``
        key is added to item.data and the item is still persisted so no data
        is silently lost.
        """
        schema = getattr(spider, "item_schema", None)
        if schema is None:
            self._valid_items += 1
            return item
        try:
            validated = schema.model_validate(item.data)
            item.data = validated.model_dump()
            self._valid_items += 1
        except Exception as exc:
            self._invalid_items += 1
            logger.warning("Item validation failed on %s: %s", item.source_url, exc)
            item.data["_validation_errors"] = str(exc)
        return item

    @staticmethod
    async def get_items(
        crawl_id: str,
        limit: int = 100,
        offset: int = 0,
        db_path: Path | None = None,
    ) -> list[dict[str, Any]]:
        """Return persisted items for a crawl."""
        async with crawl_db(db_path or DATA_DIR / "crawls.db") as db:
            rows = await db.execute_fetchall(
                """
                SELECT id, source_url, spider_name, data, created_at
                FROM items
                WHERE crawl_id = ?
                ORDER BY id ASC
                LIMIT ? OFFSET ?
                """,
                (crawl_id, limit, offset),
            )
        return [
            {**dict(r), "data": json.loads(r["data"])}
            for r in rows
        ]

    @staticmethod
    async def export_items(
        crawl_id: str,
        fmt: str = "jsonl",
        path: str | None = None,
        db_path: Path | None = None,
    ) -> str:
        """
        Export all items for a crawl as JSONL, JSON, or CSV.

        Returns the file path if *path* is given, otherwise the serialised string.
        """
        rows = []
        offset = 0
        batch = 500
        while True:
            chunk = await Crawler.get_items(crawl_id, limit=batch, offset=offset, db_path=db_path)
            if not chunk:
                break
            rows.extend(chunk)
            offset += batch

        if fmt == "jsonl":
            out = "\n".join(json.dumps(r["data"]) for r in rows)
        elif fmt == "json":
            out = json.dumps([r["data"] for r in rows], indent=2)
        elif fmt == "csv":
            if not rows:
                out = ""
            else:
                # Collect all keys across all items
                all_keys: list[str] = []
                seen: set[str] = set()
                for r in rows:
                    for k in r["data"].keys():
                        if k not in seen:
                            all_keys.append(k)
                            seen.add(k)
                buf = io.StringIO()
                writer = csv.DictWriter(buf, fieldnames=all_keys, extrasaction="ignore")
                writer.writeheader()
                for r in rows:
                    serialized = {}
                    for k, v in r["data"].items():
                        if v is None:
                            serialized[k] = ""
                        elif isinstance(v, (dict, list)):
                            serialized[k] = json.dumps(v, ensure_ascii=False)
                        else:
                            serialized[k] = v
                    # Defuse spreadsheet-formula prefixes (=, +, -, @, tab, CR)
                    # so that opening the CSV in Excel/Sheets/Calc does not
                    # execute scraped-content as a formula.
                    serialized = {k: escape_csv_cell(v) for k, v in serialized.items()}
                    writer.writerow(serialized)
                out = buf.getvalue()
        else:
            raise ValueError(f"Unknown format {fmt!r}. Use 'jsonl', 'json', or 'csv'.")

        if path:
            target = Path(path)
            target.write_text(out, encoding="utf-8")
            # Restrict the export file to the owner; the default umask of 022
            # would leave it world-readable, which is a leak on shared hosts.
            try:
                import os
                os.chmod(target, 0o600)
            except OSError:
                pass
            return path
        return out
