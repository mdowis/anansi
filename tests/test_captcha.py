"""Tests for the CAPTCHA abstraction and its browser wiring."""

from __future__ import annotations

import pytest

from anansi.captcha import (
    CaptchaChallenge,
    CaptchaResult,
    CaptchaSolver,
    CaptchaVendor,
    NullCaptchaSolver,
    detect_captcha,
)
from anansi.fetchers.browser import BrowserFetcher


# ── Detection (Task 13) ───────────────────────────────────────────────────────

def test_detect_recaptcha() -> None:
    html = '<div class="g-recaptcha" data-sitekey="ABC123"></div>'
    c = detect_captcha(html, "https://example.com/")
    assert c is not None
    assert c.vendor is CaptchaVendor.RECAPTCHA
    assert c.site_key == "ABC123"
    assert c.url == "https://example.com/"


def test_detect_hcaptcha() -> None:
    c = detect_captcha('<div class="h-captcha" data-sitekey="k"></div>')
    assert c is not None and c.vendor is CaptchaVendor.HCAPTCHA
    assert c.site_key == "k"


def test_detect_turnstile() -> None:
    c = detect_captcha('<div class="cf-turnstile" data-sitekey="t"></div>')
    assert c is not None and c.vendor is CaptchaVendor.TURNSTILE


def test_detect_datadome() -> None:
    c = detect_captcha("<html>captcha-delivery.com</html>")
    assert c is not None and c.vendor is CaptchaVendor.DATADOME


def test_detect_none_on_plain_page() -> None:
    assert detect_captcha("<html><body>hello world</body></html>") is None
    assert detect_captcha("") is None


# ── NullCaptchaSolver (Task 13) ───────────────────────────────────────────────

async def test_null_solver_returns_manual_required() -> None:
    solver = NullCaptchaSolver()
    assert isinstance(solver, CaptchaSolver)  # satisfies the protocol
    result = await solver.solve(
        CaptchaChallenge(vendor=CaptchaVendor.RECAPTCHA, url="https://x/")
    )
    assert result.solved is False
    assert result.manual_required is True
    assert result.token is None


# ── Browser wiring (Task 14) ──────────────────────────────────────────────────

class _FakePage:
    url = "https://example.com/"

    def __init__(self, content: str) -> None:
        self._content = content

    async def content(self) -> str:
        return self._content


async def test_no_solver_is_noop() -> None:
    fetcher = BrowserFetcher()  # no solver configured
    html = '<div class="g-recaptcha" data-sitekey="x"></div>'
    out = await fetcher._maybe_solve_captcha(_FakePage(html), html)
    assert out == html


async def test_solver_invoked_when_captcha_detected() -> None:
    calls: list[CaptchaChallenge] = []

    class _Solver:
        async def solve(self, challenge: CaptchaChallenge) -> CaptchaResult:
            calls.append(challenge)
            return CaptchaResult(solved=False, manual_required=True)

    fetcher = BrowserFetcher(captcha_solver=_Solver())
    html = '<div class="h-captcha" data-sitekey="k"></div>'
    out = await fetcher._maybe_solve_captcha(_FakePage(html), html)

    assert len(calls) == 1
    assert calls[0].vendor is CaptchaVendor.HCAPTCHA
    # Unsolved → page returned as-is, gracefully (no loop, no raise).
    assert out == html
    assert fetcher._last_captcha_result.manual_required is True


async def test_solver_not_invoked_without_captcha() -> None:
    class _Solver:
        async def solve(self, challenge):  # pragma: no cover
            raise AssertionError("solver must not run on a clean page")

    fetcher = BrowserFetcher(captcha_solver=_Solver())
    html = "<html><body>normal content</body></html>"
    out = await fetcher._maybe_solve_captcha(_FakePage(html), html)
    assert out == html


async def test_solver_solved_rereads_page() -> None:
    class _Solver:
        async def solve(self, challenge):
            return CaptchaResult(solved=True, token="tok")

    class _PageThatClears:
        url = "https://example.com/"
        def __init__(self):
            self.reads = 0
        async def content(self):
            self.reads += 1
            return "<html><body>unlocked</body></html>"

    fetcher = BrowserFetcher(captcha_solver=_Solver())
    page = _PageThatClears()
    html = '<div class="cf-turnstile" data-sitekey="t"></div>'
    out = await fetcher._maybe_solve_captcha(page, html)
    assert "unlocked" in out
    assert page.reads == 1  # re-read after solve


async def test_solver_exception_does_not_crash_fetch() -> None:
    class _Solver:
        async def solve(self, challenge):
            raise RuntimeError("provider down")

    fetcher = BrowserFetcher(captcha_solver=_Solver())
    html = '<div class="g-recaptcha" data-sitekey="x"></div>'
    out = await fetcher._maybe_solve_captcha(_FakePage(html), html)
    assert out == html  # graceful fallback


async def test_solver_skipped_under_disable_antibot(monkeypatch) -> None:
    from anansi import security
    monkeypatch.setattr(security, "DISABLE_ANTIBOT", True)

    class _Solver:
        async def solve(self, challenge):  # pragma: no cover
            raise AssertionError("solver must not run under DISABLE_ANTIBOT")

    fetcher = BrowserFetcher(captcha_solver=_Solver())
    html = '<div class="g-recaptcha" data-sitekey="x"></div>'
    out = await fetcher._maybe_solve_captcha(_FakePage(html), html)
    assert out == html
