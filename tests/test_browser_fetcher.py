"""Tests for BrowserFetcher pool bounding and close() correctness."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from anansi.fetchers.browser import BrowserFetcher
from anansi.persona import build_persona


class _MockContext:
    """Minimal browser context stub that tracks close() calls."""

    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


async def test_context_pool_is_bounded() -> None:
    """Pool maxsize should equal max_contexts; QueueFull is raised (not silently dropped)."""
    fetcher = BrowserFetcher(max_contexts=2)
    # Initialize the pool manually without starting Playwright
    fetcher._context_pool = asyncio.Queue(maxsize=fetcher._max_contexts)

    ctx_a = _MockContext()
    ctx_b = _MockContext()
    ctx_c = _MockContext()

    t_a = (ctx_a, 0.0, 0)
    t_b = (ctx_b, 0.0, 0)
    t_c = (ctx_c, 0.0, 0)

    fetcher._context_pool.put_nowait(t_a)
    fetcher._context_pool.put_nowait(t_b)

    with pytest.raises(asyncio.QueueFull):
        fetcher._context_pool.put_nowait(t_c)  # pool is full


async def test_close_drains_pool_without_error() -> None:
    """close() must unpack (ctx, created_at) tuples and call ctx.close(), not tuple.close()."""
    fetcher = BrowserFetcher(max_contexts=3)
    fetcher._context_pool = asyncio.Queue(maxsize=3)
    fetcher._browser = None
    fetcher._playwright = None

    ctx_a = _MockContext()
    ctx_b = _MockContext()
    fetcher._context_pool.put_nowait((ctx_a, 1000.0, 5))
    fetcher._context_pool.put_nowait((ctx_b, 1001.0, 0))

    # Should not raise AttributeError ("tuple has no attribute close")
    await fetcher.close()

    assert ctx_a.closed
    assert ctx_b.closed
    assert fetcher._context_pool.empty()


async def test_close_with_empty_pool_does_not_raise() -> None:
    fetcher = BrowserFetcher()
    fetcher._context_pool = asyncio.Queue(maxsize=5)
    fetcher._browser = None
    fetcher._playwright = None
    await fetcher.close()  # must not raise


# ── Persona-bound stealth fingerprint (Task 5) ────────────────────────────────

def test_stealth_js_renders_persona_screen_and_languages() -> None:
    persona = build_persona(seed=3)
    fetcher = BrowserFetcher(persona=persona)
    js = fetcher._make_stealth_js(persona)

    assert str(persona.screen["width"]) in js
    assert str(persona.screen["height"]) in js
    assert persona.webgl_vendor in js
    assert persona.webgl_renderer in js
    assert str(persona.hardware_concurrency) in js
    assert str(persona.max_touch_points) in js
    # navigator.languages derived from Accept-Language, q-values dropped.
    primary_lang = persona.accept_language.split(",")[0]
    assert f"'{primary_lang}'" in js
    # No leftover placeholder tokens.
    assert "__ANANSI_" not in js


def test_stealth_js_differs_between_personas() -> None:
    desktop = build_persona(seed=8, mobile=False)
    mobile = build_persona(seed=7, mobile=True)
    fetcher = BrowserFetcher()
    assert fetcher._make_stealth_js(desktop) != fetcher._make_stealth_js(mobile)


# ── Persona-consistent Playwright context (Task 6) ────────────────────────────

class _CapturingContext:
    async def add_init_script(self, script: str) -> None:
        pass

    async def clear_cookies(self) -> None:
        pass

    async def clear_permissions(self) -> None:
        pass

    async def close(self) -> None:
        pass


async def test_context_options_come_from_persona() -> None:
    persona = build_persona(seed=3)
    fetcher = BrowserFetcher(persona=persona)
    captured: dict[str, Any] = {}

    class _Browser:
        async def new_context(self, **kwargs):
            captured.update(kwargs)
            return _CapturingContext()

    fetcher._browser = _Browser()
    fetcher._context_semaphore = asyncio.Semaphore(2)
    fetcher._context_pool = asyncio.Queue(maxsize=2)

    async with fetcher._get_context() as ctx:
        pass

    assert captured["user_agent"] == persona.user_agent
    assert captured["locale"] == persona.locale
    assert captured["timezone_id"] == persona.timezone_id
    assert captured["viewport"] == dict(persona.viewport)
    assert captured["extra_http_headers"]["Accept-Language"] == persona.accept_language
    # Geolocation must NOT be unconditionally granted.
    assert "permissions" not in captured or not captured["permissions"]


async def test_pinned_persona_deterministic() -> None:
    a = BrowserFetcher(persona_seed=5)
    b = BrowserFetcher(persona_seed=5)
    assert a._persona == b._persona


# ── Sticky browser sessions (Task 7) ──────────────────────────────────────────

class _StatefulContext:
    def __init__(self) -> None:
        self.cleared = 0
        self.closed = False

    async def add_init_script(self, script: str) -> None:
        pass

    async def clear_cookies(self) -> None:
        self.cleared += 1

    async def clear_permissions(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class _StatefulBrowser:
    def __init__(self) -> None:
        self.created: list[_StatefulContext] = []

    async def new_context(self, **kwargs):
        ctx = _StatefulContext()
        self.created.append(ctx)
        return ctx


def _prime(fetcher: BrowserFetcher, browser: _StatefulBrowser) -> None:
    fetcher._browser = browser
    fetcher._context_semaphore = asyncio.Semaphore(fetcher._max_contexts)
    fetcher._context_pool = asyncio.Queue(maxsize=fetcher._max_contexts)


async def test_same_session_key_reuses_context_without_clearing() -> None:
    fetcher = BrowserFetcher(persona_seed=1)
    browser = _StatefulBrowser()
    _prime(fetcher, browser)

    async with fetcher._get_context(session_key="site-a") as c1:
        pass
    # Sticky sessions preserve earned state — never cleared.
    assert c1.cleared == 0
    assert len(browser.created) == 1

    async with fetcher._get_context(session_key="site-a") as c2:
        pass
    assert c2 is c1, "same session key must reuse the same context"
    assert len(browser.created) == 1


async def test_different_session_key_does_not_share_context() -> None:
    fetcher = BrowserFetcher(persona_seed=1)
    browser = _StatefulBrowser()
    _prime(fetcher, browser)

    async with fetcher._get_context(session_key="site-a") as c1:
        pass
    async with fetcher._get_context(session_key="site-b") as c2:
        pass
    assert c2 is not c1
    assert len(browser.created) == 2


async def test_anonymous_context_is_cleared_on_checkout() -> None:
    fetcher = BrowserFetcher(persona_seed=1)
    browser = _StatefulBrowser()
    _prime(fetcher, browser)

    async with fetcher._get_context() as c:
        pass
    # No session key → clear cookies to avoid cross-origin leakage.
    assert c.cleared == 1


async def test_force_fresh_session_context_is_closed_not_pooled() -> None:
    fetcher = BrowserFetcher(persona_seed=1)
    browser = _StatefulBrowser()
    _prime(fetcher, browser)

    async with fetcher._get_context(session_key="site-a", force_fresh=True) as c:
        pass
    assert c.closed is True
    # The keyed pool must not hold the force_fresh context.
    pool = fetcher._session_pools.get("site-a")
    assert pool is None or pool.empty()


async def test_poisoned_context_not_returned_to_pool() -> None:
    fetcher = BrowserFetcher(persona_seed=1)
    browser = _StatefulBrowser()
    _prime(fetcher, browser)

    with pytest.raises(RuntimeError):
        async with fetcher._get_context(session_key="site-a") as c:
            raise RuntimeError("hard failure mid-fetch")

    assert c.closed is True
    pool = fetcher._session_pools.get("site-a")
    assert pool is None or pool.empty()


def test_make_session_key_varies_by_component() -> None:
    from anansi.fetchers.browser import make_session_key

    p = build_persona(seed=1, mobile=False)
    base = make_session_key("example.com", None, p)
    assert base != make_session_key("other.com", None, p)
    assert base != make_session_key("example.com", "http://proxy:8080", p)
    # Desktop vs mobile persona → different identity → different key.
    assert base != make_session_key("example.com", None, build_persona(seed=1, mobile=True))
    # Deterministic for the same inputs.
    assert base == make_session_key("example.com", None, p)
