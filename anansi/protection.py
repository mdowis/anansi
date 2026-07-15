"""Shared anti-bot protection detection.

One classifier, used by the HTTP fetcher, the browser fetcher, the crawler's
escalation ladder, and proxy scoring, so every layer agrees on *what* is in
front of a page and *how* to react. It answers two questions from a single
response:

- **vendor** — which protection service (Cloudflare, Akamai, DataDome, …)
- **kind** — what it is doing right now (a solvable challenge, a hard block, a
  CAPTCHA, a JS shell, or nothing)

Detection is pure and synchronous (no I/O) and read-only classification: it runs
even when anti-bot evasion is disabled, so callers can always report an honest
"blocked by X" instead of silently escalating.

The canonical vendor marker lists live here; ``browser.py`` and ``smart.py``
import them rather than keeping their own copies.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

# NOTE: ``detect_akamai_block`` / ``needs_browser`` live in anansi.fetchers.smart
# and are imported lazily inside detect_protection(). A module-level import would
# pull in the fetchers package (and browser.py, which imports this module),
# creating a circular import.

# ── Vendor markers (canonical — single source of truth) ───────────────────────

# Solvable Cloudflare challenge pages (IUAM / Turnstile / Managed Challenge).
CLOUDFLARE_CHALLENGE_MARKERS: tuple[str, ...] = (
    "cf-turnstile",
    "challenge-platform",
    "cf_chl_opt",
    "Cloudflare Ray ID",
    "Please wait...",
    "Just a moment",
    "__cf_chl",
)

# Hard Cloudflare blocks (IP ban, WAF rule, rate limit) — NOT solvable by
# waiting or clicking; they need a different IP or human intervention.
CLOUDFLARE_BLOCK_MARKERS: tuple[str, ...] = (
    "Sorry, you have been blocked",
    "You are unable to access",
    "This website is using a security service to protect itself",
    "Error 1020",   # Access Denied (WAF rule)
    "Error 1010",   # Your IP address is banned
    "Error 1015",   # You are being rate limited
    "Error 1012",   # IP address restricted
    "Attention Required!",  # legacy CF block heading
)

# DataDome device-check / block markers (body + endpoints).
DATADOME_MARKERS: tuple[str, ...] = (
    "datadome",
    "geo.captcha-delivery.com",
    "captcha-delivery.com",
    "dd_cookie",
)

# Generic interactive-CAPTCHA markers (any vendor).
CAPTCHA_MARKERS: tuple[str, ...] = (
    "g-recaptcha",
    "grecaptcha",
    "recaptcha/api.js",
    "h-captcha",
    "hcaptcha.com",
    "arkoselabs",
    "funcaptcha",
    "captcha-delivery.com",
)

_HTML_CAP = 100_000


class ProtectionVendor(str, Enum):
    NONE = "none"
    CLOUDFLARE = "cloudflare"
    AKAMAI = "akamai"
    DATADOME = "datadome"
    UNKNOWN = "unknown"


class ProtectionKind(str, Enum):
    NONE = "none"
    JS_SHELL = "js_shell"     # not a protection wall; page just needs a browser
    CHALLENGE = "challenge"   # solvable by executing JS / waiting / clicking
    BLOCK = "block"           # hard block; needs a different IP or human
    CAPTCHA = "captcha"       # interactive CAPTCHA present


@dataclass
class ProtectionDetection:
    """Result of classifying one HTTP response."""

    vendor: ProtectionVendor = ProtectionVendor.NONE
    kind: ProtectionKind = ProtectionKind.NONE
    reason: str = ""

    @property
    def is_protected(self) -> bool:
        """True when a protection vendor / challenge / block was identified.

        A bare JS shell (page needs a browser but is not gated) counts as
        protected-needs-browser too, since callers escalate the same way.
        """
        return self.kind is not ProtectionKind.NONE

    @property
    def needs_browser(self) -> bool:
        """True when escalating to a real browser could plausibly help.

        Challenges, CAPTCHAs, and JS shells are worth a browser attempt; a hard
        block is not (a browser on the same IP will be blocked identically).
        """
        return self.kind in (
            ProtectionKind.JS_SHELL,
            ProtectionKind.CHALLENGE,
            ProtectionKind.CAPTCHA,
        )

    @property
    def is_hard_block(self) -> bool:
        return self.kind is ProtectionKind.BLOCK


def _header(headers: Mapping[str, str] | None, name: str) -> str:
    if not headers:
        return ""
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return (v or "")
    return ""


def _has(sample: str, markers: tuple[str, ...]) -> bool:
    return any(m.lower() in sample for m in markers)


def _cloudflare_signal(
    sample: str, headers: Mapping[str, str] | None
) -> bool:
    """Is Cloudflare in the request path at all (regardless of block/challenge)?"""
    if "cloudflare" in _header(headers, "server").lower():
        return True
    if _header(headers, "cf-ray") or _header(headers, "cf-mitigated"):
        return True
    if "cloudflare" in sample:
        return True
    return _has(sample, CLOUDFLARE_CHALLENGE_MARKERS) or _has(
        sample, CLOUDFLARE_BLOCK_MARKERS
    )


def _datadome_signal(
    sample: str, headers: Mapping[str, str] | None, cookies: Mapping[str, str] | None
) -> bool:
    if _header(headers, "x-datadome") or _header(headers, "x-dd-b"):
        return True
    if cookies and any(k.lower() == "datadome" for k in cookies):
        return True
    # Set-Cookie may not be split into the cookies map; check the raw header.
    if "datadome=" in _header(headers, "set-cookie").lower():
        return True
    return _has(sample, DATADOME_MARKERS)


def detect_protection(
    html: str,
    status: int,
    headers: Mapping[str, str] | None = None,
    cookies: Mapping[str, str] | None = None,
    url: str | None = None,
) -> ProtectionDetection:
    """Classify an HTTP response into (vendor, kind).

    Ordering is deliberate: named vendors are checked before generic CAPTCHA
    markers, which are checked before the JS-shell heuristic, which is checked
    before declaring the page clean. The first positive wins.
    """
    from anansi.fetchers.smart import detect_akamai_block, needs_browser

    sample = (html or "")[:_HTML_CAP]
    sample_lower = sample.lower()

    # ── Cloudflare ────────────────────────────────────────────────────────────
    if _cloudflare_signal(sample_lower, headers):
        if _has(sample_lower, CLOUDFLARE_BLOCK_MARKERS):
            return ProtectionDetection(
                ProtectionVendor.CLOUDFLARE, ProtectionKind.BLOCK,
                "Cloudflare hard block (WAF / IP reputation)",
            )
        if _has(sample_lower, CLOUDFLARE_CHALLENGE_MARKERS):
            return ProtectionDetection(
                ProtectionVendor.CLOUDFLARE, ProtectionKind.CHALLENGE,
                "Cloudflare interstitial challenge",
            )
        # CF is present and the response is an error but the body carries no
        # explicit marker (e.g. a bare 403/503 from the edge): treat as a
        # challenge worth a browser attempt rather than a clean page.
        if status in (403, 429, 503):
            return ProtectionDetection(
                ProtectionVendor.CLOUDFLARE, ProtectionKind.CHALLENGE,
                f"Cloudflare edge error {status}",
            )

    # ── DataDome ──────────────────────────────────────────────────────────────
    if _datadome_signal(sample_lower, headers, cookies):
        # DataDome device checks usually surface an interactive CAPTCHA.
        if _has(sample_lower, CAPTCHA_MARKERS) or status in (403, 405):
            return ProtectionDetection(
                ProtectionVendor.DATADOME, ProtectionKind.CAPTCHA,
                "DataDome device-check / CAPTCHA",
            )
        return ProtectionDetection(
            ProtectionVendor.DATADOME, ProtectionKind.CHALLENGE,
            "DataDome challenge",
        )

    # ── Akamai ────────────────────────────────────────────────────────────────
    if detect_akamai_block(html, status, headers):  # type: ignore[arg-type]
        return ProtectionDetection(
            ProtectionVendor.AKAMAI, ProtectionKind.BLOCK,
            "Akamai edge bot-manager block",
        )

    # ── Generic CAPTCHA (unknown vendor) ──────────────────────────────────────
    if _has(sample_lower, CAPTCHA_MARKERS):
        return ProtectionDetection(
            ProtectionVendor.UNKNOWN, ProtectionKind.CAPTCHA,
            "Interactive CAPTCHA detected",
        )

    # ── Plain JS shell (needs a browser, but not gated by a vendor) ───────────
    if needs_browser(html):
        return ProtectionDetection(
            ProtectionVendor.NONE, ProtectionKind.JS_SHELL,
            "JS-rendered shell (no server-side content)",
        )

    return ProtectionDetection(ProtectionVendor.NONE, ProtectionKind.NONE, "")
