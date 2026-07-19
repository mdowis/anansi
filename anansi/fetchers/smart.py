"""
Smart JS-detection for auto-upgrading HTTP fetches to browser-rendered fetches.

``needs_browser(html)`` is a pure, synchronous function — no I/O, safe to call
on every HTTP response. It returns True when the page looks like a JS-rendered
shell that will be empty without a real browser executing the scripts.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Known SPA mount-point markers — any of these in the raw HTML strongly implies
# the page needs a browser to render meaningful content.
_SPA_MARKERS: tuple[str, ...] = (
    "data-reactroot",
    'id="__next"',
    "id='__next'",
    'id="__nuxt"',
    "id='__nuxt'",
    "ng-version",
    "__vue",
    "data-server-rendered",
    "data-ng-app",
    "ng-app=",
    "__NEXT_DATA__",
    "__NUXT__",
    "window.__INITIAL_STATE__",
    "window.__REDUX_STATE__",
    "window.__PRELOADED_STATE__",
)

# Lowercased once at import so the hot-path marker scan doesn't re-lower per call.
_SPA_MARKERS_LOWER: tuple[str, ...] = tuple(m.lower() for m in _SPA_MARKERS)

# <noscript> containing a meta-refresh redirect signals a JS-gated page.
_NOSCRIPT_REDIRECT_RE = re.compile(
    r"<noscript[^>]*>.*?http-equiv=[\"']refresh[\"'].*?</noscript>",
    re.IGNORECASE | re.DOTALL,
)

# Precompiled tag-strip patterns for the text-ratio heuristics — ``needs_browser``
# runs on every HTTP response, so compile these once instead of per call.
_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Patterns that indicate the page is intentionally thin (error / login pages)
# and should NOT trigger an auto-upgrade.
_THIN_PAGE_EXCLUSIONS: tuple[str, ...] = (
    "404",
    "403",
    "not found",
    "access denied",
    "login",
    "sign in",
    "signin",
)

# Input cap for expensive regex operations — avoids O(n²) on large pages.
_HTML_CAP = 100_000

# Body markers of an Akamai (Bot Manager / edge) hard block. Conservative on
# purpose: a false positive triggers an expensive escalation ladder, so we
# only fire on the unambiguous Akamai edge-error signature.
_AKAMAI_BODY_MARKERS: tuple[str, ...] = (
    "reference #",
    "errors.edgesuite.net",
    "akamaighost",
    "access denied",
)


def detect_akamai_block(
    html: str, status: int, headers: dict[str, str] | None = None
) -> bool:
    """Return True if the response looks like an Akamai edge bot-block.

    Pure / synchronous (no I/O) — safe to call on every response. This is
    deliberately separate from ``needs_browser`` (which excludes 403 /
    access-denied pages as "intentionally thin"); an Akamai block must be
    detected, not skipped. Detection is read-only classification and runs
    even when anti-bot evasion is disabled — it lets callers report an
    honest "blocked by Akamai" status instead of escalating.
    """
    server = ""
    if headers:
        # Header keys may be arbitrary case.
        for k, v in headers.items():
            if k.lower() == "server":
                server = (v or "").lower()
                break
    if server.startswith("akamaighost"):
        return True
    if status in (403, 429):
        sample = (html or "")[:_HTML_CAP].lower()
        if any(m in sample for m in _AKAMAI_BODY_MARKERS):
            return True
    return False


def needs_browser(html: str) -> bool:
    """Return True if *html* looks like a JS-rendered shell needing a browser.

    Heuristics (any one is sufficient to return True):
    1. A known SPA mount-point marker is present in the raw HTML.
    2. A <noscript> tag contains a meta-refresh redirect.
    3. Text-to-HTML ratio < 3% after stripping scripts, styles, and tags.
    4. Visible body text is shorter than 500 characters (excluding known thin
       pages such as 404s, login forms, or access-denied pages).

    The function is intentionally conservative — false positives waste browser
    resources, false negatives silently return empty data.
    """
    if not html or len(html) < 50:
        return False

    sample = html[:_HTML_CAP]
    sample_lower = sample.lower()

    # Heuristic 1: SPA markers
    for marker in _SPA_MARKERS_LOWER:
        if marker in sample_lower:
            logger.debug("SPA marker found: %r — flagging as JS shell", marker)
            return True

    # Heuristic 2: noscript redirect
    if _NOSCRIPT_REDIRECT_RE.search(sample):
        logger.debug("Noscript redirect detected — flagging as JS shell")
        return True

    # Heuristics 3 & 4 need visible text — strip tags without BeautifulSoup
    # to keep the hot path free of heavy imports.
    try:
        text = _SCRIPT_RE.sub("", sample)
        text = _STYLE_RE.sub("", text)
        text = _TAG_RE.sub("", text)
        text = _WS_RE.sub(" ", text).strip()
    except Exception:
        return False

    # Skip thin-content detection on known intentionally-thin pages
    text_lower = text.lower()
    if any(pat in text_lower for pat in _THIN_PAGE_EXCLUSIONS):
        return False

    body_length = len(text)
    html_length = len(sample)

    # Heuristic 3: text-to-HTML ratio < 3%
    if html_length > 0 and body_length / html_length < 0.03:
        logger.debug(
            "Text/HTML ratio %.1f%% — flagging as JS shell",
            100 * body_length / html_length,
        )
        return True

    # Heuristic 4: visible text too short for a real content page
    if body_length < 500:
        logger.debug("Body text %d chars — flagging as JS shell", body_length)
        return True

    return False
