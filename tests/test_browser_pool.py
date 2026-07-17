"""Tests for the shared BrowserFetcher pool in the MCP server.

These guard the efficiency refactor that reuses one BrowserFetcher (and its single
Chromium launch) per bot_profile across tool calls instead of building one per call.
BrowserFetcher is mocked so no real browser is launched.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import anansi.mcp_server.server as srv


async def test_pooled_per_bot_profile() -> None:
    """Same instance per key across calls; distinct instances per bot_profile."""
    with patch("anansi.fetchers.browser.BrowserFetcher", side_effect=lambda **kw: MagicMock()) as MockBF:
        a1 = await srv._get_browser_fetcher(None)
        a2 = await srv._get_browser_fetcher(None)
        b = await srv._get_browser_fetcher("googlebot")
        assert a1 is a2
        assert a1 is not b
        assert MockBF.call_count == 2  # one per distinct key, not per call


async def test_single_construction_under_concurrency() -> None:
    """Concurrent first-calls for one key construct exactly one BrowserFetcher
    (one Chromium), thanks to the per-loop creation lock + double-check."""
    with patch("anansi.fetchers.browser.BrowserFetcher", side_effect=lambda **kw: MagicMock()) as MockBF:
        results = await asyncio.gather(*(srv._get_browser_fetcher(None) for _ in range(10)))
        assert all(r is results[0] for r in results)
        assert MockBF.call_count == 1


async def test_close_browser_fetchers_clears_pool() -> None:
    instance = MagicMock()
    instance.close = AsyncMock()
    with patch("anansi.fetchers.browser.BrowserFetcher", return_value=instance):
        await srv._get_browser_fetcher(None)
    assert srv._browser_fetchers  # populated
    await srv._close_browser_fetchers()
    assert not srv._browser_fetchers  # cleared
    instance.close.assert_awaited_once()
