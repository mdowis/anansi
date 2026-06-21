"""Tests for the bot-profile (Googlebot impersonation) feature."""

from __future__ import annotations

import httpx
import pytest
import respx

from anansi.bot_profiles import (
    BOT_PROFILES,
    UnknownBotProfileError,
    available_profiles,
    get_profile,
)
from anansi.fetchers.http import HTTPFetcher, _build_headers
from anansi.robots import RobotsCache

# Browser-only headers that a real crawler does NOT send. The Googlebot profile
# must not leak any of these.
_BROWSER_ONLY_HEADERS = {
    "Sec-Fetch-Dest",
    "Sec-Fetch-Mode",
    "Sec-Fetch-Site",
    "Sec-Fetch-User",
    "DNT",
    "Upgrade-Insecure-Requests",
    "Accept-Language",
}


# ── Registry ──────────────────────────────────────────────────────────────────

def test_registry_seeded_with_googlebot() -> None:
    assert "googlebot" in BOT_PROFILES
    assert "googlebot-mobile" in BOT_PROFILES
    assert "googlebot" in available_profiles()


def test_get_profile_passthrough_and_lookup() -> None:
    assert get_profile(None) is None
    prof = get_profile("googlebot")
    assert prof is not None and prof.name == "googlebot"
    # A BotProfile instance is returned as-is.
    assert get_profile(prof) is prof


def test_get_profile_unknown_raises() -> None:
    with pytest.raises(UnknownBotProfileError):
        get_profile("not-a-real-bot")


def test_googlebot_profile_shape() -> None:
    prof = get_profile("googlebot")
    assert "Googlebot/2.1" in prof.user_agent
    assert prof.robots_user_agent == "Googlebot"
    assert "From" in prof.headers


# ── _build_headers ────────────────────────────────────────────────────────────

def test_build_headers_profile_omits_browser_headers() -> None:
    prof = get_profile("googlebot")
    headers = _build_headers("ignored-ua", None, prof)
    assert headers["User-Agent"] == prof.user_agent
    assert "Googlebot" in headers["User-Agent"]
    for h in _BROWSER_ONLY_HEADERS:
        assert h not in headers, f"profile leaked browser header {h}"
    assert headers["From"] == "googlebot(at)googlebot.com"


def test_build_headers_extra_overrides_profile() -> None:
    prof = get_profile("googlebot")
    headers = _build_headers("x", {"From": "custom@example.com"}, prof)
    assert headers["From"] == "custom@example.com"


def test_build_headers_default_path_unchanged() -> None:
    headers = _build_headers("my-ua", None, None)
    assert headers["User-Agent"] == "my-ua"
    assert "Sec-Fetch-Mode" in headers


# ── HTTPFetcher integration ───────────────────────────────────────────────────

async def test_fetcher_sends_googlebot_ua() -> None:
    captured: dict[str, str] = {}

    with respx.mock:
        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(request.headers)
            return httpx.Response(200, text="<html>ok</html>")

        respx.get("https://example.com/").mock(side_effect=handler)
        async with HTTPFetcher(bot_profile="googlebot") as f:
            await f.fetch("https://example.com/")

    assert "Googlebot/2.1" in captured["user-agent"]
    assert captured["from"] == "googlebot(at)googlebot.com"
    for h in _BROWSER_ONLY_HEADERS:
        assert h.lower() not in captured, f"leaked browser header {h}"


async def test_profile_pins_ua_no_rotation() -> None:
    """A pinned bot UA must be identical across repeated fetches."""
    seen: list[str] = []

    with respx.mock:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["user-agent"])
            return httpx.Response(200, text="ok")

        respx.get("https://example.com/").mock(side_effect=handler)
        async with HTTPFetcher(bot_profile="googlebot") as f:
            for _ in range(4):
                await f.fetch("https://example.com/")

    assert len(set(seen)) == 1
    assert "Googlebot" in seen[0]


def test_fetcher_rejects_unknown_profile() -> None:
    with pytest.raises(UnknownBotProfileError):
        HTTPFetcher(bot_profile="bogus-bot")


# ── robots.txt evaluated as Googlebot ─────────────────────────────────────────

_ROBOTS_GOOGLEBOT_RULES = """\
User-agent: *
Disallow: /

User-agent: Googlebot
Allow: /
Crawl-delay: 2
"""


@pytest.fixture
def robots_mock():
    with respx.mock:
        yield


async def test_robots_allows_googlebot_where_wildcard_denied(robots_mock) -> None:
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text=_ROBOTS_GOOGLEBOT_RULES)
    )
    as_googlebot = RobotsCache(user_agent="Googlebot")
    as_wildcard = RobotsCache(user_agent="*")
    assert await as_googlebot.allowed("https://example.com/page") is True
    assert await as_wildcard.allowed("https://example.com/page") is False


async def test_robots_crawl_delay_uses_googlebot_token(robots_mock) -> None:
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text=_ROBOTS_GOOGLEBOT_RULES)
    )
    cache = RobotsCache(user_agent="Googlebot")
    assert await cache.crawl_delay("https://example.com/page") == 2.0
