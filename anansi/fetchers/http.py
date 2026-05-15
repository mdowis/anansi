"""Async HTTP fetcher backed by httpx with realistic headers and retry logic."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any
from urllib.parse import urljoin

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from anansi import security
from anansi.fetchers.base import BaseFetcher, FetchResult
from anansi.security import is_url_safe_for_public_fetch

logger = logging.getLogger(__name__)

# Maximum response body size accepted from a remote server. A hostile host could
# stream multi-GB bodies to exhaust process memory; cap defensively.
_DEFAULT_MAX_RESPONSE_BYTES = 50 * 1024 * 1024  # 50 MB
# Cap on redirect chain length. httpx default is 20; we tighten it.
_MAX_REDIRECTS = 5


class ResponseTooLargeError(Exception):
    """Raised when a response body exceeds the configured size cap."""


class TooManyRedirectsError(Exception):
    """Raised when a redirect chain exceeds ``_MAX_REDIRECTS``."""

_QUICK_SPA_MARKERS = ("__NEXT_DATA__", "__NUXT__", "__INITIAL_STATE__", "__PRELOADED_STATE__", "__REDUX_STATE__")


def _extract_spa(html: str) -> dict | None:
    if not any(m in html for m in _QUICK_SPA_MARKERS):
        return None
    try:
        from anansi.parser.structured import extract_spa_state
        from bs4 import BeautifulSoup
        return extract_spa_state(BeautifulSoup(html, "lxml")) or None
    except Exception:
        return None

_RETRYABLE_STATUSES = {429, 502, 503, 504}


class _RetryableStatus(Exception):
    """Raised to trigger tenacity retry on 429/5xx responses."""

# Browser-like accept headers per language (rotated)
_ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9",
    "en-US,en;q=0.8,es;q=0.6",
    "en-US,en;q=0.9,fr;q=0.8",
]

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]


def _build_headers(ua: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": random.choice(_ACCEPT_LANGUAGES),
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    if extra:
        headers.update(extra)
    return headers


class HTTPFetcher(BaseFetcher):
    """
    Lightweight async fetcher using httpx.

    Rotates user-agents, sends realistic browser headers, follows redirects,
    and retries on transient errors with exponential backoff.
    """

    def __init__(
        self,
        *,
        max_retries: int = 3,
        timeout: float = 30.0,
        follow_redirects: bool = True,
        rotate_user_agents: bool = True,
        http2: bool = True,
        cookies: dict[str, str] | None = None,
        impersonate: str | None = None,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self._max_retries = max_retries
        self._timeout = timeout
        self._follow_redirects = follow_redirects
        self._rotate_ua = rotate_user_agents
        self._http2 = http2
        self._ua = random.choice(_USER_AGENTS)
        self._client: httpx.AsyncClient | None = None
        self._base_cookies = cookies or {}
        self._session_cookies: dict[str, str] = {}
        self._impersonate = impersonate
        self._max_response_bytes = max_response_bytes

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                http2=self._http2,
                # Auto-redirect is disabled so each Location can be re-validated
                # by ``_follow_redirect_chain`` before the next hop is fetched.
                follow_redirects=False,
                timeout=httpx.Timeout(self._timeout),
                cookies={**self._base_cookies, **self._session_cookies},
            )
        return self._client

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        proxy: str | None = None,
        timeout: float | None = None,
        referer: str | None = None,
        **kwargs: Any,
    ) -> FetchResult:
        if self._rotate_ua:
            self._ua = random.choice(_USER_AGENTS)

        # Inject a Referer only when the caller supplies one and has not
        # already set it explicitly. Akamai (and similar) score a missing /
        # mismatched Referer heavily; callers (crawler link-graph parent,
        # single-shot origin) provide the value — the fetcher stays
        # mechanism-only and never fabricates one.
        extra = dict(headers) if headers else {}
        if referer and not any(k.lower() == "referer" for k in extra):
            extra["Referer"] = referer

        merged_headers = _build_headers(self._ua, extra)

        if self._impersonate:
            return await self._fetch_curl_cffi(
                url, method=method, headers=merged_headers,
                body=body, proxy=proxy, timeout=timeout or self._timeout,
            )
        return await self._fetch_httpx(
            url, method=method, headers=merged_headers,
            body=body, proxy=proxy, timeout=timeout or self._timeout,
        )

    def _resolve_redirect(
        self,
        *,
        status: int,
        resp_url: str,
        location: str | None,
        method: str,
        body: bytes | None,
        redirect_count: int,
    ) -> tuple[str, str, bytes | None] | None:
        """Decide the next hop for a redirect, re-validating it against the
        SSRF guard. Shared by the httpx and curl-cffi paths so neither can
        smuggle a public→private redirect (the curl-cffi path previously used
        ``allow_redirects=True`` and bypassed this check entirely).

        Returns ``(next_url, next_method, next_body)`` or ``None`` when the
        response is not a redirect to follow. Raises ``UnsafeURLError`` /
        ``TooManyRedirectsError`` exactly like the original httpx loop.
        """
        if not (self._follow_redirects and status in (301, 302, 303, 307, 308)):
            return None
        if not location:
            return None
        if redirect_count >= _MAX_REDIRECTS:
            raise TooManyRedirectsError(
                f"redirect chain exceeded {_MAX_REDIRECTS} hops"
            )
        next_url = urljoin(resp_url, location)
        # Raises UnsafeURLError (non-retryable) if the hop is unsafe.
        is_url_safe_for_public_fetch(
            next_url, allow_private=security.ALLOW_PRIVATE_NETWORKS
        )
        # 303 forces GET; 301/302 historically also coerce GET in practice.
        # 307/308 preserve method + body.
        if status in (301, 302, 303):
            return next_url, "GET", None
        return next_url, method, body

    async def _fetch_curl_cffi(
        self,
        url: str,
        *,
        method: str,
        headers: dict[str, str],
        body: bytes | None,
        proxy: str | None,
        timeout: float,
    ) -> FetchResult:
        """Fetch using curl-cffi to mimic a real browser TLS fingerprint."""
        # Operator kill-switch: when anti-bot evasion is disabled, do not
        # perform TLS-fingerprint impersonation. Warn and fall back to plain
        # httpx so existing callers keep working (per confirmed decision).
        if security.DISABLE_ANTIBOT:
            logger.warning(
                "ANANSI_DISABLE_ANTIBOT set — ignoring impersonate=%r and "
                "falling back to plain httpx",
                self._impersonate,
            )
            self._impersonate = None
            return await self._fetch_httpx(
                url, method=method, headers=headers,
                body=body, proxy=proxy, timeout=timeout,
            )
        try:
            from curl_cffi.requests import AsyncSession
        except ImportError:
            logger.warning(
                "curl-cffi is not installed; falling back to httpx "
                "(install anansi[tls] for TLS fingerprint mimicry)"
            )
            self._impersonate = None
            return await self._fetch_httpx(
                url, method=method, headers=headers,
                body=body, proxy=proxy, timeout=timeout,
            )

        proxies = {"https": proxy, "http": proxy} if proxy else None

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=16),
            retry=retry_if_exception_type(_RetryableStatus),
            reraise=True,
        ):
            with attempt:
                t0 = time.perf_counter()
                # Send the accumulated cookie jar. Without this every
                # impersonate request is "cold" — Akamai/DataDome behavioral
                # scoring hard-blocks requests with no _abck / bm_sz / session
                # state. Response cookies are harvested below so continuity
                # holds across calls on the same HTTPFetcher.
                request_cookies = {**self._base_cookies, **self._session_cookies}
                # allow_redirects=False so every Location is re-validated by
                # the shared SSRF-checked redirect loop (parity with the httpx
                # path; impersonate must not weaken the SSRF guard).
                async with AsyncSession(
                    impersonate=self._impersonate,
                    allow_redirects=False,
                ) as session:
                    cur_url, cur_method, cur_body = url, method, body
                    redirect_count = 0
                    while True:
                        resp = await session.request(
                            method=cur_method,
                            url=cur_url,
                            headers=headers,
                            data=cur_body,
                            cookies=request_cookies or None,
                            proxies=proxies,
                            timeout=timeout,
                        )
                        nxt = self._resolve_redirect(
                            status=resp.status_code,
                            resp_url=str(resp.url),
                            location=resp.headers.get("location"),
                            method=cur_method,
                            body=cur_body,
                            redirect_count=redirect_count,
                        )
                        if nxt is None:
                            break
                        cur_url, cur_method, cur_body = nxt
                        redirect_count += 1
                elapsed = time.perf_counter() - t0

                if resp.status_code in _RETRYABLE_STATUSES:
                    retry_after = int(resp.headers.get("Retry-After", 0))
                    if retry_after > 0:
                        await asyncio.sleep(retry_after)
                    raise _RetryableStatus(f"HTTP {resp.status_code}")

        # Persist session cookies for subsequent requests
        for name, value in resp.cookies.items():
            self._session_cookies[name] = value

        body_text = resp.text
        if len(body_text.encode("utf-8", errors="ignore")) > self._max_response_bytes:
            raise ResponseTooLargeError(
                f"response body exceeds cap {self._max_response_bytes}"
            )
        _spa = _extract_spa(body_text)
        return FetchResult(
            url=str(resp.url),
            status=resp.status_code,
            html=body_text,
            headers=dict(resp.headers),
            cookies={k: v for k, v in resp.cookies.items()},
            elapsed=elapsed,
            via_browser=False,
            spa_state=_spa or None,
        )

    async def _fetch_httpx(
        self,
        url: str,
        *,
        method: str,
        headers: dict[str, str],
        body: bytes | None,
        proxy: str | None,
        timeout: float,
    ) -> FetchResult:
        """Fetch using httpx (standard path)."""
        client = await self._get_client()

        # Rebuild client with proxy if needed (httpx doesn't support per-request proxy easily)
        if proxy:
            transport = httpx.AsyncHTTPTransport(proxy=proxy)
            fetch_client = httpx.AsyncClient(
                http2=self._http2,
                # Auto-redirect off — the loop below validates each hop.
                follow_redirects=False,
                timeout=httpx.Timeout(timeout),
                transport=transport,
            )
        else:
            fetch_client = client

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_retries),
                wait=wait_exponential(multiplier=1, min=1, max=16),
                retry=retry_if_exception_type(
                    (httpx.TransportError, httpx.TimeoutException, _RetryableStatus)
                ),
                reraise=True,
            ):
                with attempt:
                    t0 = time.perf_counter()
                    # Manual redirect loop: re-validate Location at each hop so a
                    # public host cannot redirect the fetcher into a private
                    # address (SSRF). Capped at _MAX_REDIRECTS hops.
                    current_url = url
                    current_method = method
                    current_body = body
                    redirect_count = 0
                    while True:
                        resp = await fetch_client.request(
                            method=current_method,
                            url=current_url,
                            headers=headers,
                            content=current_body,
                            timeout=timeout,
                        )
                        nxt = self._resolve_redirect(
                            status=resp.status_code,
                            resp_url=str(resp.url),
                            location=resp.headers.get("location"),
                            method=current_method,
                            body=current_body,
                            redirect_count=redirect_count,
                        )
                        if nxt is None:
                            break
                        current_url, current_method, current_body = nxt
                        redirect_count += 1
                        continue
                    elapsed = time.perf_counter() - t0

                    # Reject obviously-oversized responses before materializing the
                    # body in memory. The Content-Length check is best-effort; the
                    # body length is double-checked below for chunked responses.
                    cl = resp.headers.get("content-length")
                    if cl and cl.isdigit() and int(cl) > self._max_response_bytes:
                        raise ResponseTooLargeError(
                            f"response Content-Length {cl} exceeds cap "
                            f"{self._max_response_bytes}"
                        )

                    if resp.status_code in _RETRYABLE_STATUSES:
                        retry_after = int(resp.headers.get("Retry-After", 0))
                        if retry_after > 0:
                            await asyncio.sleep(retry_after)
                        raise _RetryableStatus(f"HTTP {resp.status_code}")

            # Persist session cookies for subsequent requests (skip proxy clients)
            for name, value in resp.cookies.items():
                self._session_cookies[name] = value
            if not proxy and self._client and not self._client.is_closed:
                for name, value in resp.cookies.items():
                    self._client.cookies.set(name, value)

            body_text = resp.text
            if len(body_text.encode("utf-8", errors="ignore")) > self._max_response_bytes:
                raise ResponseTooLargeError(
                    f"response body exceeds cap {self._max_response_bytes}"
                )
            _spa = _extract_spa(body_text)
            return FetchResult(
                url=str(resp.url),
                status=resp.status_code,
                html=body_text,
                headers=dict(resp.headers),
                cookies={k: v for k, v in resp.cookies.items()},
                elapsed=elapsed,
                via_browser=False,
                spa_state=_spa or None,
            )
        finally:
            if proxy and fetch_client is not client:
                await fetch_client.aclose()

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
