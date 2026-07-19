"""Tests for sitemap parsing: gzip support, changefreq, priority."""

from __future__ import annotations

import gzip
import httpx
import pytest
import respx

from anansi.sitemap import SitemapEntry, _maybe_decompress, iter_sitemap_entries


_PLAIN_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://example.com/page1</loc>
    <lastmod>2024-01-15</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.8</priority>
  </url>
  <url>
    <loc>https://example.com/page2</loc>
    <changefreq>monthly</changefreq>
    <priority>0.5</priority>
  </url>
  <url>
    <loc>https://example.com/page3</loc>
  </url>
</urlset>
"""

_INDEX_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap>
    <loc>https://example.com/sitemap-pages.xml</loc>
  </sitemap>
</sitemapindex>
"""


def _make_gzip(content: str) -> bytes:
    return gzip.compress(content.encode("utf-8"))


# ── _maybe_decompress ─────────────────────────────────────────────────────────

def test_maybe_decompress_plain_url_returns_unchanged() -> None:
    result = _maybe_decompress("<xml/>", "https://example.com/sitemap.xml")
    assert result == "<xml/>"


def test_maybe_decompress_gz_url_decompresses() -> None:
    original = "<?xml version='1.0'?><urlset/>"
    gz_bytes = _make_gzip(original)
    # Simulate what httpx does: decode bytes as latin-1 text
    encoded_as_text = gz_bytes.decode("latin-1")
    result = _maybe_decompress(encoded_as_text, "https://example.com/sitemap.xml.gz")
    assert result == original


def test_maybe_decompress_gzip_content_type() -> None:
    original = "<urlset/>"
    gz_bytes = _make_gzip(original)
    encoded_as_text = gz_bytes.decode("latin-1")
    result = _maybe_decompress(encoded_as_text, "https://example.com/sitemap.xml", "application/gzip")
    assert result == original


def test_maybe_decompress_malformed_gzip_falls_back() -> None:
    # Not valid gzip — should return unchanged
    result = _maybe_decompress("not gzip", "https://example.com/sitemap.xml.gz")
    assert result == "not gzip"


# ── iter_sitemap_entries ──────────────────────────────────────────────────────

async def test_changefreq_and_priority_parsed() -> None:
    with respx.mock:
        respx.get("https://example.com/sitemap.xml").mock(
            return_value=httpx.Response(200, text=_PLAIN_SITEMAP)
        )
        entries = [e async for e in iter_sitemap_entries("https://example.com")]

    assert len(entries) == 3

    e1 = next(e for e in entries if e.url == "https://example.com/page1")
    assert e1.changefreq == "weekly"
    assert e1.priority == pytest.approx(0.8)

    e2 = next(e for e in entries if e.url == "https://example.com/page2")
    assert e2.changefreq == "monthly"
    assert e2.priority == pytest.approx(0.5)

    e3 = next(e for e in entries if e.url == "https://example.com/page3")
    assert e3.changefreq is None
    assert e3.priority is None


async def test_gzip_sitemap_fallback_on_404() -> None:
    """When sitemap.xml returns 404, iter_sitemap_entries should try sitemap.xml.gz."""
    gz_content = _make_gzip(_PLAIN_SITEMAP)
    gz_as_text = gz_content.decode("latin-1")

    with respx.mock:
        respx.get("https://example.com/sitemap.xml").mock(
            return_value=httpx.Response(404)
        )
        respx.get("https://example.com/sitemap.xml.gz").mock(
            return_value=httpx.Response(200, text=gz_as_text)
        )
        entries = [e async for e in iter_sitemap_entries("https://example.com")]

    assert len(entries) == 3
    urls = [e.url for e in entries]
    assert "https://example.com/page1" in urls


async def test_gzip_sitemap_fetched_directly() -> None:
    """If sitemap.xml itself returns gzip bytes, they should be decompressed."""
    gz_content = _make_gzip(_PLAIN_SITEMAP)
    gz_as_text = gz_content.decode("latin-1")

    with respx.mock:
        respx.get("https://example.com/sitemap.xml").mock(
            return_value=httpx.Response(
                200, text=gz_as_text,
                headers={"content-type": "application/gzip"},
            )
        )
        entries = [e async for e in iter_sitemap_entries("https://example.com")]

    assert len(entries) == 3


async def test_malformed_priority_returns_none() -> None:
    sitemap = """<urlset>
      <url>
        <loc>https://example.com/</loc>
        <priority>not-a-float</priority>
      </url>
    </urlset>"""
    with respx.mock:
        respx.get("https://example.com/sitemap.xml").mock(
            return_value=httpx.Response(200, text=sitemap)
        )
        entries = [e async for e in iter_sitemap_entries("https://example.com")]

    assert entries[0].priority is None


async def test_sitemap_index_child_gzip_decompressed() -> None:
    """Child sitemaps in a sitemapindex can also be gzip-compressed."""
    gz_content = _make_gzip(_PLAIN_SITEMAP)
    gz_as_text = gz_content.decode("latin-1")

    with respx.mock:
        respx.get("https://example.com/sitemap.xml").mock(
            return_value=httpx.Response(200, text=_INDEX_SITEMAP)
        )
        respx.get("https://example.com/sitemap-pages.xml").mock(
            return_value=httpx.Response(
                200, text=gz_as_text,
                headers={"content-type": "application/gzip"},
            )
        )
        entries = [e async for e in iter_sitemap_entries("https://example.com")]

    assert len(entries) == 3


async def test_no_sitemap_yields_nothing() -> None:
    with respx.mock:
        respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(404))
        respx.get("https://example.com/sitemap.xml.gz").mock(return_value=httpx.Response(404))
        entries = [e async for e in iter_sitemap_entries("https://example.com")]
    assert entries == []


_SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/child1.xml</loc></sitemap>
  <sitemap><loc>https://example.com/child2.xml</loc></sitemap>
</sitemapindex>
"""


async def test_index_reuses_one_fetcher_across_children(monkeypatch) -> None:
    import anansi.fetchers.http as http_mod

    real = http_mod.HTTPFetcher
    count = {"n": 0}

    class _Counting(real):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **k):
            count["n"] += 1
            super().__init__(*a, **k)

    monkeypatch.setattr(http_mod, "HTTPFetcher", _Counting)

    with respx.mock:
        respx.get("https://example.com/sitemap.xml").mock(
            return_value=httpx.Response(200, text=_SITEMAP_INDEX)
        )
        respx.get("https://example.com/child1.xml").mock(
            return_value=httpx.Response(200, text=_PLAIN_SITEMAP)
        )
        respx.get("https://example.com/child2.xml").mock(
            return_value=httpx.Response(200, text=_PLAIN_SITEMAP)
        )
        entries = [e async for e in iter_sitemap_entries("https://example.com")]

    assert count["n"] == 1  # one fetcher reused for root + both children
    assert len(entries) == 6  # 3 entries per child sitemap
