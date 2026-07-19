"""Spider base class with rule-based link following."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, ClassVar
from urllib.parse import urljoin, urlparse

if TYPE_CHECKING:
    from pydantic import BaseModel

from anansi.core import Item, Request, Response

# Re-export rule decorator from core so imports work from either location
from anansi.core import rule  # noqa: F401

_RULE_ATTR = "_anansi_rules"


class SpiderMeta(type):
    def __new__(mcs, name, bases, namespace):
        # Collect rules from all methods decorated with @rule
        rules: list[tuple[str, str, bool]] = []
        for base in reversed(bases):
            rules.extend(getattr(base, "rules", []))
        for obj in namespace.values():
            for r in getattr(obj, _RULE_ATTR, []):
                rules.append(r)
        namespace["rules"] = rules
        return super().__new__(mcs, name, bases, namespace)


class Spider(metaclass=SpiderMeta):
    """
    Base spider. Subclass it and override ``parse``.

    Example::

        class BlogSpider(Spider):
            name = "blog"
            start_urls = ["https://example.com/blog"]

            async def parse(self, response):
                for link in response.css("a.post-link"):
                    yield Request(response.urljoin(link["href"]), callback="parse_post")

            async def parse_post(self, response):
                yield Item({"title": response.css("h1")[0].text})
    """

    name: str = "spider"
    start_urls: list[str] = []
    rules: list[tuple[str, str, bool]] = []
    custom_settings: dict[str, Any] = {}
    use_sitemap: bool = False
    sitemap_filter_unchanged: bool = True
    auto_paginate: bool = False
    allowed_domains: list[str] = []  # empty = allow all; "example.com" covers subdomains too
    deny_patterns: list[str] = []    # full-URL regex patterns to reject
    # Optional Pydantic model class for item validation and type coercion.
    # When set, each yielded Item is validated before persistence; validation
    # errors are logged and stored under a ``_validation_errors`` key.
    item_schema: ClassVar[type[BaseModel] | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not getattr(cls, "name", None) or cls.name == "spider":
            cls.name = cls.__name__.lower().replace("spider", "") or "spider"

    async def parse(self, response: Response) -> AsyncIterator[Item | Request]:
        """Override to extract items and yield new Requests."""
        raise NotImplementedError(f"{type(self).__name__}.parse() is not implemented")
        # Make this a proper async generator
        return
        yield  # noqa: unreachable

    async def start_requests(self, db_path: Path | None = None) -> AsyncIterator[Request]:
        if self.use_sitemap and self.start_urls:
            from anansi.sitemap import iter_sitemap_entries
            base = "{p.scheme}://{p.netloc}".format(p=urlparse(self.start_urls[0]))
            async for entry in iter_sitemap_entries(base):
                if self.sitemap_filter_unchanged and entry.lastmod and db_path:
                    cached = await self._get_url_cache(entry.url, db_path)
                    if cached and entry.lastmod.timestamp() <= cached["last_fetched"]:
                        continue  # sitemap says unchanged since last crawl
                # Map sitemap priority (0.0–1.0) to queue priority (0–10)
                q_priority = int((entry.priority or 0.5) * 10)
                yield Request(url=entry.url, callback="parse", priority=q_priority)
        else:
            for url in self.start_urls:
                yield Request(url=url, callback="parse")

    @staticmethod
    async def _get_url_cache(url: str, db_path: Path) -> dict | None:
        """Read url_cache for *url* — used for sitemap lastmod filtering."""
        from anansi.db import crawl_db
        async with crawl_db(db_path) as db:
            rows = await db.execute_fetchall(
                "SELECT last_fetched FROM url_cache WHERE url = ?", (url,)
            )
        return dict(rows[0]) if rows else None

    def follow_links(self, response: Response) -> list[Request]:
        """Apply registered @rule patterns to extract followable URLs."""
        # Reuse the response's cached parse tree instead of building another.
        soup = response._soup
        seen: set[str] = set()
        requests: list[Request] = []

        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"]).strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            full_url = urljoin(response.url, href)
            parsed = urlparse(full_url)
            if parsed.scheme not in ("http", "https"):
                continue
            if full_url in seen:
                continue
            seen.add(full_url)

            netloc = parsed.netloc
            if self.allowed_domains:
                if not any(netloc == d or netloc.endswith(f".{d}") for d in self.allowed_domains):
                    continue
            if self.deny_patterns:
                if any(re.search(pat, full_url) for pat in self.deny_patterns):
                    continue

            for pattern, callback, follow in self.rules:
                if follow and re.search(pattern, full_url):
                    requests.append(Request(url=full_url, callback=callback))
                    break

        if self.auto_paginate:
            from anansi.parser.pagination import detect_next_page_url
            next_url = detect_next_page_url(response.html, response.url)
            if next_url and next_url not in seen:
                requests.append(Request(url=next_url, callback="parse"))

        return requests
