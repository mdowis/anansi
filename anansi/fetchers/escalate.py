"""Shared, graduated escalation ladder for Akamai (edge bot-manager) blocks.

Used by both the single-shot MCP path and the crawler so the logic lives in
one place. The ladder is deliberately short and bounded — it performs at most
one impersonated retry and one browser attempt; broader retry/backoff and the
circuit breaker are owned by the callers. Triggered only by a positive,
conservative ``detect_akamai_block`` classification (server-response driven,
never client input). When the operator set ``ANANSI_DISABLE_ANTIBOT`` the
block is still *detected* (for honest reporting) but never escalated.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from anansi.fetchers.base import FetchResult
from anansi.fetchers.smart import detect_akamai_block
from anansi.protection import (
    ProtectionDetection,
    ProtectionKind,
    ProtectionVendor,
    detect_protection,
)

logger = logging.getLogger(__name__)

# Used when neither the caller nor the operator specified an impersonation
# target but an Akamai block forces one. Must be in security.IMPERSONATE_ALLOWLIST.
DEFAULT_IMPERSONATE = "chrome124"


def _blocked(result: FetchResult) -> bool:
    return detect_akamai_block(result.html, result.status, result.headers)


async def escalate_akamai(
    *,
    url: str,
    initial: FetchResult,
    retry_impersonated: Callable[[], Awaitable[FetchResult]],
    browser_fetch: Callable[[], Awaitable[FetchResult]] | None,
    disable_antibot: bool,
) -> FetchResult:
    """Return the best result for *url* given an *initial* fetch.

    Ladder: detect → (1) impersonated retry → (2) headless browser.
    Returns *initial* unchanged when it is not an Akamai block, or when
    escalation is disabled by the operator kill-switch.
    """
    if not _blocked(initial):
        return initial

    if disable_antibot:
        logger.info(
            "Akamai block detected at %s; escalation disabled "
            "(ANANSI_DISABLE_ANTIBOT) — returning %s as-is",
            url, initial.status,
        )
        return initial

    # Rung 1 — impersonated retry (caller supplies a freshly-warmed,
    # TLS/HTTP-2-impersonating fetch).
    logger.info("Akamai block at %s — retrying with TLS/HTTP-2 impersonation", url)
    try:
        r1 = await retry_impersonated()
        if not _blocked(r1):
            return r1
    except Exception as exc:  # noqa: BLE001 - fall through to next rung
        logger.debug("Impersonated retry for %s failed: %s", url, exc)
        r1 = initial

    # Rung 2 — real headless browser (can execute the Akamai sensor JS).
    if browser_fetch is not None:
        logger.info("Still blocked at %s — escalating to headless browser", url)
        try:
            return await browser_fetch()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Browser escalation for %s failed: %s", url, exc)

    return r1


async def escalate_protection(
    *,
    url: str,
    initial: FetchResult,
    retry_impersonated: Callable[[], Awaitable[FetchResult]],
    browser_fetch: Callable[[], Awaitable[FetchResult]] | None,
    disable_antibot: bool,
    detection: ProtectionDetection | None = None,
) -> FetchResult:
    """Vendor-aware escalation ladder.

    Classifies *initial* (or uses the supplied *detection*) and follows the
    playbook for the detected vendor:

    - **Cloudflare challenge** → straight to the browser (only a real browser
      can execute the CF interstitial JS). Do NOT gate on ``result.ok`` — CF
      challenges arrive as 403/503/429, which look like failures.
    - **Cloudflare hard block** → return as-is; a browser on the same IP hits
      the same WAF rule. The crawler's proxy layer reacts to the status.
    - **Akamai** → impersonated retry, then browser (see ``escalate_akamai``).
    - **DataDome** → prefer browser (with a sticky/residential proxy upstream);
      if unsolved, return the detected state honestly.
    - **generic CAPTCHA / JS shell** → browser attempt.

    Returns *initial* unchanged when nothing actionable is detected or when the
    operator kill-switch (``disable_antibot``) is set.
    """
    det = detection or detect_protection(
        initial.html, initial.status, initial.headers, initial.cookies, url
    )

    if not det.is_protected:
        return initial

    if disable_antibot:
        logger.info(
            "protection detected at %s (%s/%s); escalation disabled "
            "(ANANSI_DISABLE_ANTIBOT) — returning %s as-is",
            url, det.vendor.value, det.kind.value, initial.status,
        )
        return initial

    async def _try_browser() -> FetchResult:
        if browser_fetch is None:
            return initial
        try:
            return await browser_fetch()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Browser escalation for %s failed: %s", url, exc)
            return initial

    if det.vendor is ProtectionVendor.CLOUDFLARE:
        if det.is_hard_block:
            logger.warning(
                "Cloudflare hard block at %s — returning as-is; a cleaner IP "
                "or residential proxy is required (not browser-solvable)", url,
            )
            return initial
        logger.info("Cloudflare challenge at %s — escalating to headless browser", url)
        return await _try_browser()

    if det.vendor is ProtectionVendor.AKAMAI:
        return await escalate_akamai(
            url=url, initial=initial,
            retry_impersonated=retry_impersonated,
            browser_fetch=browser_fetch,
            disable_antibot=disable_antibot,
        )

    if det.vendor is ProtectionVendor.DATADOME:
        logger.info(
            "DataDome %s at %s — escalating to browser (prefer a sticky "
            "residential proxy upstream)", det.kind.value, url,
        )
        return await _try_browser()

    # Generic CAPTCHA (unknown vendor) or a plain JS shell: a browser is the
    # only lever we have here.
    if det.kind in (ProtectionKind.CAPTCHA, ProtectionKind.JS_SHELL):
        return await _try_browser()

    return initial
