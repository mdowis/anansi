"""Internal CAPTCHA abstraction.

Makes CAPTCHA handling an explicit, pluggable step instead of behaviour buried
inside the browser fetcher. The first pass is **detection-only** plus a
graceful "not solved / manual required" default — no third-party solving
provider is wired in. A real solver (manual queue, human-in-the-loop, or a
commercial API) implements :class:`CaptchaSolver` and is passed in by the
caller; nothing here reaches out to any external service.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from anansi.protection import CAPTCHA_MARKERS


class CaptchaVendor(str, Enum):
    UNKNOWN = "unknown"
    RECAPTCHA = "recaptcha"
    HCAPTCHA = "hcaptcha"
    TURNSTILE = "turnstile"
    DATADOME = "datadome"
    FUNCAPTCHA = "funcaptcha"


@dataclass
class CaptchaChallenge:
    """A CAPTCHA detected on a page, described enough for a solver to act."""

    vendor: CaptchaVendor
    url: str
    site_key: str | None = None
    detail: str = ""


@dataclass
class CaptchaResult:
    """Outcome of an attempt (or non-attempt) to solve a challenge."""

    solved: bool
    token: str | None = None
    error: str | None = None
    # True when the challenge can only be cleared by a human / external step —
    # the signal a caller uses to surface the page for manual handling instead
    # of looping.
    manual_required: bool = False


@runtime_checkable
class CaptchaSolver(Protocol):
    """Protocol for a CAPTCHA solver. Implementations must be async and must
    never block indefinitely — return an unsolved result instead."""

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaResult: ...


class NullCaptchaSolver:
    """Default solver: solves nothing, asks for manual handling. Lets the
    CAPTCHA plumbing exist without implying automated solving is available."""

    async def solve(self, challenge: CaptchaChallenge) -> CaptchaResult:
        return CaptchaResult(
            solved=False,
            manual_required=True,
            error=f"no CAPTCHA solver configured (vendor={challenge.vendor.value})",
        )


_SITE_KEY_RE = re.compile(r"""data-sitekey=["']([^"']+)["']""", re.IGNORECASE)


def _vendor_of(sample_lower: str) -> CaptchaVendor | None:
    if "cf-turnstile" in sample_lower or "challenges.cloudflare.com/turnstile" in sample_lower:
        return CaptchaVendor.TURNSTILE
    if "h-captcha" in sample_lower or "hcaptcha.com" in sample_lower:
        return CaptchaVendor.HCAPTCHA
    if "g-recaptcha" in sample_lower or "recaptcha/api.js" in sample_lower or "grecaptcha" in sample_lower:
        return CaptchaVendor.RECAPTCHA
    if "arkoselabs" in sample_lower or "funcaptcha" in sample_lower:
        return CaptchaVendor.FUNCAPTCHA
    if "captcha-delivery.com" in sample_lower or "datadome" in sample_lower:
        return CaptchaVendor.DATADOME
    if any(m in sample_lower for m in CAPTCHA_MARKERS):
        return CaptchaVendor.UNKNOWN
    return None


def detect_captcha(html: str, url: str | None = None) -> CaptchaChallenge | None:
    """Return a :class:`CaptchaChallenge` if *html* contains a CAPTCHA, else None."""
    if not html:
        return None
    sample = html[:100_000]
    vendor = _vendor_of(sample.lower())
    if vendor is None:
        return None
    m = _SITE_KEY_RE.search(sample)
    return CaptchaChallenge(
        vendor=vendor,
        url=url or "",
        site_key=m.group(1) if m else None,
        detail=f"{vendor.value} CAPTCHA detected",
    )
