"""Phase 3: per-host fetcher reuse, origin warm-up, Referer + browser
cookie hand-off for surviving behavioral (Akamai-style) bot scoring."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from anansi.core import Request, Response
from anansi.fetchers.base import FetchResult
from anansi.spider.crawler import Crawler
from anansi.spider.spider import Spider
from anansi import security


class _LinkSpider(Spider):
    name = "continuity_test"
    start_urls = ["https://example.com/"]

    async def parse(self, response: Response):
        from urllib.parse import urljoin

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(response.html, "lxml")
        for a in soup.find_all("a", href=True):
            yield Request(url=urljoin(response.url, str(a["href"])), callback="parse")


def _make_crawler(tmp_path: Path) -> Crawler:
    return Crawler(
        _LinkSpider,
        delay=0.0,
        delay_jitter=0.0,
        domain_delay=0.0,
        respect_robots=False,
        auto_browser=False,
        db_path=tmp_path / "c.db",
        adaptive_rate_limiting=False,
    )


async def test_one_fetcher_per_host_and_warmup(tmp_path: Path) -> None:
    crawler = _make_crawler(tmp_path)
    with respx.mock:
        origin = respx.get("https://example.com/").mock(
            return_value=httpx.Response(
                200, text="ok", headers={"Set-Cookie": "bm_sz=z; Path=/"}
            )
        )
        f1 = await crawler._get_host_fetcher("https://example.com/a", {})
        f2 = await crawler._get_host_fetcher("https://example.com/b", {})

    assert f1 is f2, "fetcher must be reused per host"
    assert origin.called, "origin warm-up GET should fire on first contact"
    # The warm-up cookie is now in the shared jar for subsequent requests.
    assert f1._session_cookies.get("bm_sz") == "z"


async def test_warmup_skipped_under_disable_antibot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(security, "DISABLE_ANTIBOT", True)
    crawler = _make_crawler(tmp_path)
    with respx.mock:
        origin = respx.get("https://example.com/").mock(
            return_value=httpx.Response(200, text="ok")
        )
        await crawler._get_host_fetcher("https://example.com/a", {})

    assert not origin.called, "warm-up must be skipped when anti-bot disabled"


async def test_browser_cookies_handed_to_http_session(tmp_path: Path) -> None:
    crawler = _make_crawler(tmp_path)
    result = FetchResult(
        url="https://example.com/x", status=200, html="<html></html>",
        cookies={"_abck": "valid-token"}, via_browser=True,
    )
    crawler._handoff_browser_cookies("https://example.com/x", result)

    fetcher = crawler._host_fetchers.get("example.com")
    assert fetcher is not None
    assert fetcher._session_cookies.get("_abck") == "valid-token"


async def test_handoff_noop_under_disable_antibot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(security, "DISABLE_ANTIBOT", True)
    crawler = _make_crawler(tmp_path)
    result = FetchResult(
        url="https://example.com/x", status=200, html="x",
        cookies={"_abck": "v"},
    )
    crawler._handoff_browser_cookies("https://example.com/x", result)
    assert "example.com" not in crawler._host_fetchers


async def test_referer_set_from_parent_in_crawl(tmp_path: Path) -> None:
    """A followed link must carry the parent page as its Referer."""
    seen_referers: dict[str, str] = {}

    with respx.mock:
        def record(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            seen_referers[path] = request.headers.get("referer", "")
            if path == "/":
                return httpx.Response(
                    200, text='<a href="https://example.com/child">c</a>'
                )
            return httpx.Response(200, text="<html>leaf</html>")

        respx.get(url__regex=r"https://example\.com/.*").mock(side_effect=record)

        crawler = _make_crawler(tmp_path)
        _ = [item async for item in crawler.run()]

    assert seen_referers.get("/child") == "https://example.com/", (
        f"child request missing parent Referer; got {seen_referers!r}"
    )
