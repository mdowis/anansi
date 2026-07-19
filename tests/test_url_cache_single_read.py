"""_do_fetch builds conditional-GET headers from the pre-read url_cache row and
does not read the cache again itself (#16)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from anansi.core import Spider
from anansi.fetchers.base import FetchResult
from anansi.spider.crawler import Crawler


class _S(Spider):
    name = "s"
    start_urls = ["http://e.com/"]

    async def parse(self, response):  # pragma: no cover - not driven here
        return
        yield


async def test_do_fetch_uses_passed_cache_row_without_hitting_db() -> None:
    fetcher = AsyncMock()
    fetcher.fetch = AsyncMock(
        return_value=FetchResult(
            url="http://e.com/p", status=200, html="<html><body>ok</body></html>"
        )
    )
    crawler = Crawler(
        spider_class=_S, fetcher=fetcher, conditional_get=True, auto_browser=False
    )

    calls = {"n": 0}

    async def _spy_get_url_cache(url: str):
        calls["n"] += 1
        return None

    crawler._get_url_cache = _spy_get_url_cache  # type: ignore[assignment]

    cached = {"etag": 'W/"abc"', "last_modified": "Mon, 01 Jan 2026", "content_hash": "h"}
    result = await crawler._do_fetch(
        "http://e.com/p", proxy=None, meta={}, cached=cached
    )

    assert calls["n"] == 0  # no second DB read inside _do_fetch
    sent = fetcher.fetch.call_args.kwargs["headers"]
    assert sent["If-None-Match"] == 'W/"abc"'
    assert sent["If-Modified-Since"] == "Mon, 01 Jan 2026"
    assert result.status == 200
