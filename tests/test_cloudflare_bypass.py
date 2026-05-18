"""Tests for the improved Cloudflare bypass — block detection, human simulation,
improved Turnstile interaction, cf_clearance cookie check, fresh-context retry,
5-second grace period, and stale-progress early-exit guard.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from anansi.fetchers.browser import (
    BrowserFetcher,
    _CF_BLOCK_INDICATORS,
    _CF_INDICATORS,
    _USER_AGENTS,
    _WEBRTC_BLOCK_JS,
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


# ── User agent currency ───────────────────────────────────────────────────────

def test_user_agents_are_chrome_131() -> None:
    for ua in _USER_AGENTS:
        assert "Chrome/131" in ua or "Edg/131" in ua, (
            f"UA is not Chrome/Edge 131: {ua}"
        )


# ── WebRTC block JS preserves mediaDevices ────────────────────────────────────

def test_webrtc_block_js_does_not_remove_media_devices() -> None:
    assert "mediaDevices = undefined" not in _WEBRTC_BLOCK_JS
    assert "enumerateDevices" in _WEBRTC_BLOCK_JS


def test_webrtc_block_js_blocks_ice_servers() -> None:
    assert "iceServers" in _WEBRTC_BLOCK_JS


# ── Grace period resolves IUAM without a click ───────────────────────────────

async def test_wait_for_cloudflare_grace_period_resolves_iuam(monkeypatch) -> None:
    """IUAM challenge that auto-resolves during the grace period should return
    before any Turnstile click is ever attempted."""
    from anansi import security
    monkeypatch.setattr(security, "DISABLE_ANTIBOT", False)

    fetcher = BrowserFetcher(cf_wait_timeout=60.0)
    # Speed up: 1 grace iteration (1 s sleep) instead of 5
    fetcher._CF_GRACE_ITERS = 1

    content_calls = 0

    class _FakeContext:
        async def cookies(self):
            return []  # never issues cf_clearance

    class _FakePage:
        frames = []
        context = _FakeContext()

        async def content(self) -> str:
            nonlocal content_calls
            content_calls += 1
            # Challenge present on first call (initial_content), gone afterwards
            if content_calls == 1:
                return "Just a moment cf-turnstile __cf_chl"
            return "<html><body>Welcome</body></html>"

        class mouse:
            @staticmethod
            async def move(x, y) -> None:
                pass

        @staticmethod
        async def evaluate(script: str) -> None:
            pass

    t0 = time.monotonic()
    await fetcher._wait_for_cloudflare(_FakePage())
    elapsed = time.monotonic() - t0
    # Should resolve during grace period (1 s sleep + content check)
    assert elapsed < 5.0, f"IUAM grace-period resolution took {elapsed:.1f}s"
    # No click should have been attempted (frames=[])
    assert content_calls >= 2


# ── Stale-progress guard — early exit when challenge never moves ──────────────

async def test_wait_for_cloudflare_no_progress_early_exit(monkeypatch) -> None:
    """If page content never changes and no Turnstile widget is found, the
    stale-progress guard fires early instead of burning the full timeout."""
    from anansi import security
    monkeypatch.setattr(security, "DISABLE_ANTIBOT", False)

    fetcher = BrowserFetcher(cf_wait_timeout=60.0)
    # Speed up: skip grace period, stale guard fires after 2 s
    fetcher._CF_GRACE_ITERS = 0
    fetcher._CF_STALE_GUARD_S = 2.0

    class _FakeContext:
        async def cookies(self):
            return []

    class _FakePage:
        frames = []
        context = _FakeContext()

        async def content(self) -> str:
            return "Just a moment cf-turnstile __cf_chl"

        class mouse:
            @staticmethod
            async def move(x, y) -> None:
                pass

        @staticmethod
        async def evaluate(script: str) -> None:
            pass

    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match="not progressing"):
        await fetcher._wait_for_cloudflare(_FakePage())
    elapsed = time.monotonic() - t0
    # Guard fires after 2 s stale + ≤1 poll cycle overhead
    assert elapsed < 10.0, f"stale guard should fire quickly, took {elapsed:.1f}s"
    # Must not burn the full 60 s timeout
    assert elapsed < 60.0
