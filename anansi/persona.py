"""Coherent browser persona model.

A :class:`Persona` bundles the identity surfaces an anti-bot system cross-checks
— User-Agent, platform, viewport, screen, locale, timezone, hardware, WebGL —
into a single internally-consistent unit. HTTP and browser fetchers both drive
their headers / fingerprint from the *same* persona so a site never sees, say, a
macOS User-Agent next to a ``Win32`` ``navigator.platform`` or a 4K screen
behind a phone UA.

Design choices:
- A small **curated catalog** of realistic bundles rather than free-form
  randomisation — every combination in the catalog is one a real device
  actually ships, so we never emit an impossible fingerprint.
- Generation is **deterministic given a seed** so tests (and reproducible
  crawls) stay stable; without a seed a fresh random persona is drawn.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class Persona:
    """An internally-consistent browser identity.

    Every field is a surface an anti-bot fingerprint check reads. They are
    chosen together (from the curated catalog) so they never contradict each
    other. ``viewport`` and ``screen`` are read-only mappings with ``width`` /
    ``height`` (and ``color_depth`` for ``screen``).
    """

    user_agent: str
    ua_family: str            # "chrome" | "firefox" | "safari"
    platform: str             # navigator.platform, e.g. "Win32", "MacIntel"
    viewport: Mapping[str, int]
    screen: Mapping[str, int]
    locale: str               # e.g. "en-US"
    timezone_id: str          # IANA tz, e.g. "America/New_York"
    accept_language: str      # e.g. "en-US,en;q=0.9"
    hardware_concurrency: int
    device_memory: int
    webgl_vendor: str
    webgl_renderer: str
    max_touch_points: int
    mobile: bool

    def __hash__(self) -> int:
        # The viewport/screen mappings are unhashable, so the auto-generated
        # frozen-dataclass __hash__ would raise. Hash the scalar surfaces plus
        # the mapping items instead so personas can live in sets / dict keys.
        return hash((
            self.user_agent, self.platform, self.locale, self.timezone_id,
            tuple(sorted(self.viewport.items())),
            tuple(sorted(self.screen.items())),
            self.hardware_concurrency, self.device_memory,
            self.max_touch_points, self.mobile,
        ))

    @property
    def persona_id(self) -> str:
        """Stable short identifier for pooling / logging (never PII)."""
        return (
            f"{self.ua_family}-{'m' if self.mobile else 'd'}-"
            f"{self.viewport['width']}x{self.viewport['height']}-"
            f"{self.platform}"
        )


# ── Curated catalog ───────────────────────────────────────────────────────────
# Each entry is a real-device fingerprint bundle. ``viewports`` lists inner
# window sizes that fit inside ``screen``; build_persona picks one. Locales pair
# a language tag with a plausible timezone.

_LOCALES = [
    ("en-US", "America/New_York", "en-US,en;q=0.9"),
    ("en-US", "America/Chicago", "en-US,en;q=0.9"),
    ("en-US", "America/Los_Angeles", "en-US,en;q=0.9"),
    ("en-GB", "Europe/London", "en-GB,en;q=0.9"),
    ("en-CA", "America/Toronto", "en-CA,en;q=0.9,fr-CA;q=0.8"),
]

_DESKTOP_CATALOG = [
    {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "ua_family": "chrome",
        "platform": "Win32",
        "screen": {"width": 1920, "height": 1080, "color_depth": 24},
        "viewports": [{"width": 1920, "height": 953}, {"width": 1536, "height": 864},
                      {"width": 1280, "height": 720}],
        "hardware_concurrency": [8, 12, 16],
        "device_memory": [8, 16],
        "webgl_vendor": "Google Inc. (Intel)",
        "webgl_renderer": (
            "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"
        ),
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "ua_family": "chrome",
        "platform": "MacIntel",
        "screen": {"width": 2560, "height": 1440, "color_depth": 30},
        "viewports": [{"width": 2560, "height": 1329}, {"width": 1680, "height": 1050},
                      {"width": 1440, "height": 900}],
        "hardware_concurrency": [8, 10, 12],
        "device_memory": [8, 16],
        "webgl_vendor": "Google Inc. (Apple)",
        "webgl_renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)",
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15"
        ),
        "ua_family": "safari",
        "platform": "MacIntel",
        "screen": {"width": 1512, "height": 982, "color_depth": 30},
        "viewports": [{"width": 1512, "height": 871}, {"width": 1280, "height": 720}],
        "hardware_concurrency": [8, 10],
        "device_memory": [8, 16],
        "webgl_vendor": "Apple",
        "webgl_renderer": "Apple M2",
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
            "Gecko/20100101 Firefox/125.0"
        ),
        "ua_family": "firefox",
        "platform": "Win32",
        "screen": {"width": 1366, "height": 768, "color_depth": 24},
        "viewports": [{"width": 1366, "height": 641}, {"width": 1280, "height": 720}],
        "hardware_concurrency": [4, 8],
        "device_memory": [8],
        "webgl_vendor": "Mozilla",
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    },
    {
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "ua_family": "chrome",
        "platform": "Linux x86_64",
        "screen": {"width": 1920, "height": 1080, "color_depth": 24},
        "viewports": [{"width": 1920, "height": 953}, {"width": 1600, "height": 900}],
        "hardware_concurrency": [8, 16],
        "device_memory": [8, 16],
        "webgl_vendor": "Google Inc. (Intel)",
        "webgl_renderer": "ANGLE (Intel, Mesa Intel(R) UHD Graphics (CML GT2), OpenGL 4.6)",
    },
]

_MOBILE_CATALOG = [
    {
        "user_agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
            "Mobile/15E148 Safari/604.1"
        ),
        "ua_family": "safari",
        "platform": "iPhone",
        "screen": {"width": 390, "height": 844, "color_depth": 24},
        "viewports": [{"width": 390, "height": 664}],
        "hardware_concurrency": [6],
        "device_memory": [4],
        "webgl_vendor": "Apple",
        "webgl_renderer": "Apple GPU",
        "max_touch_points": 5,
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
        ),
        "ua_family": "chrome",
        "platform": "Linux armv8l",
        "screen": {"width": 412, "height": 915, "color_depth": 24},
        "viewports": [{"width": 412, "height": 738}],
        "hardware_concurrency": [8],
        "device_memory": [8],
        "webgl_vendor": "Google Inc. (Qualcomm)",
        "webgl_renderer": "ANGLE (Qualcomm, Adreno (TM) 740, OpenGL ES 3.2)",
        "max_touch_points": 5,
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
        ),
        "ua_family": "chrome",
        "platform": "Linux armv8l",
        "screen": {"width": 360, "height": 780, "color_depth": 24},
        "viewports": [{"width": 360, "height": 640}],
        "hardware_concurrency": [8],
        "device_memory": [8],
        "webgl_vendor": "Google Inc. (Qualcomm)",
        "webgl_renderer": "ANGLE (Qualcomm, Adreno (TM) 740, OpenGL ES 3.2)",
        "max_touch_points": 5,
    },
]


def _freeze(d: dict[str, int]) -> Mapping[str, int]:
    return MappingProxyType(dict(d))


def build_persona(
    *, seed: int | None = None, mobile: bool | None = None
) -> Persona:
    """Build one coherent :class:`Persona`.

    Args:
        seed: When provided, selection is deterministic — the same seed always
            yields the same persona (used by tests and reproducible crawls).
            When ``None`` a fresh random persona is drawn each call.
        mobile: Force a mobile (``True``) or desktop (``False``) persona. When
            ``None`` the catalog is chosen at random (weighted toward desktop,
            the common scraping case).
    """
    rng = random.Random(seed)

    if mobile is None:
        # Desktop is the common case; keep mobile a minority of random draws.
        mobile = rng.random() < 0.25

    catalog = _MOBILE_CATALOG if mobile else _DESKTOP_CATALOG
    base = rng.choice(catalog)
    locale, timezone_id, accept_language = rng.choice(_LOCALES)
    viewport = rng.choice(base["viewports"])

    return Persona(
        user_agent=base["user_agent"],
        ua_family=base["ua_family"],
        platform=base["platform"],
        viewport=_freeze(viewport),
        screen=_freeze(base["screen"]),
        locale=locale,
        timezone_id=timezone_id,
        accept_language=accept_language,
        hardware_concurrency=rng.choice(base["hardware_concurrency"]),
        device_memory=rng.choice(base["device_memory"]),
        webgl_vendor=base["webgl_vendor"],
        webgl_renderer=base["webgl_renderer"],
        max_touch_points=base.get("max_touch_points", 0),
        mobile=mobile,
    )
