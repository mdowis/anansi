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
