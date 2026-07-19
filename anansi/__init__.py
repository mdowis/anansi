"""
Anansi — The spider that learns.

Adaptive web scraping framework with self-healing selectors,
anti-bot bypass, concurrent crawling, and MCP server integration.
"""

from anansi.captcha import (
    CaptchaChallenge,
    CaptchaResult,
    CaptchaSolver,
    CaptchaVendor,
    NullCaptchaSolver,
    detect_captcha,
)
from anansi.core import Crawler, Spider, Request, Response, Item
from anansi.fetchers.http import HTTPFetcher
from anansi.fetchers.browser import BrowserFetcher
from anansi.fetchers.smart import needs_browser
from anansi.parser.adaptive import AdaptiveParser
from anansi.persona import Persona, build_persona
from anansi.protection import (
    ProtectionDetection,
    ProtectionKind,
    ProtectionVendor,
    detect_protection,
)
from anansi.proxy.manager import ProxyManager

__version__ = "1.1.0"
__all__ = [
    "Crawler",
    "Spider",
    "Request",
    "Response",
    "Item",
    "HTTPFetcher",
    "BrowserFetcher",
    "needs_browser",
    "AdaptiveParser",
    "Persona",
    "build_persona",
    "ProtectionDetection",
    "ProtectionKind",
    "ProtectionVendor",
    "detect_protection",
    "CaptchaChallenge",
    "CaptchaResult",
    "CaptchaSolver",
    "CaptchaVendor",
    "NullCaptchaSolver",
    "detect_captcha",
    "ProxyManager",
]
