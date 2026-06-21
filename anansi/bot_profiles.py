"""Crawler bot-profile registry — present as a known search-engine crawler.

Anansi's default HTTP/browser fetchers rotate a *browser* User-Agent and send
browser-y headers (``Sec-Fetch-*``, ``DNT``, ``Upgrade-Insecure-Requests``).
A bot profile instead pins the User-Agent to a real crawler string (e.g.
Googlebot), sends that crawler's accurate (minimal) header set, and tells the
robots.txt layer which agent token to evaluate rules against.

Many sites serve their full, ungated content to search engines for SEO while
gating browsers behind consent walls or JS shells; presenting as Googlebot is a
well-known way to reach that content.

Caveat (intentionally not enforced here): this only spoofs the User-Agent. Sites
that verify crawlers by reverse DNS / source-IP ranges will still see a
non-Google address. Spoofing the UA does not place you on Google's network.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class UnknownBotProfileError(ValueError):
    """Raised when a bot-profile name is not in the registry."""


@dataclass(frozen=True)
class BotProfile:
    """A named crawler identity: pinned UA, header set, and robots.txt token."""

    name: str
    user_agent: str
    # Agent token used for robots.txt evaluation (RobotFileParser.can_fetch).
    robots_user_agent: str
    # Full header set sent with each request. Replaces the default browser
    # header block so browser-only headers (Sec-Fetch-*, DNT, ...) are not
    # leaked alongside a crawler UA. The pinned User-Agent is added by the
    # fetcher; do not duplicate it here.
    headers: dict[str, str] = field(default_factory=dict)


# Googlebot sends a lean header set: an Accept that prefers HTML/images, an
# Accept-Encoding, and a From mailbox. It notably omits Accept-Language and the
# Sec-Fetch-* / DNT / Upgrade-Insecure-Requests headers a browser would send.
_GOOGLEBOT_HEADERS: dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "From": "googlebot(at)googlebot.com",
}


BOT_PROFILES: dict[str, BotProfile] = {
    "googlebot": BotProfile(
        name="googlebot",
        user_agent=(
            "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
            "Googlebot/2.1; +http://www.google.com/bot.html) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        robots_user_agent="Googlebot",
        headers=dict(_GOOGLEBOT_HEADERS),
    ),
    "googlebot-mobile": BotProfile(
        name="googlebot-mobile",
        user_agent=(
            "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 "
            "Mobile Safari/537.36 (compatible; Googlebot/2.1; "
            "+http://www.google.com/bot.html)"
        ),
        robots_user_agent="Googlebot",
        headers=dict(_GOOGLEBOT_HEADERS),
    ),
}


def available_profiles() -> list[str]:
    """Return the sorted list of registered bot-profile names."""
    return sorted(BOT_PROFILES)


def get_profile(profile: str | BotProfile | None) -> BotProfile | None:
    """Resolve *profile* to a :class:`BotProfile`.

    ``None`` passes through (no profile). A :class:`BotProfile` is returned
    as-is. A string is looked up in the registry, raising
    :class:`UnknownBotProfileError` on an unknown name.
    """
    if profile is None or isinstance(profile, BotProfile):
        return profile
    try:
        return BOT_PROFILES[profile]
    except KeyError:
        raise UnknownBotProfileError(
            f"unknown bot profile {profile!r}; available: {available_profiles()}"
        ) from None
