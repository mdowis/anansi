"""Tests for HTTPFetcher session cookie persistence across requests."""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest
import respx

from anansi import security
from anansi.fetchers.http import HTTPFetcher


async def test_set_cookie_persisted_to_next_request() -> None:
    """Cookie from Set-Cookie response header should be sent on the next request."""
    received_cookies: list[str] = []

    with respx.mock:
        # First request sets a session cookie
        respx.get("https://example.com/login").mock(
            return_value=httpx.Response(
                200,
                text="logged in",
                headers={"Set-Cookie": "session=abc123; Path=/"},
            )
        )

        # Second request — capture whatever cookie header is sent
        def capture(request: httpx.Request) -> httpx.Response:
            received_cookies.append(request.headers.get("cookie", ""))
            return httpx.Response(200, text="profile")

        respx.get("https://example.com/profile").mock(side_effect=capture)

        async with HTTPFetcher() as fetcher:
            await fetcher.fetch("https://example.com/login")
            await fetcher.fetch("https://example.com/profile")

    assert received_cookies, "Second request was never made"
    assert "session=abc123" in received_cookies[0], (
        f"Session cookie not sent on second request; got: {received_cookies[0]!r}"
    )


async def test_constructor_cookies_sent_on_first_request() -> None:
    """Cookies passed to HTTPFetcher() constructor must be sent from the first request."""
    received_cookies: list[str] = []

    with respx.mock:
        def capture(request: httpx.Request) -> httpx.Response:
            received_cookies.append(request.headers.get("cookie", ""))
            return httpx.Response(200, text="ok")

        respx.get("https://example.com/").mock(side_effect=capture)

        async with HTTPFetcher(cookies={"auth": "token123"}) as fetcher:
            await fetcher.fetch("https://example.com/")

    assert "auth=token123" in received_cookies[0]


async def test_proxy_request_does_not_corrupt_session_jar() -> None:
    """Cookies set via a proxy request should accumulate in session_cookies but not
    overwrite the persistent client's jar in a harmful way for non-proxy requests."""
    with respx.mock:
        respx.get("https://example.com/data").mock(
            return_value=httpx.Response(
                200,
                text="ok",
                headers={"Set-Cookie": "x=1; Path=/"},
            )
        )

        async with HTTPFetcher() as fetcher:
            # Proxy request — should not raise
            result = await fetcher.fetch("https://example.com/data", proxy="http://proxy:8080")

        assert result.status == 200
        # session_cookies should have accumulated the cookie
        assert fetcher._session_cookies.get("x") == "1"


# ── curl-cffi (impersonate) path: cookie + Referer continuity ─────────────────
#
# curl-cffi is not respx-interceptable (respx only patches httpx), and it is an
# optional dependency that may be absent. We inject a fake `curl_cffi.requests`
# module so `_fetch_curl_cffi`'s local import succeeds with our stand-in, and
# record exactly what kwargs reach AsyncSession.request().


class _FakeResp:
    def __init__(self, *, status=200, text="ok", headers=None, url="https://example.com/",
                 cookies=None):
        self.status_code = status
        self.text = text
        # Real curl_cffi/httpx responses expose raw bytes; the fetcher sizes the
        # body via len(resp.content).
        self.content = text.encode("utf-8")
        self.headers = headers or {}
        self.url = url
        self.cookies = cookies or {}


@contextmanager
def _fake_curl_cffi(responses):
    """Patch curl_cffi.requests.AsyncSession; `responses` is a list consumed
    per .request() call. Returns a list that collects each call's kwargs."""
    calls: list[dict] = []
    seq = iter(responses)

    class _FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, **kwargs):
            calls.append(kwargs)
            return next(seq)

    fake_requests = types.ModuleType("curl_cffi.requests")
    fake_requests.AsyncSession = _FakeSession
    fake_pkg = types.ModuleType("curl_cffi")
    fake_pkg.requests = fake_requests

    with patch.dict(sys.modules, {"curl_cffi": fake_pkg,
                                  "curl_cffi.requests": fake_requests}):
        yield calls


async def test_curl_cffi_sends_base_and_session_cookies() -> None:
    """The impersonate path must send base + accumulated session cookies;
    without this every request is 'cold' and Akamai/DataDome hard-block it."""
    responses = [
        _FakeResp(headers={"Set-Cookie": "bm_sz=zzz"}, cookies={"bm_sz": "zzz"}),
        _FakeResp(),
    ]
    with _fake_curl_cffi(responses) as calls:
        async with HTTPFetcher(cookies={"_abck": "x"}, impersonate="chrome124") as f:
            await f.fetch("https://example.com/")
            await f.fetch("https://example.com/next")

    assert len(calls) == 2
    # First request carries the operator-supplied base cookie.
    assert calls[0]["cookies"] == {"_abck": "x"}
    # Second request carries base + the harvested session cookie.
    assert calls[1]["cookies"] == {"_abck": "x", "bm_sz": "zzz"}


async def test_curl_cffi_sends_referer_when_provided() -> None:
    with _fake_curl_cffi([_FakeResp()]) as calls:
        async with HTTPFetcher(impersonate="chrome124") as f:
            await f.fetch("https://example.com/page", referer="https://example.com/")

    assert calls[0]["headers"].get("Referer") == "https://example.com/"


async def test_curl_cffi_caller_referer_not_overridden() -> None:
    with _fake_curl_cffi([_FakeResp()]) as calls:
        async with HTTPFetcher(impersonate="chrome124") as f:
            await f.fetch(
                "https://example.com/page",
                headers={"Referer": "https://caller.example/"},
                referer="https://ignored.example/",
            )

    assert calls[0]["headers"]["Referer"] == "https://caller.example/"


async def test_disable_antibot_falls_back_but_cookies_still_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With DISABLE_ANTIBOT the impersonate path warns + falls back to httpx;
    cookies must still be sent (regression guard on the fallback)."""
    monkeypatch.setattr(security, "DISABLE_ANTIBOT", True)
    received: list[str] = []

    with respx.mock:
        def capture(request: httpx.Request) -> httpx.Response:
            received.append(request.headers.get("cookie", ""))
            return httpx.Response(200, text="ok")

        respx.get("https://example.com/").mock(side_effect=capture)

        async with HTTPFetcher(cookies={"auth": "t"}, impersonate="chrome124") as f:
            await f.fetch("https://example.com/")

    assert "auth=t" in received[0]


async def test_curl_cffi_redirect_to_private_ip_rejected() -> None:
    """SSRF parity: the impersonate path must re-validate every redirect hop
    (it previously used allow_redirects=True and bypassed the guard)."""
    from anansi.security import UnsafeURLError

    redirect = _FakeResp(
        status=302,
        headers={"location": "http://127.0.0.1/"},
        url="https://example.com/",
    )
    with _fake_curl_cffi([redirect, _FakeResp()]) as calls:
        async with HTTPFetcher(impersonate="chrome124", max_retries=1) as f:
            with pytest.raises(UnsafeURLError):
                await f.fetch("https://example.com/")

    # The loopback hop must never have been requested.
    assert len(calls) == 1


async def test_multiple_set_cookies_all_accumulated() -> None:
    with respx.mock:
        respx.get("https://example.com/a").mock(
            return_value=httpx.Response(
                200, text="ok",
                headers={"Set-Cookie": "k1=v1; Path=/"},
            )
        )
        respx.get("https://example.com/b").mock(
            return_value=httpx.Response(
                200, text="ok",
                headers={"Set-Cookie": "k2=v2; Path=/"},
            )
        )

        async with HTTPFetcher() as fetcher:
            await fetcher.fetch("https://example.com/a")
            await fetcher.fetch("https://example.com/b")

        assert fetcher._session_cookies["k1"] == "v1"
        assert fetcher._session_cookies["k2"] == "v2"
