"""Tests for RobotsCache.crawl_delay() and crawler integration."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from anansi.robots import RobotsCache


_ROBOTS_WITH_DELAY = """\
User-agent: *
Crawl-delay: 5
Disallow: /private/
"""

_ROBOTS_NO_DELAY = """\
User-agent: *
Disallow: /private/
"""

_ROBOTS_AGENT_SPECIFIC = """\
User-agent: mybot
Crawl-delay: 3

User-agent: *
Crawl-delay: 10
"""


@pytest.fixture
def robots_mock():
    with respx.mock:
        yield


async def test_crawl_delay_returns_float(robots_mock) -> None:
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text=_ROBOTS_WITH_DELAY)
    )
    cache = RobotsCache(user_agent="*")
    delay = await cache.crawl_delay("https://example.com/page")
    assert delay == 5.0


async def test_crawl_delay_no_directive_returns_none(robots_mock) -> None:
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text=_ROBOTS_NO_DELAY)
    )
    cache = RobotsCache(user_agent="*")
    delay = await cache.crawl_delay("https://example.com/page")
    assert delay is None


async def test_crawl_delay_fallback_to_wildcard(robots_mock) -> None:
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text=_ROBOTS_AGENT_SPECIFIC)
    )
    # Using an agent that has no explicit Crawl-delay — should fall back to *
    cache = RobotsCache(user_agent="unknownbot")
    delay = await cache.crawl_delay("https://example.com/page")
    assert delay == 10.0


async def test_crawl_delay_reuses_cache(robots_mock) -> None:
    """crawl_delay() must not issue a second HTTP request within TTL."""
    route = respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text=_ROBOTS_WITH_DELAY)
    )
    cache = RobotsCache(user_agent="*")
    await cache.crawl_delay("https://example.com/page1")
    await cache.crawl_delay("https://example.com/page2")
    assert route.call_count == 1


async def test_crawl_delay_unreachable_robots_returns_none(robots_mock) -> None:
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(404)
    )
    cache = RobotsCache(user_agent="*")
    delay = await cache.crawl_delay("https://example.com/page")
    assert delay is None


async def test_crawler_applies_crawl_delay_to_domain_gap(tmp_path) -> None:
    """Crawler should update domain throttle gap from robots.txt Crawl-delay."""
    from anansi.spider.crawler import Crawler
    from anansi.spider.spider import Spider

    class _S(Spider):
        name = "robots_test"
        start_urls = ["https://example.com/"]

    with respx.mock:
        respx.get("https://example.com/robots.txt").mock(
            return_value=httpx.Response(200, text=_ROBOTS_WITH_DELAY)
        )
        respx.get("https://example.com/").mock(
            return_value=httpx.Response(200, text="<html><body>hi</body></html>")
        )

        crawler = Crawler(
            _S,
            delay=0.0,
            delay_jitter=0.0,
            domain_delay=0.0,
            respect_robots=True,
            auto_browser=False,
            db_path=tmp_path / "crawls.db",
            adaptive_rate_limiting=False,
        )
        _ = [item async for item in crawler.run()]

    assert crawler._domain_crawl_delays.get("example.com") == 5.0
    assert crawler._domain_throttle._gaps["example.com"] == 5.0


async def test_crawler_does_not_reduce_gap_below_domain_delay(tmp_path) -> None:
    """Domain gap should not be overridden when crawl_delay < current gap."""
    from anansi.spider.crawler import Crawler
    from anansi.spider.spider import Spider

    class _S(Spider):
        name = "robots_test2"
        start_urls = ["https://example.com/"]

    robots_txt = "User-agent: *\nCrawl-delay: 1\n"

    with respx.mock:
        respx.get("https://example.com/robots.txt").mock(
            return_value=httpx.Response(200, text=robots_txt)
        )
        respx.get("https://example.com/").mock(
            return_value=httpx.Response(200, text="<html><body>hi</body></html>")
        )

        crawler = Crawler(
            _S,
            delay=0.0,
            delay_jitter=0.0,
            domain_delay=5.0,  # existing gap is 5s — higher than crawl_delay=1
            respect_robots=True,
            auto_browser=False,
            db_path=tmp_path / "crawls.db",
            adaptive_rate_limiting=True,
        )
        _ = [item async for item in crawler.run()]

    # Gap must not be reduced below the configured domain_delay
    assert crawler._domain_throttle._gaps["example.com"] >= 1.0


async def test_robots_single_flight_fetches_once(robots_mock) -> None:
    route = respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text=_ROBOTS_WITH_DELAY)
    )
    cache = RobotsCache(user_agent="*")
    results = await asyncio.gather(
        *(cache.allowed("https://example.com/page") for _ in range(10))
    )
    assert all(results)
    assert route.call_count == 1  # 10 concurrent calls → one robots.txt fetch


async def test_robots_cache_is_bounded(robots_mock) -> None:
    respx.get(url__regex=r"https://.*/robots\.txt").mock(
        return_value=httpx.Response(200, text=_ROBOTS_NO_DELAY)
    )
    cache = RobotsCache(user_agent="*")
    cache._MAX_ENTRIES = 5
    for i in range(20):
        await cache.allowed(f"https://host{i}.com/page")
    assert len(cache._cache) <= 5
