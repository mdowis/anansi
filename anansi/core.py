"""Core data models shared across the framework."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, AsyncIterator, Callable
from urllib.parse import urljoin, urlparse


@dataclass
class Request:
    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    use_browser: bool = False
    proxy: str | None = None
    callback: str | None = None  # spider method name to call with response


@dataclass
class Response:
    url: str
    status: int
    html: str
    headers: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    elapsed: float = 0.0
    via_browser: bool = False
    spa_state: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def urljoin(self, href: str) -> str:
        return urljoin(self.url, href)

    @cached_property
    def _soup(self) -> Any:
        """Parsed BeautifulSoup tree, built once and shared by every ``css()``
        call (and by the spider's ``follow_links``) instead of re-parsing per call."""
        from bs4 import BeautifulSoup
        return BeautifulSoup(self.html, "lxml")

    @cached_property
    def _dom(self) -> Any:
        """Parsed lxml tree, built once and shared by every ``xpath()`` call."""
        from lxml import etree
        return etree.fromstring(self.html.encode(), etree.HTMLParser())

    def css(self, selector: str) -> list[Any]:
        return self._soup.select(selector)

    def xpath(self, query: str) -> list[Any]:
        return self._dom.xpath(query)


@dataclass
class Item:
    data: dict[str, Any]
    source_url: str = ""
    spider_name: str = ""


# ── Spider base ──────────────────────────────────────────────────────────────

_RULE_ATTR = "_anansi_rules"


def rule(pattern: str, callback: str | None = None, follow: bool = True):
    """Decorator that registers a link-following rule on a spider method."""
    def decorator(fn: Callable) -> Callable:
        if not hasattr(fn, _RULE_ATTR):
            setattr(fn, _RULE_ATTR, [])
        getattr(fn, _RULE_ATTR).append((pattern, callback or fn.__name__, follow))
        return fn
    return decorator


class SpiderMeta(type):
    def __new__(mcs, name, bases, namespace):
        rules: list[tuple[str, str, bool]] = []
        for obj in namespace.values():
            for r in getattr(obj, _RULE_ATTR, []):
                rules.append(r)
        namespace.setdefault("rules", rules)
        return super().__new__(mcs, name, bases, namespace)


class Spider(metaclass=SpiderMeta):
    """Base spider class. Subclass and override ``parse``."""

    name: str = "spider"
    start_urls: list[str] = []
    rules: list[tuple[str, str, bool]] = []
    custom_settings: dict[str, Any] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.name or cls.name == "spider":
            cls.name = cls.__name__.lower().replace("spider", "")

    async def parse(self, response: Response) -> AsyncIterator[Item | Request]:
        """Override to extract items and yield new requests."""
        raise NotImplementedError

    def follow_links(self, response: Response) -> list[Request]:
        """Apply registered rules to extract followable links."""
        # Reuse the response's cached parse tree instead of building another.
        soup = response._soup
        requests: list[Request] = []
        for anchor in soup.find_all("a", href=True):
            href = response.urljoin(anchor["href"])
            parsed = urlparse(href)
            if parsed.scheme not in ("http", "https"):
                continue
            for pattern, callback, follow in self.rules:
                if follow and re.search(pattern, href):
                    requests.append(Request(url=href, callback=callback))
                    break
        return requests

    async def start_requests(self) -> AsyncIterator[Request]:
        for url in self.start_urls:
            yield Request(url=url, callback="parse")


# Re-export Crawler lazily to avoid circular imports
def __getattr__(name: str):
    if name == "Crawler":
        from anansi.spider.crawler import Crawler
        return Crawler
    raise AttributeError(name)
