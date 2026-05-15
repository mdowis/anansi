"""Phase 4: Akamai block detection + graduated escalation ladder."""

from __future__ import annotations

import httpx
import pytest
import respx

from anansi.fetchers.base import FetchResult
from anansi.fetchers.escalate import escalate_akamai
from anansi.fetchers.smart import detect_akamai_block, needs_browser


# ── detect_akamai_block (pure) ────────────────────────────────────────────────

def test_detect_akamai_reference_body() -> None:
    html = (
        "<html><body>Access Denied<br>Reference #18.abcd1234.efgh5678.9ijkl"
        "<br>https://errors.edgesuite.net/18.abcd</body></html>"
    )
    assert detect_akamai_block(html, 403, {}) is True


def test_detect_akamai_server_header() -> None:
    assert detect_akamai_block("<html>ok</html>", 200, {"Server": "AkamaiGHost"}) is True
    # Header key case-insensitive.
    assert detect_akamai_block("x", 403, {"server": "AkamaiGHost/9.0"}) is True


def test_detect_akamai_non_block_is_false() -> None:
    # A normal 403 login page must NOT be flagged (would waste the ladder).
    assert detect_akamai_block("<html>Please sign in</html>", 403, {}) is False
    # 200 with normal server.
    assert detect_akamai_block("<html>content</html>", 200, {"Server": "nginx"}) is False
    # Markers only count on 403/429.
    assert detect_akamai_block("Reference #1.2.3.4", 200, {}) is False


def test_detect_akamai_does_not_regress_needs_browser() -> None:
    """needs_browser still treats a thin 403 as 'not a JS shell'."""
    assert needs_browser("<html><body>403 access denied</body></html>") is False


# ── escalate_akamai ladder ────────────────────────────────────────────────────

def _r(status: int, html: str = "ok", headers=None, via_browser: bool = False) -> FetchResult:
    return FetchResult(url="https://example.com/", status=status, html=html,
                        headers=headers or {}, via_browser=via_browser)


async def test_ladder_noop_when_not_blocked() -> None:
    good = _r(200, "<html>fine</html>")

    async def _never():  # pragma: no cover - must not be called
        raise AssertionError("escalation should not run")

    out = await escalate_akamai(
        url="https://example.com/", initial=good,
        retry_impersonated=_never, browser_fetch=_never,
        disable_antibot=False,
    )
    assert out is good


async def test_ladder_impersonate_resolves_block() -> None:
    blocked = _r(403, "Reference #1.2.3.4 errors.edgesuite.net")

    async def _imp():
        return _r(200, "<html>unlocked</html>")

    async def _browser():  # pragma: no cover - should not reach browser
        raise AssertionError("browser rung should not run")

    out = await escalate_akamai(
        url="https://example.com/", initial=blocked,
        retry_impersonated=_imp, browser_fetch=_browser,
        disable_antibot=False,
    )
    assert out.status == 200


async def test_ladder_escalates_to_browser() -> None:
    blocked = _r(403, "AkamaiGHost Reference #9")

    async def _imp():
        return _r(403, "Reference #9 still blocked")  # still blocked

    async def _browser():
        return _r(200, "<html>browser solved</html>", via_browser=True)

    out = await escalate_akamai(
        url="https://example.com/", initial=blocked,
        retry_impersonated=_imp, browser_fetch=_browser,
        disable_antibot=False,
    )
    assert out.status == 200 and out.via_browser


async def test_ladder_disabled_returns_block_unchanged() -> None:
    blocked = _r(403, "Reference #1 errors.edgesuite.net")

    async def _never():  # pragma: no cover
        raise AssertionError("must not escalate when anti-bot disabled")

    out = await escalate_akamai(
        url="https://example.com/", initial=blocked,
        retry_impersonated=_never, browser_fetch=_never,
        disable_antibot=True,
    )
    assert out is blocked  # detected, but returned as-is for honest reporting


# ── single-shot integration: _fetch_one escalates a simulated Akamai 403 ──────

async def test_fetch_one_escalates_akamai_403_to_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Akamai 403 from the HTTP path escalates through the ladder. The
    impersonated retry (curl-cffi absent → httpx fallback, still cold → 403)
    fails, so it reaches the browser rung (mocked to succeed)."""
    import anansi.mcp_server.server as srv
    srv._page_cache.clear()
    from anansi import security
    from unittest.mock import AsyncMock, MagicMock, patch

    monkeypatch.setattr(security, "DISABLE_ANTIBOT", False)

    browser_result = FetchResult(
        url="https://example.com/", status=200,
        html="<html>browser solved</html>", via_browser=True,
    )

    with respx.mock:
        respx.get("https://example.com/").mock(
            return_value=httpx.Response(
                403,
                text="Access Denied Reference #18.aa.bb.cc errors.edgesuite.net",
                headers={"Server": "AkamaiGHost"},
            )
        )

        bf_instance = MagicMock()
        bf_instance.fetch = AsyncMock(return_value=browser_result)
        bf_instance.close = AsyncMock()
        bf_cm = MagicMock()
        bf_cm.__aenter__ = AsyncMock(return_value=bf_instance)
        bf_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("anansi.mcp_server.server._validate_url"), \
                patch("anansi.fetchers.browser.BrowserFetcher",
                      return_value=bf_cm):
            res = await srv._fetch_one("https://example.com/")

    assert res["status"] == 200
    assert res["via_browser"] is True


async def test_fetch_one_akamai_403_not_escalated_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under DISABLE_ANTIBOT the block is detected but returned as an honest
    403 — no browser is ever constructed."""
    import anansi.mcp_server.server as srv
    srv._page_cache.clear()
    from anansi import security
    from unittest.mock import patch

    monkeypatch.setattr(security, "DISABLE_ANTIBOT", True)

    with respx.mock:
        respx.get("https://example.com/").mock(
            return_value=httpx.Response(
                403, text="Reference #18.aa errors.edgesuite.net",
                headers={"Server": "AkamaiGHost"},
            )
        )
        with patch("anansi.mcp_server.server._validate_url"), \
                patch("anansi.fetchers.browser.BrowserFetcher") as MockBF:
            res = await srv._fetch_one("https://example.com/")
            MockBF.assert_not_called()

    assert res["status"] == 403
