"""Tests for the improved Cloudflare bypass — block detection, human simulation,
improved Turnstile interaction, cf_clearance cookie check, and fresh-context retry.
"""

from __future__ import annotations

import asyncio

import pytest

from anansi.fetchers.browser import (
    BrowserFetcher,
    _CF_BLOCK_INDICATORS,
    _CF_INDICATORS,
)


# ── Block vs challenge classification ────────────────────────────────────────

def test_cf_block_indicators_not_empty() -> None:
    assert len(_CF_BLOCK_INDICATORS) >= 5


def test_is_cloudflare_block_true_on_known_phrase() -> None:
    fetcher = BrowserFetcher()
    assert fetcher._is_cloudflare_block("Sorry, you have been blocked")
    assert fetcher._is_cloudflare_block("Error 1020 Access Denied")
    assert fetcher._is_cloudflare_block("You are unable to access this site")


def test_is_cloudflare_block_false_on_normal_content() -> None:
    fetcher = BrowserFetcher()
    assert not fetcher._is_cloudflare_block("<html><body>Hello world</body></html>")


def test_is_cloudflare_challenge_unchanged() -> None:
    fetcher = BrowserFetcher()
    assert fetcher._is_cloudflare_challenge("Just a moment cf-turnstile")
    assert not fetcher._is_cloudflare_challenge("<html>normal page</html>")


# ── _simulate_idle_human ─────────────────────────────────────────────────────

async def test_simulate_idle_human_calls_mouse_move() -> None:
    fetcher = BrowserFetcher()
    moves: list[tuple[float, float]] = []
    evaluates: list[str] = []

    class _FakePage:
        class mouse:
            @staticmethod
            async def move(x: float, y: float) -> None:
                moves.append((x, y))

        @staticmethod
        async def evaluate(script: str) -> None:
            evaluates.append(script)

    await fetcher._simulate_idle_human(_FakePage())
    assert len(moves) >= 2  # initial move + at least one jitter step


# ── _wait_for_cloudflare — hard block fast-fail ───────────────────────────────

async def test_wait_for_cloudflare_returns_immediately_on_hard_block(
    monkeypatch,
) -> None:
    """A hard-block page must not spin for 45 s — return immediately."""
    from anansi import security
    monkeypatch.setattr(security, "DISABLE_ANTIBOT", False)

    fetcher = BrowserFetcher(cf_wait_timeout=45.0)

    class _FakePage:
        frames = []

        async def content(self) -> str:
            return "Sorry, you have been blocked by Cloudflare Error 1020"

        async def mouse_move(self, *a, **kw) -> None:
            pass

        class mouse:
            @staticmethod
            async def move(x, y) -> None:
                pass

        @staticmethod
        async def evaluate(script: str) -> None:
            pass

    import time
    t0 = time.monotonic()
    # Must not raise and must return in well under the 45s timeout
    await fetcher._wait_for_cloudflare(_FakePage())
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"hard-block should return fast, took {elapsed:.1f}s"


async def test_wait_for_cloudflare_skipped_when_antibot_disabled(monkeypatch) -> None:
    from anansi import security
    monkeypatch.setattr(security, "DISABLE_ANTIBOT", True)

    fetcher = BrowserFetcher()

    class _FakePage:
        frames = []
        async def content(self) -> str:
            # This would spin if DISABLE_ANTIBOT wasn't respected
            return "Just a moment cf-turnstile __cf_chl"
        class mouse:
            @staticmethod
            async def move(x, y) -> None:
                pass
        @staticmethod
        async def evaluate(script: str) -> None:
            pass

    # Must return immediately without polling
    import time
    t0 = time.monotonic()
    await fetcher._wait_for_cloudflare(_FakePage())
    assert time.monotonic() - t0 < 1.0


async def test_wait_for_cloudflare_accepts_cf_clearance_cookie(monkeypatch) -> None:
    """If cf_clearance cookie is set, treat challenge as resolved immediately."""
    from anansi import security
    monkeypatch.setattr(security, "DISABLE_ANTIBOT", False)

    fetcher = BrowserFetcher(cf_wait_timeout=30.0)
    poll_count = 0

    class _FakeContext:
        async def cookies(self):
            return [{"name": "cf_clearance", "value": "abc123"}]

    class _FakePage:
        frames = []
        context = _FakeContext()

        async def content(self) -> str:
            nonlocal poll_count
            poll_count += 1
            # Still showing challenge markers, but cookie is set
            return "Just a moment cf-turnstile __cf_chl"

        class mouse:
            @staticmethod
            async def move(x, y) -> None:
                pass

        @staticmethod
        async def evaluate(script: str) -> None:
            pass

    import time
    t0 = time.monotonic()
    await fetcher._wait_for_cloudflare(_FakePage())
    elapsed = time.monotonic() - t0
    # Should have resolved on the first poll (after initial content check)
    assert elapsed < 10.0
    assert poll_count <= 2


# ── _get_context force_fresh ─────────────────────────────────────────────────

async def test_force_fresh_bypasses_pool() -> None:
    """force_fresh=True must create a new context even when the pool has one."""
    fetcher = BrowserFetcher(max_contexts=2)
    fetcher._context_pool = asyncio.Queue(maxsize=2)

    class _PooledContext:
        closed = False
        async def close(self): self.closed = True
        async def clear_cookies(self): pass
        async def clear_permissions(self): pass

    pooled = _PooledContext()
    fetcher._context_pool.put_nowait((pooled, 0.0, 0))

    # force_fresh should not touch the pooled context
    created_contexts: list[Any] = []

    class _FakeBrowser:
        async def new_context(self, **kwargs):
            ctx = _PooledContext()
            created_contexts.append(ctx)
            return ctx

    fetcher._browser = _FakeBrowser()
    fetcher._context_semaphore = asyncio.Semaphore(2)

    from anansi import security
    original_disable = security.DISABLE_ANTIBOT
    security.DISABLE_ANTIBOT = True  # skip stealth injection in this unit test
    try:
        async with fetcher._get_context(force_fresh=True) as ctx:
            pass
    finally:
        security.DISABLE_ANTIBOT = original_disable

    assert len(created_contexts) == 1, "force_fresh should have created one new context"
    assert not fetcher._context_pool.empty(), "pooled context should still be in pool"
    assert created_contexts[0].closed, "force_fresh context should be closed after use"


from typing import Any
