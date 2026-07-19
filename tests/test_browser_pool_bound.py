"""BrowserFetcher bounds its per-session context pools with LRU eviction (#13)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from anansi.fetchers.browser import BrowserFetcher


async def test_lru_evicts_and_closes_idle_contexts() -> None:
    bf = BrowserFetcher()
    bf._max_session_pools = 2

    ctx_a = MagicMock()
    ctx_a.close = AsyncMock()
    pool_a = await bf._acquire_pool("a")
    pool_a.put_nowait((ctx_a, 0.0, 0))  # an idle context parked in pool "a"

    await bf._acquire_pool("b")  # pools: a, b
    await bf._acquire_pool("c")  # over cap → evict LRU ("a")

    assert set(bf._session_pools.keys()) == {"b", "c"}
    ctx_a.close.assert_awaited_once()  # evicted pool's idle context was closed


async def test_reuse_promotes_most_recently_used() -> None:
    bf = BrowserFetcher()
    bf._max_session_pools = 2
    await bf._acquire_pool("a")
    await bf._acquire_pool("b")
    await bf._acquire_pool("a")  # touch "a" → now MRU, "b" is LRU
    await bf._acquire_pool("c")  # evict "b"

    assert set(bf._session_pools.keys()) == {"a", "c"}


async def test_anonymous_pool_is_not_capped() -> None:
    bf = BrowserFetcher()
    # session_key=None returns the shared anonymous pool (None until browser up),
    # and never populates _session_pools.
    assert await bf._acquire_pool(None) is bf._context_pool
    assert len(bf._session_pools) == 0
