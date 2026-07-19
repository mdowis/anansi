"""Async sitemap.xml parser — yields structured entries from a site's sitemap."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, AsyncIterator, NamedTuple
from urllib.parse import urlparse

from anansi.security import (
    DecompressionTooLargeError,
    UnsafeURLError,
    is_url_safe_for_public_fetch,
    safe_gzip_decompress,
    same_registrable_domain,
)

logger = logging.getLogger(__name__)

# Hard caps to bound recursion and decompression on attacker-controlled sitemaps.
_MAX_SITEMAP_BYTES = 50 * 1024 * 1024  # 50 MB after gzip decompression
_MAX_SITEMAP_DEPTH = 3
_MAX_CHILD_SITEMAPS = 1_000


class SitemapEntry(NamedTuple):
    """A single URL entry from a sitemap."""

    url: str
    lastmod: datetime | None  # None when the tag is absent or unparseable
    changefreq: str | None = None  # "always", "daily", "weekly", etc.
    priority: float | None = None  # 0.0–1.0; sitemap default is 0.5


# Common lastmod date formats used in the wild
_LASTMOD_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",   # 2024-01-15T10:30:00+00:00
    "%Y-%m-%dT%H:%M:%SZ",    # 2024-01-15T10:30:00Z (not strictly valid but common)
    "%Y-%m-%dT%H:%M:%S",     # 2024-01-15T10:30:00 (no tz, treated as UTC)
    "%Y-%m-%d",               # 2024-01-15
)


def _parse_lastmod(raw: str) -> datetime | None:
    """Parse a sitemap lastmod string into an aware datetime (UTC). Returns None on failure."""
    raw = raw.strip()
    for fmt in _LASTMOD_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _maybe_decompress(data: str, url: str, content_type: str = "") -> str:
    """Decompress gzip-encoded sitemap content if the URL or content-type indicates it.

    httpx decodes the response body as text using the server-reported charset (or
    latin-1 as fallback).  For a raw .gz file the bytes survive intact when
    re-encoded with latin-1, so we can recover the original bytes and decompress.
    """
    is_gz = url.rstrip("/").endswith(".gz") or "gzip" in content_type or "zip" in content_type
    if not is_gz:
        return data
    try:
        decoded = safe_gzip_decompress(
            data.encode("latin-1"), max_output_bytes=_MAX_SITEMAP_BYTES
        )
        return decoded.decode("utf-8")
    except DecompressionTooLargeError:
        logger.warning("sitemap %s exceeds %d-byte cap; refusing to decompress",
                       url, _MAX_SITEMAP_BYTES)
        return ""
    except Exception:
        return data  # fall back to raw text if decompression fails


async def iter_sitemap_urls(base_url: str) -> AsyncIterator[str]:
    """Yield every page URL from *base_url*'s sitemap.xml (bare strings).

    This is the legacy interface kept for backward compatibility. For access to
    ``<lastmod>`` dates use ``iter_sitemap_entries`` instead.
    """
    async for entry in iter_sitemap_entries(base_url):
        yield entry.url


async def iter_sitemap_entries(base_url: str) -> AsyncIterator[SitemapEntry]:
    """Fetch /sitemap.xml from *base_url* and yield every page as a SitemapEntry.

    Handles sitemap index files (``<sitemapindex>``) by recursively fetching
    each child sitemap. Falls back gracefully — if no sitemap exists or the
    fetch fails, yields nothing.

    Supports gzip-compressed sitemaps (``sitemap.xml.gz``): tries the plain URL
    first, then automatically retries with the ``.gz`` variant on 404.

    Each entry carries ``<lastmod>``, ``<changefreq>``, and ``<priority>`` when
    the sitemap provides them.
    """
    from anansi.fetchers.http import HTTPFetcher

    sitemap_url = f"{base_url.rstrip('/')}/sitemap.xml"
    parent_host = urlparse(base_url).hostname or ""

    # One fetcher for the whole traversal (root + gzip fallback + every child
    # sitemap) so connections/TLS are reused instead of a fresh client per fetch.
    async with HTTPFetcher() as f:
        xml: str | None = None
        try:
            result = await f.fetch(sitemap_url, timeout=15.0)
            if result.status == 200:
                xml = _maybe_decompress(result.html, sitemap_url, result.headers.get("content-type", ""))
            else:
                # Try the gzip variant as a fallback
                gz_url = f"{base_url.rstrip('/')}/sitemap.xml.gz"
                result = await f.fetch(gz_url, timeout=15.0)
                if result.status == 200:
                    xml = _maybe_decompress(result.html, gz_url, result.headers.get("content-type", ""))
        except Exception:
            return

        if not xml:
            return

        async for entry in _parse_sitemap(
            xml, parent_host=parent_host, depth=0, fetched=[0], fetcher=f
        ):
            yield entry


async def _parse_sitemap(
    xml: str,
    *,
    parent_host: str,
    depth: int,
    fetched: list[int],
    fetcher: Any,
) -> AsyncIterator[SitemapEntry]:
    """Parse one sitemap or sitemap-index XML string, yielding SitemapEntry objects.

    *parent_host* and *depth* / *fetched* bound the recursion against hostile
    sitemaps that try to fan out into private networks or unbounded child trees.
    ``fetched`` is a single-element list used as a mutable counter shared across
    recursive calls. *fetcher* is the shared HTTPFetcher reused for child fetches.
    """
    if "<sitemapindex" in xml:
        # Index file — each <loc> points to a child sitemap; <lastmod> here
        # refers to the child sitemap file, not the page, so we ignore it.
        if depth >= _MAX_SITEMAP_DEPTH:
            logger.warning("sitemap recursion depth cap %d hit — stopping", _MAX_SITEMAP_DEPTH)
            return
        for child_url in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml):
            if fetched[0] >= _MAX_CHILD_SITEMAPS:
                logger.warning("sitemap fan-out cap %d hit — stopping", _MAX_CHILD_SITEMAPS)
                return
            fetched[0] += 1
            child_url = child_url.strip()
            # Refuse SSRF and cross-domain child fetches: the child sitemap must
            # be http(s), resolve to a public address, and share the parent's
            # registrable domain.
            try:
                is_url_safe_for_public_fetch(child_url)
            except UnsafeURLError as exc:
                logger.warning("rejecting child sitemap %s: %s", child_url, exc)
                continue
            child_host = urlparse(child_url).hostname or ""
            if not same_registrable_domain(child_host, parent_host):
                logger.warning(
                    "rejecting child sitemap %s: domain %r does not match parent %r",
                    child_url, child_host, parent_host,
                )
                continue
            try:
                result = await fetcher.fetch(child_url, timeout=15.0)
                if result.status == 200:
                    child_xml = _maybe_decompress(
                        result.html, child_url, result.headers.get("content-type", "")
                    )
                    async for entry in _parse_sitemap(
                        child_xml,
                        parent_host=parent_host,
                        depth=depth + 1,
                        fetched=fetched,
                        fetcher=fetcher,
                    ):
                        yield entry
            except Exception:
                continue
    else:
        # Stream each <url> block instead of materialising every block of a
        # (up to 50 MB) sitemap as a list before iterating.
        found_any = False
        for m in re.finditer(r"<url>(.*?)</url>", xml, re.DOTALL):
            found_any = True
            block = m.group(1)
            loc_match = re.search(r"<loc>\s*(.*?)\s*</loc>", block)
            if not loc_match:
                continue
            url = loc_match.group(1).strip()

            lastmod_match = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", block)
            lastmod = _parse_lastmod(lastmod_match.group(1)) if lastmod_match else None

            changefreq_match = re.search(r"<changefreq>\s*(.*?)\s*</changefreq>", block)
            changefreq = changefreq_match.group(1).strip() if changefreq_match else None

            priority_match = re.search(r"<priority>\s*(.*?)\s*</priority>", block)
            priority: float | None = None
            if priority_match:
                try:
                    priority = float(priority_match.group(1))
                except ValueError:
                    pass

            yield SitemapEntry(url=url, lastmod=lastmod, changefreq=changefreq, priority=priority)

        if not found_any:
            # Fallback: sitemap without <url> wrappers — extract bare <loc> tags
            for m in re.finditer(r"<loc>\s*(.*?)\s*</loc>", xml):
                yield SitemapEntry(url=m.group(1).strip(), lastmod=None)
