"""HTTPFetcher reuses one httpx client per proxy instead of one per request (#12)."""

from __future__ import annotations

import httpx
import respx

from anansi.fetchers.http import HTTPFetcher


async def test_proxy_client_pooled_and_reused() -> None:
    async with HTTPFetcher(impersonate=None) as f:
        with respx.mock:
            respx.get("https://example.com/").mock(
                return_value=httpx.Response(200, text="ok")
            )
            await f.fetch("https://example.com/", proxy="http://proxy:8080")
            first = f._proxy_clients.get("http://proxy:8080")
            await f.fetch("https://example.com/", proxy="http://proxy:8080")
            second = f._proxy_clients.get("http://proxy:8080")

        assert first is not None
        assert first is second  # same client reused across proxied requests
        assert len(f._proxy_clients) == 1


async def test_close_drains_pooled_proxy_clients() -> None:
    f = HTTPFetcher(impersonate=None)
    with respx.mock:
        respx.get("https://example.com/").mock(return_value=httpx.Response(200, text="ok"))
        await f.fetch("https://example.com/", proxy="http://proxy:9090")
    client = f._proxy_clients["http://proxy:9090"]
    await f.close()
    assert client.is_closed
    assert f._proxy_clients == {}
