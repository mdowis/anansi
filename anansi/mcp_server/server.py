"""
Anansi MCP Server

Exposes Anansi's scraping capabilities as MCP tools so any LLM can
fetch pages, extract structured data, run full crawls, and manage
pause/resume — all through a clean tool interface.

Run with:
    python -m anansi.mcp_server.server          # stdio transport (default)
    anansi-mcp                                   # via installed entry-point
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import textwrap
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP

from anansi.db import DATA_DIR
from anansi import security
from anansi.security import (
    InvalidImpersonateError,
    OutOfRangeError,
    PathOutsideSandboxError,
    UnsafeRegexError,
    UnsafeURLError,
    clamp_int,
    confine_to_dir,
    is_url_safe_for_public_fetch,
    redact_userinfo,
    validate_browser_selector,
    validate_impersonate,
    validate_proxy_url,
    validate_regex,
)

logger = logging.getLogger(__name__)

# Sandbox for files written via export_crawl. The MCP client supplies the path;
# without this confinement an arbitrary file write is one tool call away.
_EXPORT_ROOT = DATA_DIR / "exports"

# Allowed Playwright action types. Anything outside this set is rejected before
# the action list reaches BrowserFetcher.
_ALLOWED_ACTION_TYPES = frozenset({
    "click", "scroll_to_bottom", "fill", "press", "wait", "wait_for_selector",
})
# Keys allowed in {"type": "press", ...} actions. Restricts the LLM from
# triggering arbitrary global shortcuts (e.g. Control+S).
_ALLOWED_PRESS_KEYS = frozenset({
    "Enter", "Tab", "Escape", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight",
    "Space", "Backspace", "Delete", "PageDown", "PageUp", "Home", "End",
})

# Aggregate-bytes cap on the page cache, on top of the entry-count cap.
_PAGE_CACHE_MAX_BYTES = 100 * 1024 * 1024  # 100 MB across all cached entries
_page_cache_bytes = 0

# Per-tool input caps. The MCP client is untrusted; without these an LLM can
# easily DoS the operator's machine by passing huge integer arguments or
# multi-megabyte payloads.
_MAX_PAGES = 100_000
_MAX_DEPTH = 100
_MAX_CONCURRENCY = 32
_MAX_URLS_PER_BATCH = 500
_MAX_ACTIONS = 50
_MAX_HTML_BYTES = 25 * 1024 * 1024  # 25 MB
_MAX_GET_ITEMS = 10_000
_MAX_ACTIVE_CRAWLS = 16
# Aggregate wall-clock budget across a single action list. Combined with
# _MAX_ACTIONS this bounds the time a Playwright context can be held.
_MAX_ACTION_BUDGET_MS = 60_000


def _validate_proxy(proxy: str | None) -> str | None:
    """Validate a caller-supplied proxy URL or return None.

    Raises ``UnsafeURLError``; callers convert that into a structured tool
    error. Returns the (unchanged) proxy URL on success so it can be passed
    through to the fetcher. The private-network range check is governed by the
    operator-only ``ANANSI_ALLOW_PRIVATE_NETWORKS`` env var, never by the
    untrusted MCP client.
    """
    if proxy is None:
        return None
    validate_proxy_url(proxy, allow_private=security.ALLOW_PRIVATE_NETWORKS)
    return proxy


# Warn once at import (not per request) if the operator set conflicting env
# flags. ANANSI_DISABLE_ANTIBOT always wins over ANANSI_IMPERSONATE.
if security.DISABLE_ANTIBOT and security.IMPERSONATE_DEFAULT is not None:
    logger.warning(
        "Both ANANSI_DISABLE_ANTIBOT and ANANSI_IMPERSONATE are set; "
        "anti-bot is disabled so impersonation will NOT be used."
    )


def _resolve_impersonate(value: str | None) -> str | None:
    """Resolve the effective curl-cffi impersonation target.

    Precedence: ANANSI_DISABLE_ANTIBOT (operator kill-switch) always wins and
    forces None. Otherwise a caller-supplied value is validated against the
    allowlist (the MCP client is untrusted) and used; if the caller passed
    nothing, fall back to the operator ANANSI_IMPERSONATE default (already
    validated at import). Raises ``InvalidImpersonateError`` for a bad
    caller value; callers convert that into a structured tool error.
    """
    if security.DISABLE_ANTIBOT:
        return None
    if value is not None:
        return validate_impersonate(value)
    return security.IMPERSONATE_DEFAULT


def _validate_url(url: str) -> None:
    """Reject schemes other than http(s) and (unless the operator opted in via
    ``ANANSI_ALLOW_PRIVATE_NETWORKS``) non-public destinations."""
    is_url_safe_for_public_fetch(url, allow_private=security.ALLOW_PRIVATE_NETWORKS)


def _validate_actions(actions: list[dict[str, Any]] | None) -> None:
    """Reject action lists with unknown types, out-of-allowlist press keys, or
    non-CSS selectors. Validation here matches what BrowserFetcher enforces at
    runtime, but firing earlier produces a clearer structured error to the MCP
    client (and avoids waking the Playwright pool for invalid input).
    """
    if not actions:
        return
    for i, action in enumerate(actions):
        if not isinstance(action, dict):
            raise ValueError(f"action #{i} must be a dict, got {type(action).__name__}")
        atype = action.get("type")
        if atype not in _ALLOWED_ACTION_TYPES:
            raise ValueError(
                f"action #{i} type {atype!r} not allowed; "
                f"permitted: {sorted(_ALLOWED_ACTION_TYPES)}"
            )
        if atype == "press":
            key = action.get("key")
            if key not in _ALLOWED_PRESS_KEYS:
                raise ValueError(
                    f"action #{i} press key {key!r} not in allowlist"
                )
        # Validate selector strings (CSS only, no Playwright engine prefixes).
        if atype in ("click", "fill", "wait_for_selector", "press"):
            sel = action.get("selector")
            try:
                validate_browser_selector(sel)
            except ValueError as exc:
                raise ValueError(f"action #{i} selector rejected: {exc}") from exc


def _cache_entry_bytes(chunks: list[str], meta: dict[str, Any]) -> int:
    return sum(len(c) for c in chunks) + sum(
        len(str(k)) + len(str(v)) for k, v in meta.items()
    )

mcp = FastMCP(
    name="anansi",
    instructions=textwrap.dedent("""
        Anansi is an adaptive web scraping framework.

        You can use it to:
        - Fetch single pages (HTTP or headless browser with anti-bot bypass)
        - Extract structured data with self-healing CSS selectors
        - Launch full site crawls with concurrency, proxy rotation, and pause/resume
        - List and manage active or paused crawls

        For pages behind Cloudflare or other bot protection, set use_browser=true.
        Selectors that stop working are automatically healed and re-learned.

        Handling large pages with fetch_url:
        - format="text"     — strips all HTML tags, typically 5-10x smaller than raw HTML
        - format="markdown" — converts to Markdown, best for reading structured content
        - format="html"     — raw HTML (default)
        - chunk_size=20000  — split at DOM/paragraph boundaries; use chunk_index to page
          through results. total_chunks in the response tells you how many chunks exist.
          Subsequent chunks are served from cache — no re-download needed.

        Interacting with JS-heavy pages (use_browser=true required):
        - Pass actions=[...] to click buttons, fill forms, scroll, or wait after load.
          Example: actions=[{"type":"click","selector":"button.load-more"},{"type":"wait","ms":1500}]
          The HTML returned reflects the page state after all actions complete.

        Crawling authenticated (login-protected) sites:
        - Pass cookies={"session": "abc"} or auth_headers={"Authorization": "Bearer token"}
          to crawl_site. These are forwarded to every request in the crawl.

        Getting crawl results:
        - get_crawl_items(crawl_id) returns structured items as JSON (paginated via offset).
        - export_crawl(crawl_id, format="csv", path="/tmp/out.csv") exports to a file.
        - Items are persisted to SQLite and survive process restarts.
    """),
)

# In-memory registry of active Crawler instances (crawl_id → Crawler)
_active_crawlers: dict[str, Any] = {}
_crawl_tasks: dict[str, asyncio.Task] = {}

# Page cache: (url, format) → (chunks, metadata, expiry). LRU-capped to 200 entries.
import time as _time
_PAGE_CACHE_TTL = 300.0  # 5 minutes
_PAGE_CACHE_MAX = 200
_page_cache: OrderedDict[tuple[str, str], tuple[list[str], dict[str, Any], float]] = OrderedDict()


def _html_to_text(html: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "head"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def _html_to_markdown(html: str) -> str:
    import markdownify
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return markdownify.markdownify(str(soup), heading_style="ATX", strip=["a"]).strip()


def _chunk_by_dom(html: str, chunk_size: int) -> list[str]:
    """
    Split HTML into chunks that never break mid-element.

    Walks the direct children of <body>, accumulating them into groups whose
    combined HTML length stays under chunk_size. A single element larger than
    chunk_size becomes its own chunk rather than being dropped.
    """
    from bs4 import BeautifulSoup, Tag
    soup = BeautifulSoup(html, "lxml")
    body = soup.find("body") or soup
    children = [c for c in body.children if isinstance(c, Tag)]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for child in children:
        fragment = str(child)
        flen = len(fragment)
        if current and current_len + flen > chunk_size:
            chunks.append("".join(current))
            current, current_len = [], 0
        current.append(fragment)
        current_len += flen

    if current:
        chunks.append("".join(current))

    return chunks or [html]


def _chunk_by_paragraph(text: str, chunk_size: int) -> list[str]:
    """
    Split plain text / markdown into chunks at paragraph boundaries.

    Tries to keep each chunk under chunk_size characters. A single paragraph
    larger than chunk_size becomes its own chunk.
    """
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        plen = len(para) + 2  # +2 for the \n\n separator
        if current and current_len + plen > chunk_size:
            chunks.append("\n\n".join(current))
            current, current_len = [], 0
        current.append(para)
        current_len += plen

    if current:
        chunks.append("\n\n".join(current))

    return chunks or [text]


def _get_chunks(html: str, fmt: str, chunk_size: int) -> list[str]:
    if fmt == "html":
        return _chunk_by_dom(html, chunk_size)
    content = _html_to_text(html) if fmt == "text" else _html_to_markdown(html)
    return _chunk_by_paragraph(content, chunk_size)


# ── Shared fetch helper (used by fetch_url, fetch_urls, fetch_and_extract) ────

async def _fetch_one(
    url: str,
    *,
    use_browser: bool = False,
    proxy: str | None = None,
    wait_for_selector: str | None = None,
    timeout: float = 30.0,
    format: str = "html",
    chunk_size: int | None = None,
    chunk_index: int = 0,
    actions: list[dict[str, Any]] | None = None,
    impersonate: str | None = None,
) -> dict[str, Any]:
    """Fetch one URL, apply format conversion + chunking, and cache the result."""
    global _page_cache_bytes
    try:
        _validate_url(url)
    except UnsafeURLError as exc:
        return {"url": url, "error": f"unsafe URL: {exc}", "status": None}
    try:
        _validate_actions(actions)
    except ValueError as exc:
        return {"url": url, "error": f"invalid actions: {exc}", "status": None}
    cache_key = (url, format)
    now = _time.monotonic()

    # Actions bypass cache since they affect dynamic page state
    cached = None if actions else _page_cache.get(cache_key)
    if cached and now < cached[2]:
        _page_cache.move_to_end(cache_key)
        chunks, meta = cached[0], cached[1]
    else:
        if use_browser:
            from anansi.fetchers.browser import BrowserFetcher
            async with BrowserFetcher(timeout=timeout) as fetcher:
                result = await fetcher.fetch(
                    url,
                    proxy=proxy,
                    wait_for=wait_for_selector,
                    timeout=timeout,
                    actions=actions,
                )
        else:
            from anansi.fetchers.http import HTTPFetcher
            async with HTTPFetcher(
                timeout=timeout, impersonate=impersonate
            ) as fetcher:
                result = await fetcher.fetch(url, proxy=proxy, timeout=timeout)

            # Graduated Akamai escalation: impersonated retry → browser.
            # Detection still runs under DISABLE_ANTIBOT (honest status) but
            # does not escalate.
            from anansi.fetchers.escalate import (
                DEFAULT_IMPERSONATE,
                escalate_akamai,
            )

            async def _retry_impersonated() -> Any:
                target = impersonate or security.IMPERSONATE_DEFAULT or DEFAULT_IMPERSONATE
                async with HTTPFetcher(
                    timeout=timeout, impersonate=target
                ) as f2:
                    return await f2.fetch(url, proxy=proxy, timeout=timeout)

            async def _browser_fetch() -> Any:
                from anansi.fetchers.browser import BrowserFetcher
                async with BrowserFetcher(timeout=timeout) as bf:
                    return await bf.fetch(url, proxy=proxy, timeout=timeout)

            result = await escalate_akamai(
                url=url,
                initial=result,
                retry_impersonated=_retry_impersonated,
                browser_fetch=_browser_fetch,
                disable_antibot=security.DISABLE_ANTIBOT,
            )

        meta = {
            "url": result.url,
            "status": result.status,
            "elapsed": round(result.elapsed, 3),
            "via_browser": result.via_browser,
        }

        if chunk_size:
            chunks = _get_chunks(result.html, format, chunk_size)
        else:
            if format == "text":
                chunks = [_html_to_text(result.html)]
            elif format == "markdown":
                chunks = [_html_to_markdown(result.html)]
            else:
                chunks = [result.html]

        # Evict the previous entry for this key from the byte counter if present.
        if cache_key in _page_cache:
            prev_chunks, prev_meta, _ = _page_cache[cache_key]
            _page_cache_bytes -= _cache_entry_bytes(prev_chunks, prev_meta)
        entry_bytes = _cache_entry_bytes(chunks, meta)
        _page_cache[cache_key] = (chunks, meta, now + _PAGE_CACHE_TTL)
        _page_cache.move_to_end(cache_key)
        _page_cache_bytes += entry_bytes
        # Evict LRU entries until both caps are satisfied.
        while _page_cache and (
            len(_page_cache) > _PAGE_CACHE_MAX
            or _page_cache_bytes > _PAGE_CACHE_MAX_BYTES
        ):
            _, (evicted_chunks, evicted_meta, _ttl) = _page_cache.popitem(last=False)
            _page_cache_bytes -= _cache_entry_bytes(evicted_chunks, evicted_meta)

    total_chunks = len(chunks)
    if chunk_index >= total_chunks:
        return {
            **meta,
            "error": f"chunk_index {chunk_index} out of range (total_chunks={total_chunks})",
            "total_chunks": total_chunks,
        }

    content = chunks[chunk_index]
    return {
        **meta,
        "content": content,
        "format": format,
        "content_length": len(content),
        "chunk_index": chunk_index,
        "total_chunks": total_chunks,
    }


# ── Tool: fetch_url ───────────────────────────────────────────────────────────

@mcp.tool()
async def fetch_url(
    url: str,
    use_browser: bool = False,
    proxy: str | None = None,
    wait_for_selector: str | None = None,
    timeout: float = 30.0,
    format: str = "html",
    chunk_size: int | None = None,
    chunk_index: int = 0,
    actions: list[dict[str, Any]] | None = None,
    impersonate: str | None = None,
) -> dict[str, Any]:
    """
    Fetch a single URL and return its content.

    For large pages, use chunk_size to split the response at natural DOM or
    paragraph boundaries. Subsequent chunks are served from cache — no
    re-download needed.

    Args:
        url: The URL to fetch.
        use_browser: Use a real headless browser (bypasses Cloudflare, JS-heavy sites).
        proxy: Optional proxy URL (e.g. "http://user:pass@host:port").
        wait_for_selector: CSS selector to wait for before returning (browser mode only).
        timeout: Request timeout in seconds.
        format: Output format — "html" (default), "text" (plain text, ~5-10x smaller),
                or "markdown" (converted to Markdown, best for LLM reading).
        chunk_size: Split output into chunks of this many characters at DOM/paragraph
                    boundaries. Set to e.g. 20000 for large pages. None = no chunking.
        chunk_index: Which chunk to return (0-indexed). Check total_chunks in the
                     response to know how many chunks exist.
        actions: Browser-only. List of interactions to execute after page load, in order.
                 Each action is a dict with a "type" key. Supported types:
                 - {"type": "click", "selector": "button.load-more"}
                 - {"type": "scroll_to_bottom"}
                 - {"type": "fill", "selector": "#query", "value": "search term"}
                 - {"type": "press", "selector": "#query", "key": "Enter"}
                 - {"type": "wait", "ms": 1500}
                 - {"type": "wait_for_selector", "selector": ".results"}
                 The final page HTML (after all actions) is returned.
        impersonate: Optional curl-cffi browser TLS/HTTP-2 fingerprint target
                     (e.g. "chrome124") for sites that fingerprint at the edge
                     (Akamai/Cloudflare/DataDome). Must be an allowlisted
                     target; ignored if the operator set ANANSI_DISABLE_ANTIBOT.
                     Defaults to the operator's ANANSI_IMPERSONATE if unset.

    Returns:
        {url, status, content, format, content_length, chunk_index, total_chunks,
         elapsed, via_browser}
    """
    if format not in ("html", "text", "markdown"):
        return {"error": f"Invalid format {format!r}. Must be 'html', 'text', or 'markdown'."}
    if actions and len(actions) > _MAX_ACTIONS:
        return {"error": f"actions list length {len(actions)} exceeds cap {_MAX_ACTIONS}"}
    try:
        proxy = _validate_proxy(proxy)
    except UnsafeURLError as exc:
        return {"error": f"unsafe proxy: {redact_userinfo(str(exc))}"}
    try:
        impersonate = _resolve_impersonate(impersonate)
    except InvalidImpersonateError as exc:
        return {"error": f"invalid impersonate: {exc}"}
    if wait_for_selector is not None:
        try:
            validate_browser_selector(wait_for_selector)
        except ValueError as exc:
            return {"error": f"invalid wait_for_selector: {exc}"}
    return await _fetch_one(
        url,
        use_browser=use_browser,
        proxy=proxy,
        wait_for_selector=wait_for_selector,
        timeout=timeout,
        format=format,
        chunk_size=chunk_size,
        chunk_index=chunk_index,
        actions=actions,
        impersonate=impersonate,
    )


# ── Tool: fetch_urls ──────────────────────────────────────────────────────────

@mcp.tool()
async def fetch_urls(
    urls: list[str],
    use_browser: bool = False,
    proxy: str | None = None,
    timeout: float = 30.0,
    format: str = "html",
    chunk_size: int | None = None,
    concurrency: int = 5,
    impersonate: str | None = None,
) -> dict[str, Any]:
    """
    Fetch multiple URLs in one call, returning results in the same order.

    URLs are fetched concurrently up to the concurrency limit. Failures for
    individual URLs are captured per-result and do not abort the batch.
    Results are cached the same as fetch_url — subsequent chunk requests
    are free.

    Args:
        urls: List of URLs to fetch.
        use_browser: Use headless browser for all URLs (bypasses Cloudflare, renders JS).
        proxy: Proxy URL applied to every request in the batch.
        timeout: Per-request timeout in seconds.
        format: Output format for all URLs — "html", "text", or "markdown".
        chunk_size: If set, each result is split into chunks of this many characters.
                    Only chunk 0 is returned per URL; use fetch_url with chunk_index
                    to retrieve subsequent chunks (served from cache).
        concurrency: Maximum simultaneous fetches (default 5).
        impersonate: Optional allowlisted curl-cffi TLS/HTTP-2 fingerprint
                     target (e.g. "chrome124") applied to every request;
                     ignored under ANANSI_DISABLE_ANTIBOT; defaults to the
                     operator's ANANSI_IMPERSONATE.

    Returns:
        {results: [...], total, succeeded, failed}
        Each result: {url, status, content, format, content_length,
                      chunk_index, total_chunks, elapsed, via_browser}
                  or {url, error, status: null} on failure.
    """
    if format not in ("html", "text", "markdown"):
        return {"error": f"Invalid format {format!r}. Must be 'html', 'text', or 'markdown'."}
    if not urls:
        return {"results": [], "total": 0, "succeeded": 0, "failed": 0}
    if len(urls) > _MAX_URLS_PER_BATCH:
        return {"error": f"urls list length {len(urls)} exceeds cap {_MAX_URLS_PER_BATCH}"}
    try:
        concurrency = clamp_int(
            concurrency, name="concurrency", minimum=1, maximum=_MAX_CONCURRENCY
        )
        proxy = _validate_proxy(proxy)
    except (OutOfRangeError, UnsafeURLError) as exc:
        return {"error": str(exc) if isinstance(exc, OutOfRangeError)
                else f"unsafe proxy: {redact_userinfo(str(exc))}"}
    try:
        impersonate = _resolve_impersonate(impersonate)
    except InvalidImpersonateError as exc:
        return {"error": f"invalid impersonate: {exc}"}

    sem = asyncio.Semaphore(concurrency)

    async def _one(url: str) -> dict[str, Any]:
        async with sem:
            try:
                return await _fetch_one(
                    url,
                    use_browser=use_browser,
                    proxy=proxy,
                    timeout=timeout,
                    format=format,
                    chunk_size=chunk_size,
                    chunk_index=0,
                    impersonate=impersonate,
                )
            except Exception as exc:
                return {"url": url, "error": str(exc), "status": None}

    results = list(await asyncio.gather(*[_one(u) for u in urls]))
    succeeded = sum(1 for r in results if r.get("status") is not None)
    return {
        "results": results,
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
    }


# ── Tool: fetch_and_extract ───────────────────────────────────────────────────

@mcp.tool()
async def fetch_and_extract(
    url: str,
    selectors: dict[str, str],
    use_browser: bool = False,
    proxy: str | None = None,
    timeout: float = 30.0,
    impersonate: str | None = None,
) -> dict[str, Any]:
    """
    Fetch a URL and extract structured data in a single tool call.

    Combines fetch_url + extract, eliminating a round-trip when you already
    know which fields you want. The fetched HTML is cached so a follow-up
    fetch_url call for the same URL is free.

    Args:
        url: The URL to fetch.
        selectors: Field → CSS selector mapping.
                   Example: {"title": "h1.article-title", "price": ".price-tag"}
        use_browser: Use headless browser (bypasses Cloudflare, renders JS).
        proxy: Optional proxy URL.
        timeout: Request timeout in seconds.
        impersonate: Optional allowlisted curl-cffi TLS/HTTP-2 fingerprint
                     target (e.g. "chrome124"); ignored under
                     ANANSI_DISABLE_ANTIBOT; defaults to ANANSI_IMPERSONATE.

    Returns:
        {url, status, elapsed, via_browser, data: {field: value, ...}}
    """
    try:
        proxy = _validate_proxy(proxy)
    except UnsafeURLError as exc:
        return {"error": f"unsafe proxy: {redact_userinfo(str(exc))}"}
    try:
        impersonate = _resolve_impersonate(impersonate)
    except InvalidImpersonateError as exc:
        return {"error": f"invalid impersonate: {exc}"}
    result = await _fetch_one(
        url,
        use_browser=use_browser,
        proxy=proxy,
        timeout=timeout,
        format="html",
        impersonate=impersonate,
    )
    if "error" in result:
        return result

    from anansi.parser.adaptive import AdaptiveParser
    from anansi.parser.structured import extract_all as _extract_all_structured
    from bs4 import BeautifulSoup as _BS
    parser = AdaptiveParser()
    data = await parser.extract(result["content"], selectors, url=url)
    structured_data = _extract_all_structured(_BS(result["content"], "lxml"))
    return {
        "url": result["url"],
        "status": result["status"],
        "elapsed": result["elapsed"],
        "via_browser": result["via_browser"],
        "data": data,
        "structured_data": structured_data,
    }


# ── Tool: extract ─────────────────────────────────────────────────────────────

@mcp.tool()
async def extract(
    html: str,
    selectors: dict[str, str],
    url: str = "",
) -> dict[str, Any]:
    """
    Extract structured data from HTML using adaptive CSS selectors.

    The parser remembers which selectors work for each URL pattern and
    automatically tries to heal broken selectors using fuzzy matching.

    Args:
        html: Raw HTML string to parse.
        selectors: Mapping of field_name → CSS selector.
                   Example: {"title": "h1.article-title", "price": ".price-tag"}
        url: The page URL (used to scope selector memory). Optional but recommended.

    Returns:
        Dict mapping each field name to its extracted text value (or null).
    """
    # MCP-supplied HTML is untrusted and unbounded. Refuse to parse anything
    # larger than _MAX_HTML_BYTES before it reaches BeautifulSoup, which would
    # otherwise materialise a multi-megabyte tree.
    if isinstance(html, str) and len(html.encode("utf-8", errors="ignore")) > _MAX_HTML_BYTES:
        return {"error": f"html exceeds {_MAX_HTML_BYTES}-byte cap"}
    from anansi.parser.adaptive import AdaptiveParser
    parser = AdaptiveParser()
    css_data = await parser.extract(html, selectors, url=url)
    structured_data = await parser.extract_structured(html)
    return {**css_data, "structured_data": structured_data}


# ── Tool: crawl_site ──────────────────────────────────────────────────────────

@mcp.tool()
async def crawl_site(
    start_url: str,
    link_pattern: str = ".*",
    selectors: dict[str, str] | None = None,
    max_pages: int = 50,
    max_depth: int | None = None,
    concurrency: int = 3,
    delay: float = 1.0,
    use_browser: bool = False,
    proxies: list[str] | None = None,
    cookies: dict[str, str] | None = None,
    auth_headers: dict[str, str] | None = None,
    use_sitemap: bool = False,
    deduplicate_content: bool = False,
    auto_paginate: bool = False,
    allowed_domains: list[str] | None = None,
    deny_patterns: list[str] | None = None,
    max_duration_seconds: float | None = None,
    forward_credentials_cross_origin: bool = False,
    impersonate: str | None = None,
) -> dict[str, Any]:
    """
    Crawl a website and extract structured data from every page.

    Starts a background crawl and returns a crawl_id you can use with
    get_crawl_items, crawl_metrics, or pause_crawl. Items are persisted to
    SQLite so they survive process restarts.

    Args:
        start_url: The entry-point URL for the crawl.
        link_pattern: Regex pattern — only URLs matching this are followed.
        selectors: CSS selectors to extract from each page (field → selector).
        max_pages: Maximum number of pages to crawl.
        max_depth: Maximum link-follow depth from start_url (None = unlimited).
        concurrency: Number of simultaneous fetches.
        delay: Polite delay between requests (seconds).
        use_browser: Use headless browser for all fetches.
        proxies: List of proxy URLs for rotation.
        cookies: Session cookies to send with every request (for logged-in crawls).
                 Example: {"session": "abc123", "csrf": "xyz"}
        auth_headers: HTTP headers to send with every request.
                      Example: {"Authorization": "Bearer token123"}
        use_sitemap: Discover URLs from /sitemap.xml instead of following links.
                     More efficient for large sites with a well-maintained sitemap.
        deduplicate_content: Skip pages whose HTML content is identical to a page
                             already scraped in this crawl (MD5 fingerprint check).
        auto_paginate: Automatically detect and follow "next page" links using
                       heuristics (rel=next, text patterns, query-string page params).
        allowed_domains: Restrict crawling to these domains (subdomains included).
                         Example: ["example.com"] allows "blog.example.com" too.
                         When omitted (default) the crawl is scoped to the
                         registrable domain of *start_url* — a deliberate
                         narrowing to prevent off-domain credential leakage.
        deny_patterns: Reject URLs matching any of these regex patterns.
                       Example: [r"/admin/", r"\\.pdf$"]
        max_duration_seconds: Stop the crawl after this many seconds regardless of
                              how many pages have been visited (None = no time limit).
        forward_credentials_cross_origin: When True, *cookies* and
                                          *auth_headers* are sent to every URL,
                                          including off-domain links. Default
                                          False; credentials are stripped on
                                          any request to a host not sharing
                                          start_url's registrable domain.
        impersonate: Optional allowlisted curl-cffi TLS/HTTP-2 fingerprint
                     target (e.g. "chrome124") applied to every HTTP fetch in
                     the crawl, for edge-fingerprinting WAFs (Akamai/Cloudflare/
                     DataDome). Ignored under ANANSI_DISABLE_ANTIBOT; defaults
                     to the operator's ANANSI_IMPERSONATE.

    Returns:
        {crawl_id, status, message, start_url, max_pages}
    """
    from anansi.core import Item, Request, Response
    from anansi.parser.adaptive import AdaptiveParser
    from anansi.spider.crawler import Crawler
    from anansi.spider.spider import Spider

    # Cap concurrent crawls before doing anything else; failing here ensures
    # an attacker cannot exhaust the per-process active-crawl registry.
    if len(_active_crawlers) >= _MAX_ACTIVE_CRAWLS:
        return {"error": f"active crawl cap reached ({_MAX_ACTIVE_CRAWLS}); "
                          "wait for a crawl to finish or call delete_crawl"}

    # Validate start_url against SSRF policy before doing anything else.
    # Private/loopback/metadata ranges are rejected unless the operator opted
    # in via the ANANSI_ALLOW_PRIVATE_NETWORKS env var (never the MCP client).
    try:
        _validate_url(start_url)
    except UnsafeURLError as exc:
        return {"error": f"unsafe start_url: {exc}"}

    try:
        impersonate = _resolve_impersonate(impersonate)
    except InvalidImpersonateError as exc:
        return {"error": f"invalid impersonate: {exc}"}

    # Validate regex inputs — link_pattern and each deny_pattern — against an
    # obvious-ReDoS heuristic and a length cap. Catastrophic-backtracking
    # patterns can stall the crawler.
    try:
        validate_regex(link_pattern)
        for pat in deny_patterns or []:
            validate_regex(pat)
    except UnsafeRegexError as exc:
        return {"error": f"unsafe regex: {exc}"}

    # Per-tool resource caps on numeric arguments.
    try:
        max_pages = clamp_int(max_pages, name="max_pages", minimum=1, maximum=_MAX_PAGES)
        max_depth = clamp_int(max_depth, name="max_depth", minimum=0, maximum=_MAX_DEPTH)
        concurrency = clamp_int(
            concurrency, name="concurrency", minimum=1, maximum=_MAX_CONCURRENCY
        )
    except OutOfRangeError as exc:
        return {"error": str(exc)}

    # Validate every proxy in the rotation pool.
    if proxies:
        try:
            for p in proxies:
                validate_proxy_url(p, allow_private=security.ALLOW_PRIVATE_NETWORKS)
        except UnsafeURLError as exc:
            return {"error": f"unsafe proxy: {redact_userinfo(str(exc))}"}

    crawl_id = str(uuid.uuid4())
    _sel = selectors or {}
    _pattern = link_pattern
    _use_browser = use_browser
    _use_sitemap = use_sitemap
    _auto_paginate = auto_paginate
    # Default credential scope: restrict to the start_url's registrable domain
    # so cookies/auth_headers can't leak via the first off-domain link.
    start_host = urlparse(start_url).hostname or ""
    if allowed_domains:
        _allowed_domains = allowed_domains
    elif start_host:
        # Use the last two labels as a cheap public-suffix-free heuristic.
        labels = start_host.split(".")
        _allowed_domains = [".".join(labels[-2:])] if len(labels) >= 2 else [start_host]
    else:
        _allowed_domains = []
    _deny_patterns = deny_patterns or []

    class _DynamicSpider(Spider):
        name = f"mcp_{crawl_id[:8]}"
        start_urls = [start_url]
        rules = [(_pattern, "parse", True)]
        use_sitemap = _use_sitemap
        auto_paginate = _auto_paginate
        allowed_domains = _allowed_domains
        deny_patterns = _deny_patterns

        async def parse(self, response: Response):
            if _sel:
                parser = AdaptiveParser()
                data = await parser.extract(response.html, _sel, url=response.url)
                if any(v is not None for v in data.values()):
                    yield Item(data=data, source_url=response.url, spider_name=self.name)

            # Follow links matching pattern
            for req in self.follow_links(response):
                if _use_browser:
                    req.meta["use_browser"] = True
                yield req

    proxy_manager = None
    if proxies:
        from anansi.proxy.manager import ProxyManager
        proxy_manager = ProxyManager(proxies)
        await proxy_manager.start()

    fetcher = None
    if use_browser:
        from anansi.fetchers.browser import BrowserFetcher
        fetcher = BrowserFetcher()

    # When the caller has not opted into cross-origin credential forwarding,
    # confine cookies and auth headers to the registrable domain of start_url.
    scope_host = None if forward_credentials_cross_origin else (start_host or None)

    crawler = Crawler(
        _DynamicSpider,
        concurrency=concurrency,
        delay=delay,
        max_pages=max_pages,
        max_depth=max_depth,
        max_duration_seconds=max_duration_seconds,
        fetcher=fetcher,
        proxy_manager=proxy_manager,
        crawl_id=crawl_id,
        cookies=cookies,
        auth_headers=auth_headers,
        credential_scope_host=scope_host,
        deduplicate_content=deduplicate_content,
        impersonate=impersonate,
    )
    _active_crawlers[crawl_id] = crawler

    async def _run():
        try:
            async for _ in crawler.run():
                pass  # items are persisted to SQLite by the crawler
        finally:
            if proxy_manager:
                await proxy_manager.stop()
            if fetcher:
                await fetcher.close()
            # Drop the crawler/task entries when the run finishes so the
            # registry does not grow unboundedly across many crawl_site calls.
            _active_crawlers.pop(crawl_id, None)
            _crawl_tasks.pop(crawl_id, None)

    task = asyncio.create_task(_run())
    _crawl_tasks[crawl_id] = task

    await asyncio.sleep(0)

    return {
        "crawl_id": crawl_id,
        "status": "running",
        "message": (
            f"Crawl started. Use get_crawl_items('{crawl_id}') to retrieve results "
            f"or export_crawl('{crawl_id}') to export as CSV/JSON."
        ),
        "start_url": start_url,
        "max_pages": max_pages,
        "max_depth": max_depth,
        "max_duration_seconds": max_duration_seconds,
    }


# ── Tool: get_crawl_items ─────────────────────────────────────────────────────

@mcp.tool()
async def get_crawl_items(
    crawl_id: str,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """
    Retrieve items collected by a running or completed crawl.

    Items are persisted to SQLite, so this works even after the process restarts.
    Use offset to page through large result sets.

    Args:
        crawl_id: The crawl ID returned by crawl_site.
        limit: Maximum number of items to return (default 100).
        offset: Number of items to skip (for pagination).

    Returns:
        {crawl_id, status, items_count, items: [{data, source_url, ...}]}
    """
    from anansi.spider.crawler import Crawler

    try:
        limit = clamp_int(limit, name="limit", minimum=1, maximum=_MAX_GET_ITEMS)
        offset = clamp_int(offset, name="offset", minimum=0, maximum=10**9)
    except OutOfRangeError as exc:
        return {"error": str(exc)}

    crawler = _active_crawlers.get(crawl_id)
    task = _crawl_tasks.get(crawl_id)

    status = "unknown"
    if crawler:
        status = crawler.state.value
    elif task and task.done():
        status = "finished" if not task.exception() else "error"

    if status == "unknown":
        crawls = await Crawler.list_crawls()
        info = next((c for c in crawls if c["crawl_id"] == crawl_id), None)
        if info is None:
            return {"error": f"Crawl '{crawl_id}' not found"}
        status = info["state"]

    items = await Crawler.get_items(crawl_id, limit=limit, offset=offset)
    total = crawler._items_count if crawler else len(items)

    return {
        "crawl_id": crawl_id,
        "status": status,
        "items_returned": len(items),
        "items_count": total,
        "offset": offset,
        "items": items,
    }


# ── Tool: export_crawl ────────────────────────────────────────────────────────

@mcp.tool()
async def export_crawl(
    crawl_id: str,
    format: str = "jsonl",
    path: str | None = None,
) -> dict[str, Any]:
    """
    Export all collected items from a crawl to a file or return them as a string.

    Supports JSONL (one JSON object per line), JSON (array), and CSV formats.

    Args:
        crawl_id: The crawl ID returned by crawl_site.
        format: Output format — "jsonl" (default), "json", or "csv".
        path: File path to write the output to. If omitted, the serialised
              data is returned directly in the response (suitable for small crawls).

    Returns:
        {crawl_id, format, rows, path} if path given, or
        {crawl_id, format, rows, content} if no path.
    """
    from anansi.spider.crawler import Crawler

    rows = await Crawler.get_items(crawl_id, limit=100_000)
    if not rows and not (await Crawler.list_crawls()):
        return {"error": f"Crawl '{crawl_id}' not found"}

    # Confine the export path to ~/.anansi/exports/. Any '..' segment or
    # absolute path that escapes the sandbox is rejected. An attacker-controlled
    # ``path`` cannot otherwise be allowed to flow into Path.write_text().
    confined_path: str | None = None
    if path:
        try:
            confined_path = str(confine_to_dir(path, _EXPORT_ROOT))
        except PathOutsideSandboxError as exc:
            return {"error": f"export path rejected: {exc}"}

    out = await Crawler.export_items(crawl_id, fmt=format, path=confined_path)
    result: dict[str, Any] = {"crawl_id": crawl_id, "format": format, "rows": len(rows)}
    if confined_path:
        result["path"] = confined_path
    else:
        result["content"] = out
    return result


# ── Tool: crawl_metrics ───────────────────────────────────────────────────────

@mcp.tool()
async def crawl_metrics(crawl_id: str) -> dict[str, Any]:
    """
    Return live performance metrics for a running or completed crawl.

    Args:
        crawl_id: The crawl ID returned by crawl_site.

    Returns:
        {crawl_id, status, pages_visited, pages_pending, pages_failed,
         items_collected, elapsed_seconds, pages_per_second, error_rate}
    """
    from anansi.spider.crawler import Crawler
    from anansi.spider.queue import SQLiteQueue

    queue = SQLiteQueue(crawl_id)
    visited = await queue.visited_count()
    pending = await queue.pending_count()
    failed = await queue.failed_count()

    crawler = _active_crawlers.get(crawl_id)
    task = _crawl_tasks.get(crawl_id)
    items = crawler._items_count if crawler else 0

    status = "unknown"
    if crawler:
        status = crawler.state.value
    elif task and task.done():
        status = "finished" if not task.exception() else "error"

    # Pull created_at from persistent store for elapsed calculation
    elapsed: float | None = None
    pages_per_second: float | None = None
    crawls = await Crawler.list_crawls()
    info = next((c for c in crawls if c["crawl_id"] == crawl_id), None)
    if info:
        status = status if crawler else info["state"]
        try:
            import datetime
            created = datetime.datetime.fromisoformat(info["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=datetime.timezone.utc)
            elapsed = (datetime.datetime.now(tz=datetime.timezone.utc) - created).total_seconds()
            if elapsed > 0 and visited > 0:
                pages_per_second = round(visited / elapsed, 3)
        except Exception:
            pass

    total_attempted = visited + failed
    error_rate = round(failed / total_attempted, 3) if total_attempted > 0 else 0.0

    return {
        "crawl_id": crawl_id,
        "status": status,
        "pages_visited": visited,
        "pages_pending": pending,
        "pages_failed": failed,
        "items_collected": items,
        "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
        "pages_per_second": pages_per_second,
        "error_rate": error_rate,
        "unchanged_pages": crawler._unchanged_pages if crawler else 0,
        "valid_items": crawler._valid_items if crawler else 0,
        "invalid_items": crawler._invalid_items if crawler else 0,
        "validation_error_rate": round(
            (crawler._invalid_items / max(crawler._valid_items + crawler._invalid_items, 1))
            if crawler else 0.0,
            3,
        ),
    }


# ── Tool: pause_crawl ─────────────────────────────────────────────────────────

@mcp.tool()
async def pause_crawl(crawl_id: str) -> dict[str, Any]:
    """
    Pause a running crawl. The crawler will finish in-flight requests then stop.

    The crawl state is persisted to SQLite so it can be resumed later, even
    after the process restarts.

    Args:
        crawl_id: The crawl ID to pause.

    Returns:
        {crawl_id, status}
    """
    crawler = _active_crawlers.get(crawl_id)
    if crawler is None:
        return {"error": f"No active crawl with id '{crawl_id}'"}
    crawler.pause()
    return {"crawl_id": crawl_id, "status": "paused"}


# ── Tool: resume_crawl ────────────────────────────────────────────────────────

@mcp.tool()
async def resume_crawl(crawl_id: str) -> dict[str, Any]:
    """
    Resume a paused crawl (within the same process).

    For cross-process resume (after restart), use crawl_site with the same
    start URL — the SQLite queue will pick up where it left off.

    Args:
        crawl_id: The crawl ID to resume.

    Returns:
        {crawl_id, status}
    """
    crawler = _active_crawlers.get(crawl_id)
    if crawler is None:
        return {"error": f"No active crawl with id '{crawl_id}'"}
    crawler.resume_in_place()
    return {"crawl_id": crawl_id, "status": "running"}


# ── Tool: list_crawls ─────────────────────────────────────────────────────────

@mcp.tool()
async def list_crawls() -> dict[str, Any]:
    """
    List all crawls (active, paused, and completed) from the persistent store.

    Returns:
        {crawls: [{crawl_id, spider_name, state, items_count, created_at, updated_at}]}
    """
    from anansi.spider.crawler import Crawler
    crawls = await Crawler.list_crawls()

    # Annotate with live status from in-memory registry
    for c in crawls:
        cid = c["crawl_id"]
        if cid in _active_crawlers:
            c["state"] = _active_crawlers[cid].state.value

    return {"crawls": crawls}


# ── Tool: selector_health ─────────────────────────────────────────────────────

@mcp.tool()
async def selector_health(
    url_pattern: str,
    field_name: str,
) -> dict[str, Any]:
    """
    Inspect the learned selector history for a URL pattern and field.

    Shows all known selectors sorted by confidence score, so you can see
    which selectors Anansi has learned to use (or avoid) for a given page type.

    Args:
        url_pattern: Host + path pattern (e.g. "example.com/products/{id}").
        field_name: The field name to inspect (e.g. "price").

    Returns:
        {url_pattern, field_name, selectors: [{selector, confidence, success_count, failure_count}]}
    """
    from anansi.parser.adaptive import AdaptiveParser
    parser = AdaptiveParser()
    selectors = await parser.known_selectors(url_pattern, field_name)
    return {
        "url_pattern": url_pattern,
        "field_name": field_name,
        "selectors": selectors,
    }


# ── Tool: cancel_crawl ────────────────────────────────────────────────────────

@mcp.tool()
async def cancel_crawl(crawl_id: str) -> dict[str, Any]:
    """
    Permanently cancel a running or paused crawl.

    Unlike pause_crawl, cancellation is irreversible. The crawl state is
    marked as "cancelled" in the persistent store. Use crawl_site to start
    a fresh crawl on the same URL.

    Args:
        crawl_id: The crawl ID to cancel.

    Returns:
        {crawl_id, status}
    """
    crawler = _active_crawlers.get(crawl_id)
    task = _crawl_tasks.get(crawl_id)

    if crawler is None and task is None:
        return {"error": f"No active crawl with id '{crawl_id}'"}

    if crawler is not None:
        crawler.cancel()

    if task is not None and not task.done():
        task.cancel()

    _active_crawlers.pop(crawl_id, None)
    _crawl_tasks.pop(crawl_id, None)
    return {"crawl_id": crawl_id, "status": "cancelled"}


# ── Tool: screenshot_url ──────────────────────────────────────────────────────

@mcp.tool()
async def screenshot_url(
    url: str,
    selector: str | None = None,
    full_page: bool = False,
    path: str | None = None,
    proxy: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """
    Capture a screenshot of a web page using a headless browser.

    Useful for visually inspecting JS-rendered pages, verifying selectors,
    and debugging layout issues. Returns a base64-encoded PNG by default,
    or writes to a file if path is specified.

    Args:
        url: The page URL to screenshot.
        selector: CSS selector of a specific element to capture (optional).
        full_page: Capture the entire scrollable page (default: visible viewport only).
        path: File path to save the PNG. If omitted, returns base64 data.
        proxy: Proxy URL to use for this request.
        timeout: Navigation timeout in seconds (default 30).

    Returns:
        {url, format, width, height, data_b64} or {url, format, width, height, path}

    Security: this tool enforces the same validation as fetch_url — the URL
    and proxy are SSRF-checked (private/loopback/metadata addresses are
    rejected unless the operator set ANANSI_ALLOW_PRIVATE_NETWORKS), the
    selector must be plain CSS, and any output path is confined to
    ~/.anansi/exports/.
    """
    # SSRF guard on the navigation target (parity with fetch_url).
    try:
        _validate_url(url)
    except UnsafeURLError as exc:
        return {"url": url, "error": f"unsafe URL: {exc}", "status": None}

    # SSRF guard on the proxy.
    try:
        proxy = _validate_proxy(proxy)
    except UnsafeURLError as exc:
        return {"error": f"unsafe proxy: {redact_userinfo(str(exc))}"}

    # CSS-only selector (no xpath=/text= engine prefixes or >> chaining).
    if selector is not None:
        try:
            validate_browser_selector(selector)
        except ValueError as exc:
            return {"error": f"invalid selector: {exc}"}

    # Bound the navigation timeout.
    try:
        timeout = clamp_int(int(timeout), name="timeout", minimum=1, maximum=120)
    except OutOfRangeError as exc:
        return {"error": str(exc)}

    # Confine the output path to the export sandbox; an attacker-controlled
    # path must not reach Path.write_bytes() (arbitrary file write).
    confined_path: str | None = None
    if path:
        try:
            confined_path = str(confine_to_dir(path, _EXPORT_ROOT))
        except PathOutsideSandboxError as exc:
            return {"error": f"screenshot path rejected: {exc}"}

    from anansi.fetchers.browser import BrowserFetcher
    bf = BrowserFetcher()
    try:
        return await bf.screenshot(
            url,
            selector=selector,
            full_page=full_page,
            path=confined_path,
            proxy=proxy,
            timeout=timeout,
        )
    finally:
        await bf.close()


# ── Tool: train_selector ──────────────────────────────────────────────────────

@mcp.tool()
async def train_selector(
    url_pattern: str,
    field_name: str,
    selector: str,
    selector_type: str = "css",
) -> dict[str, Any]:
    """
    Manually teach Anansi a correct CSS selector for a URL pattern and field.

    The selector is stored at confidence 1.0, making it the top candidate for
    future extractions on matching pages. Use this to pre-seed knowledge,
    correct a wrong selector, or provide hints before running a crawl.

    Args:
        url_pattern: Host + path pattern (e.g. "shop.example.com/products/{id}").
                     Use the same normalised form shown by selector_health.
        field_name: The field this selector extracts (e.g. "price", "title").
        selector: CSS selector string (e.g. ".product-price span").
        selector_type: "css" (default), "xpath", or "text".

    Returns:
        {url_pattern, field_name, selector, selector_type, confidence}

    Security: selector_type is allowlisted; a "text" selector is compiled as a
    regex during selector healing, so it is run through the ReDoS heuristic; a
    "css" selector must be plain CSS (no Playwright engine prefixes); url_pattern
    and field_name are length-bounded.
    """
    if selector_type not in ("css", "xpath", "text"):
        return {"error": f"selector_type {selector_type!r} not allowed; "
                          "use 'css', 'xpath', or 'text'"}
    # url_pattern is stored verbatim as an exact-match DB key, and field_name
    # likewise — bound both so the untrusted client cannot write unbounded rows.
    if len(url_pattern) > 2_000 or len(field_name) > 500:
        return {"error": "url_pattern or field_name too long"}
    if selector_type == "text":
        # A text selector is re.compile()'d and matched against page content
        # during healing — gate it with the ReDoS heuristic + length cap.
        try:
            validate_regex(selector)
        except UnsafeRegexError as exc:
            return {"error": f"unsafe text selector: {exc}"}
    elif selector_type == "css":
        try:
            validate_browser_selector(selector)
        except ValueError as exc:
            return {"error": f"invalid selector: {exc}"}

    from anansi.parser.adaptive import AdaptiveParser
    parser = AdaptiveParser()
    return await parser.train(url_pattern, field_name, selector, selector_type)


# ── Tool: validate_selector ───────────────────────────────────────────────────

@mcp.tool()
async def validate_selector(
    url: str,
    selectors: dict[str, str],
    use_browser: bool = False,
) -> dict[str, Any]:
    """
    Test CSS selectors against a live page without affecting selector confidence scores.

    Fetches the page (using the cache if available) and runs each selector,
    returning the extracted values. Use this to check selectors before launching
    an expensive crawl.

    Args:
        url: The page URL to test against.
        selectors: Map of field name → CSS selector (e.g. {"price": ".prod-price"}).
        use_browser: Use headless browser for JS-rendered pages.

    Returns:
        {url, results: {field: {value, selector}}}
    """
    import time as _time_mod
    from anansi.parser.adaptive import AdaptiveParser, SelectorConfig

    result = await _fetch_one(url, use_browser=use_browser)
    if result.get("error"):
        return {"url": url, "error": result["error"]}

    html = result["content"] if result.get("format") == "html" else ""
    if not html:
        # Re-fetch as HTML for extraction
        result = await _fetch_one(url, use_browser=use_browser, format="html")
        html = result.get("content", "")

    parser = AdaptiveParser()
    t0 = _time_mod.perf_counter()
    data = await parser.extract(html, selectors, url=url, use_structured=False)
    elapsed = round(_time_mod.perf_counter() - t0, 3)

    return {
        "url": url,
        "elapsed": elapsed,
        "results": {
            field: {"value": value, "selector": selectors[field]}
            for field, value in data.items()
        },
    }


# ── Tool: clear_cache ─────────────────────────────────────────────────────────

@mcp.tool()
async def clear_cache(url: str | None = None) -> dict[str, Any]:
    """
    Invalidate the in-memory page cache.

    The fetch cache stores pages for 5 minutes to avoid redundant downloads.
    Use this when you need fresh content before the TTL expires.

    Args:
        url: Specific URL to remove from the cache. If omitted, clears everything.

    Returns:
        {cleared: N} where N is the number of cache entries removed.
    """
    if url is None:
        count = len(_page_cache)
        _page_cache.clear()
        return {"cleared": count}

    removed = 0
    for key in list(_page_cache.keys()):
        if key[0] == url:
            del _page_cache[key]
            removed += 1
    return {"cleared": removed}


# ── Entry point ───────────────────────────────────────────────────────────────

def run() -> None:
    """Run the MCP server (stdio by default; use --transport sse for HTTP/SSE)."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="anansi-mcp",
        description="Anansi MCP server — expose web scraping tools to any LLM.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport to use: stdio (default, for local clients) or sse (HTTP, for remote clients).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind when using SSE transport (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to listen on when using SSE transport (default: 8000).",
    )
    args = parser.parse_args()

    if args.transport == "sse":
        print(
            f"Anansi MCP server listening on http://{args.host}:{args.port}/sse",
            flush=True,
        )
        try:
            mcp.run(transport="sse", host=args.host, port=args.port)
        except KeyboardInterrupt:
            pass
        return

    # stdio transport — fix Windows binary-mode issue before starting
    if sys.platform == "win32":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

    try:
        mcp.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
