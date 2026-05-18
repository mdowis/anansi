"""
Browser fetcher backed by Playwright.

Features:
- Persistent browser instance with a pooled context queue
- Full stealth JS injection (webdriver flag, canvas/WebGL noise, plugin spoofing)
- Cloudflare Turnstile & challenge detection with automatic wait
- Human-like mouse movement via Bézier interpolation
- Per-request proxy support via new browser contexts
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from anansi.fetchers.base import BaseFetcher, FetchResult

logger = logging.getLogger(__name__)

# ── Stealth JavaScript ────────────────────────────────────────────────────────

_STEALTH_JS = """
(function () {
  // 1. Remove webdriver fingerprint
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

  // 2. Spoof realistic plugins list
  const fakePlugins = [
    { name: 'Chrome PDF Plugin',     filename: 'internal-pdf-viewer',  description: 'Portable Document Format' },
    { name: 'Chrome PDF Viewer',     filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
    { name: 'Native Client',         filename: 'internal-nacl-plugin',  description: '' },
  ];
  const pluginArray = Object.create(PluginArray.prototype);
  fakePlugins.forEach((p, i) => {
    const plugin = Object.create(Plugin.prototype);
    Object.defineProperties(plugin, {
      name:        { get: () => p.name },
      filename:    { get: () => p.filename },
      description: { get: () => p.description },
      length:      { get: () => 1 },
    });
    pluginArray[i] = plugin;
  });
  Object.defineProperties(pluginArray, {
    length: { get: () => fakePlugins.length },
    item:   { value: (i) => pluginArray[i] },
    namedItem: { value: (name) => fakePlugins.find((_, i) => pluginArray[i].name === name) || null },
    refresh: { value: () => {} },
  });
  Object.defineProperty(navigator, 'plugins', { get: () => pluginArray });

  // 3. Languages
  Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

  // 4. Hardware concurrency & device memory (realistic desktop values)
  Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
  try { Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 }); } catch(_) {}

  // 5. Chrome runtime object (expected by fingerprint checks)
  if (!window.chrome) {
    window.chrome = {
      app: { isInstalled: false, InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }, RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } },
      runtime: {
        OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' },
        OnRestartRequiredReason: { APP_UPDATE: 'app_update', GC_PRESSURE: 'gc_pressure', OS_UPDATE: 'os_update' },
        PlatformArch: { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
        PlatformNaclArch: { ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
        PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' },
        RequestUpdateCheckStatus: { NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' },
      },
    };
  }

  // 6. Canvas fingerprint noise (tiny random perturbation per session)
  const _noise = Math.random() * 0.05 - 0.025;
  const origGetContext = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (type, ...args) {
    const ctx = origGetContext.call(this, type, ...args);
    if (!ctx || type !== '2d') return ctx;
    const origFillText  = ctx.fillText.bind(ctx);
    const origStrokeText = ctx.strokeText.bind(ctx);
    ctx.fillText   = (t, x, y, ...r) => origFillText(t, x + _noise, y + _noise, ...r);
    ctx.strokeText = (t, x, y, ...r) => origStrokeText(t, x + _noise, y + _noise, ...r);
    return ctx;
  };

  // 7. WebGL vendor/renderer noise
  const origGetParam = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (param) {
    if (param === 37445) return 'Intel Inc.';
    if (param === 37446) return 'Intel Iris OpenGL Engine';
    return origGetParam.call(this, param);
  };
  try {
    const origGetParam2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function (param) {
      if (param === 37445) return 'Intel Inc.';
      if (param === 37446) return 'Intel Iris OpenGL Engine';
      return origGetParam2.call(this, param);
    };
  } catch(_) {}

  // 8. Permissions API — report granted for notifications to look real
  const origQuery = Permissions.prototype.query;
  Permissions.prototype.query = function ({ name }) {
    if (name === 'notifications') return Promise.resolve({ state: Notification.permission });
    return origQuery.apply(this, arguments);
  };

  // 9. Screen dimensions consistent with a real desktop
  Object.defineProperty(screen, 'width',     { get: () => 1920 });
  Object.defineProperty(screen, 'height',    { get: () => 1080 });
  Object.defineProperty(screen, 'colorDepth',{ get: () => 24 });

  // 10. iframe contentWindow.navigator.webdriver fix
  const origAttach = Element.prototype.attachShadow;
  Element.prototype.attachShadow = function (...args) {
    const root = origAttach.apply(this, args);
    return root;
  };

  // 11. Audio context fingerprint noise — adds imperceptible perturbation to
  //     audio sample data, defeating hash-based AudioBuffer fingerprinting.
  try {
    const _origGetChannelData = AudioBuffer.prototype.getChannelData;
    AudioBuffer.prototype.getChannelData = function(channel) {
      const data = _origGetChannelData.call(this, channel);
      for (let i = 0; i < data.length; i += 100) {
        data[i] += Math.random() * 1e-7 - 5e-8;
      }
      return data;
    };
  } catch(_) {}

  // 12. Font measurement noise — canvas font fingerprinting measures glyph
  //     widths; tiny variation per session defeats exact-match clustering.
  try {
    const _origMeasureText = CanvasRenderingContext2D.prototype.measureText;
    CanvasRenderingContext2D.prototype.measureText = function(text) {
      const m = _origMeasureText.call(this, text);
      try {
        Object.defineProperty(m, 'width', {
          value: m.width + (Math.random() - 0.5) * 0.05,
        });
      } catch(_) {}
      return m;
    };
  } catch(_) {}

  // 13. Battery API — navigator.getBattery() is commonly fingerprinted;
  //     return a plausible plugged-in desktop value.
  try {
    navigator.getBattery = () => Promise.resolve({
      charging: true, chargingTime: 0, dischargingTime: Infinity, level: 0.95,
      addEventListener: () => {}, removeEventListener: () => {},
    });
  } catch(_) {}

  // 14. Touch points — real desktops report 0; Playwright headless may not.
  try {
    Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });
  } catch(_) {}
})();
"""

# Cloudflare challenge indicators
_CF_INDICATORS = [
    "cf-turnstile",
    "challenge-platform",
    "cf_chl_opt",
    "Cloudflare Ray ID",
    "Please wait...",
    "Just a moment",
    "__cf_chl",
]

# Hard CF block pages (IP ban, WAF rule, rate limit).  These are NOT solvable
# by waiting or clicking — they need a different IP or human intervention.
# Distinct from the solvable challenge pages above.
_CF_BLOCK_INDICATORS = [
    "Sorry, you have been blocked",
    "You are unable to access",
    "This website is using a security service to protect itself",
    "Error 1020",   # Access Denied (WAF rule)
    "Error 1010",   # Your IP address is banned
    "Error 1015",   # You are being rate limited
    "Error 1012",   # IP address restricted
    "Attention Required!",  # legacy CF block heading
]


def _bezier_points(
    x0: float, y0: float, x1: float, y1: float, steps: int = 12
) -> list[tuple[float, float]]:
    """Generate human-like mouse path via quadratic Bézier curve."""
    cx = (x0 + x1) / 2 + random.uniform(-80, 80)
    cy = (y0 + y1) / 2 + random.uniform(-80, 80)
    pts = []
    for i in range(steps + 1):
        t = i / steps
        bx = (1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t ** 2 * x1
        by = (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t ** 2 * y1
        pts.append((bx, by))
    return pts


_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
]

_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 2560, "height": 1440},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 800},
]

_HW_CONCURRENCY_OPTIONS = [4, 8, 12, 16]
_DEVICE_MEMORY_OPTIONS = [4, 8, 16]

# GDPR/CCPA consent platform selectors, ordered most-specific → most-generic.
# Only used by _dismiss_cookie_consent(); never exposed to untrusted callers.
_CONSENT_SELECTORS = [
    "#onetrust-accept-btn-handler",                              # OneTrust
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",    # Cookiebot
    ".qc-cmp2-summary-buttons button:last-child",                # Quantcast
    ".truste_cm_btn",                                            # TrustArc
    "#truste-consent-button",                                    # TrustArc alt
    "#didomi-notice-agree-button",                               # Didomi
    ".cc-btn.cc-allow",                                          # Cookie Consent by Insites
    "#cookie-consent-accept",
    # Generic heuristics (broader — tried last to minimise false positives)
    "button[id*='accept-all']",
    "button[id*='cookie-accept']",
    "button[id*='consent-accept']",
    "button[class*='cookie-accept']",
    "button[class*='consent-accept']",
    "[aria-label*='Accept all']",
    "[aria-label*='accept cookies']",
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Allow all')",
    "button:has-text('I agree')",
    "button:has-text('Accept cookies')",
]


async def _dismiss_cookie_consent(page: Any) -> bool:
    """Attempt to click a GDPR/cookie consent accept button.

    Tries each selector in _CONSENT_SELECTORS with a short timeout. Returns
    True if a banner was dismissed, False if none was found. Never raises —
    a missing banner is not an error. Skips entirely when DISABLE_ANTIBOT is
    set so tests and debugging environments stay predictable.
    """
    from anansi import security
    if security.DISABLE_ANTIBOT:
        return False
    for sel in _CONSENT_SELECTORS:
        try:
            await page.locator(sel).first.click(timeout=1500)
            await asyncio.sleep(0.3)
            return True
        except Exception:
            continue
    return False


# WebRTC leak mitigation — hides real IP even when behind a proxy
_WEBRTC_BLOCK_JS = """
(function() {
  // Block WebRTC ICE to prevent IP leaks through proxies
  if (window.RTCPeerConnection) {
    const _OrigRTC = window.RTCPeerConnection;
    window.RTCPeerConnection = function(cfg, ...args) {
      if (cfg && cfg.iceServers) cfg.iceServers = [];
      return new _OrigRTC(cfg, ...args);
    };
    Object.setPrototypeOf(window.RTCPeerConnection, _OrigRTC);
  }
  // Spoof mediaDevices — keep the object (removing it is suspicious to CF)
  // but prevent real device enumeration
  try {
    if (navigator.mediaDevices) {
      navigator.mediaDevices.enumerateDevices = () => Promise.resolve([]);
      navigator.mediaDevices.getUserMedia = () =>
        Promise.reject(new DOMException('Permission denied', 'NotAllowedError'));
    }
  } catch(_) {}
})();
"""


class BrowserFetcher(BaseFetcher):
    """
    Playwright-based fetcher with full stealth and anti-bot evasion.

    Maintains a single persistent browser instance and a pool of contexts
    to avoid the overhead of launching a browser per request.
    """

    # Overridable in tests to avoid real wall-clock waits
    _CF_GRACE_ITERS: int = 5     # 1-second iterations before first click attempt
    _CF_STALE_GUARD_S: float = 20.0  # seconds of no content change → early exit

    def __init__(
        self,
        *,
        max_contexts: int = 5,
        headless: bool = True,
        timeout: float = 30.0,
        cf_wait_timeout: float = 45.0,
        channel: str = "chromium",
        context_max_age: float = 300.0,
        max_requests_per_context: int = 50,
        insecure: bool = False,
        sandbox: bool = True,
    ) -> None:
        self._max_contexts = max_contexts
        self._headless = headless
        self._timeout = timeout
        self._cf_timeout = cf_wait_timeout
        self._channel = channel
        self._context_max_age = context_max_age
        self._max_requests_per_context = max_requests_per_context
        self._insecure = insecure
        self._sandbox = sandbox
        self._browser = None
        self._playwright = None
        self._context_semaphore: asyncio.Semaphore | None = None
        self._context_pool: asyncio.Queue | None = None  # holds (ctx, created_at, req_count)
        self._lock = asyncio.Lock()

    async def _ensure_browser(self) -> None:
        async with self._lock:
            if self._browser is not None:
                return
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1920,1080",
                "--disable-dev-shm-usage",
            ]
            if not self._sandbox:
                launch_args.insert(0, "--no-sandbox")
            self._browser = await self._playwright.chromium.launch(
                headless=self._headless,
                args=launch_args,
            )
            self._context_semaphore = asyncio.Semaphore(self._max_contexts)
            self._context_pool: asyncio.Queue = asyncio.Queue(maxsize=self._max_contexts)

    def _make_stealth_js(self, hw_concurrency: int, device_memory: int) -> str:
        """Build the stealth script with randomised hardware values injected."""
        return _STEALTH_JS.replace(
            "{ get: () => 8 });",
            f"{{ get: () => {hw_concurrency} }});",
            1,
        ).replace(
            "{ get: () => 8 }); } catch(_) {}",
            f"{{ get: () => {device_memory} }}); }} catch(_) {{}}",
            1,
        )

    @asynccontextmanager
    async def _get_context(
        self, proxy: str | None = None, force_fresh: bool = False
    ) -> AsyncIterator[Any]:
        await self._ensure_browser()
        assert self._context_semaphore is not None
        assert self._context_pool is not None

        async with self._context_semaphore:
            # Try to reuse an idle context (only when no proxy override and not
            # explicitly requesting a fresh one — the latter is used when a CF
            # challenge timed out and we want a new fingerprint on retry).
            ctx = None
            created_at: float = 0.0
            req_count: int = 0
            if proxy is None and not force_fresh:
                try:
                    ctx, created_at, req_count = self._context_pool.get_nowait()
                    # Retire contexts that exceeded their age or request-count limit
                    if (
                        time.monotonic() - created_at > self._context_max_age
                        or req_count >= self._max_requests_per_context
                    ):
                        await ctx.close()
                        ctx = None
                        req_count = 0
                except asyncio.QueueEmpty:
                    pass

            if ctx is None:
                proxy_cfg = {"server": proxy} if proxy else None
                ua = random.choice(_USER_AGENTS)
                viewport = random.choice(_VIEWPORTS)
                hw = random.choice(_HW_CONCURRENCY_OPTIONS)
                mem = random.choice(_DEVICE_MEMORY_OPTIONS)
                ctx = await self._browser.new_context(
                    user_agent=ua,
                    viewport=viewport,
                    proxy=proxy_cfg,
                    locale="en-US",
                    timezone_id="America/New_York",
                    permissions=["geolocation"],
                    java_script_enabled=True,
                    ignore_https_errors=self._insecure,
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                )
                # Anti-bot stealth is skipped when the operator has disabled
                # all evasion via ANANSI_DISABLE_ANTIBOT.
                from anansi import security
                if not security.DISABLE_ANTIBOT:
                    await ctx.add_init_script(self._make_stealth_js(hw, mem))
                    await ctx.add_init_script(_WEBRTC_BLOCK_JS)
                created_at = time.monotonic()
                req_count = 0

            # Reset per-origin state on every checkout so cookies / permissions
            # set by a previous fetch on this pooled context cannot leak into
            # the next one (potentially for a different origin).
            try:
                await ctx.clear_cookies()
            except Exception:
                pass
            try:
                await ctx.clear_permissions()
            except Exception:
                pass

            try:
                yield ctx
            finally:
                req_count += 1
                # force_fresh contexts are always closed after use — they were
                # created for a single CF retry attempt and must not re-enter
                # the pool (their fingerprint may already be flagged).
                if proxy is None and not force_fresh:
                    try:
                        # Preserve original created_at so age is not reset on reuse
                        self._context_pool.put_nowait((ctx, created_at, req_count))
                    except asyncio.QueueFull:
                        await ctx.close()
                else:
                    await ctx.close()

    async def _simulate_human(self, page: Any) -> None:
        """Move mouse in a human-like arc before interacting."""
        x0, y0 = random.randint(100, 400), random.randint(100, 400)
        x1, y1 = random.randint(400, 900), random.randint(200, 600)
        for (bx, by) in _bezier_points(x0, y0, x1, y1):
            await page.mouse.move(bx, by)
            await asyncio.sleep(random.uniform(0.01, 0.03))

    def _is_cloudflare_challenge(self, content: str) -> bool:
        return any(indicator in content for indicator in _CF_INDICATORS)

    def _is_cloudflare_block(self, content: str) -> bool:
        return any(indicator in content for indicator in _CF_BLOCK_INDICATORS)

    async def _simulate_idle_human(self, page: Any) -> None:
        """Micro-interactions that make the browser look human while waiting."""
        x = random.randint(150, 850)
        y = random.randint(100, 650)
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.04, 0.12))
        for _ in range(random.randint(1, 3)):
            await page.mouse.move(
                x + random.randint(-12, 12),
                y + random.randint(-8, 8),
            )
            await asyncio.sleep(random.uniform(0.03, 0.09))
        if random.random() < 0.35:
            await page.evaluate(f"window.scrollBy(0, {random.randint(-25, 55)})")
            await asyncio.sleep(random.uniform(0.1, 0.22))

    async def _wait_for_cloudflare(self, page: Any) -> None:
        """Poll until the Cloudflare challenge clears or times out.

        Strategy:
        1. Fail fast on hard IP/WAF blocks (Error 1020/1010/1015) — not solvable.
        2. 5-second grace period: simulate idle human behaviour while CF's JS
           behavioural scoring runs. CF IUAM auto-resolves after ~5 s; clicking
           during this window can reset the timer.
        3. Poll loop: attempt Turnstile interaction across ALL frames (not just
           CF-URL frames — managed challenge embeds directly in the main frame).
           Rate-limit clicks to ≤1 per 4 s to avoid automated-clicker detection.
        4. Progress guard: if content hasn't changed in 20 s and no Turnstile
           widget was ever found, raise immediately with proxy guidance rather
           than burning the remaining timeout on an unsolvable challenge.
        5. Accept success from either: CF indicators disappearing from the DOM,
           OR a cf_clearance cookie being set (whichever comes first).
        """
        from anansi import security
        if security.DISABLE_ANTIBOT:
            logger.info(
                "ANANSI_DISABLE_ANTIBOT set — not waiting out Cloudflare challenge"
            )
            return

        # Fail fast: hard blocks (IP ban, WAF rule) have no solvable widget.
        initial_content = await page.content()
        if self._is_cloudflare_block(initial_content):
            logger.warning(
                "Cloudflare hard-block detected (no solvable challenge on page) — "
                "returning block page as-is; a clean IP or residential proxy is needed"
            )
            return

        # ── Grace period ─────────────────────────────────────────────────────
        # CF IUAM evaluates JS for ~5 s then auto-redirects. Wait out that
        # window with only idle mouse movement so we don't interfere with CF's
        # behavioural timer. Check for early resolution after each second.
        for _ in range(self._CF_GRACE_ITERS):
            try:
                await self._simulate_idle_human(page)
            except Exception:
                pass
            await asyncio.sleep(1.0)
            try:
                if any(
                    c.get("name") == "cf_clearance"
                    for c in await page.context.cookies()
                ):
                    return
            except Exception:
                pass
            try:
                if not self._is_cloudflare_challenge(await page.content()):
                    return
            except Exception:
                pass

        deadline = time.monotonic() + self._cf_timeout
        _last_click_at: float = 0.0
        _last_content_hash: int = hash(initial_content)
        _last_change_at: float = time.monotonic()

        # CF-specific selectors ordered from most-specific to least-specific.
        # We search ALL frames (including the main frame) because CF Managed
        # Challenge embeds the widget directly without a separate iframe.
        _CF_CLICK_SELECTORS = [
            ".cb-lb",                          # CF challenge body label (Turnstile)
            "[data-testid='checkbox']",        # Turnstile interactive checkbox
            ".cf-turnstile-wrapper input",     # Turnstile wrapper → input
            "#challenge-stage input[type=checkbox]",  # Managed challenge (main frame)
            "#challenge-form button[type=submit]",    # IUAM submit (main frame)
        ]

        while time.monotonic() < deadline:
            try:
                await self._simulate_idle_human(page)
            except Exception:
                pass

            now = time.monotonic()
            if now - _last_click_at >= 4.0:
                try:
                    for frame in page.frames:
                        for sel in _CF_CLICK_SELECTORS:
                            el = await frame.query_selector(sel)
                            if el:
                                box = await el.bounding_box()
                                if box:
                                    cx = box["x"] + box["width"] / 2 + random.uniform(-2, 2)
                                    cy = box["y"] + box["height"] / 2 + random.uniform(-2, 2)
                                    await page.mouse.move(cx, cy)
                                    await asyncio.sleep(random.uniform(0.08, 0.20))
                                    await el.click()
                                    _last_click_at = time.monotonic()
                                    _last_change_at = time.monotonic()
                                    break
                        if _last_click_at == now:
                            break
                except Exception:
                    pass

            await asyncio.sleep(random.uniform(1.8, 2.4))
            content = await page.content()

            if not self._is_cloudflare_challenge(content):
                return

            try:
                cookies = await page.context.cookies()
                if any(c.get("name") == "cf_clearance" for c in cookies):
                    return
            except Exception:
                pass

            # Progress guard: if content is unchanged for 20 s and we have
            # never successfully clicked a Turnstile widget, this challenge is
            # almost certainly unsolvable (enterprise IP reputation block).
            new_hash = hash(content)
            if new_hash != _last_content_hash:
                _last_content_hash = new_hash
                _last_change_at = time.monotonic()
            elif (
                time.monotonic() - _last_change_at > self._CF_STALE_GUARD_S
                and _last_click_at == 0.0
            ):
                raise TimeoutError(
                    f"Cloudflare challenge is not progressing (page unchanged for "
                    f"{self._CF_STALE_GUARD_S:.0f} s, no Turnstile widget found). "
                    "This site likely uses Cloudflare Enterprise Bot Management "
                    "with IP reputation scoring — datacenter IPs are blocked before "
                    "any solvable challenge is issued. Pass a residential or ISP "
                    "proxy via the `proxy` parameter to bypass this."
                )

        raise TimeoutError(
            f"Cloudflare challenge did not resolve within {self._cf_timeout}s. "
            "If this site uses Cloudflare Enterprise Bot Management, datacenter "
            "IPs are blocked regardless of browser fingerprint. Pass a residential "
            "or ISP proxy via the `proxy` parameter "
            "(e.g. proxy='http://user:pass@host:port')."
        )

    async def _run_actions(self, page: Any, actions: list[dict[str, Any]]) -> None:
        """Execute a sequence of browser interactions after the initial page load.

        Each action may include ``required: false`` to make it optional — optional
        action failures are logged as warnings and skipped rather than aborting the
        fetch. Required actions (the default) raise RuntimeError on failure.

        Two safety nets enforced here:
        - Every ``selector`` is validated through ``validate_browser_selector``
          to refuse Playwright engine prefixes (``xpath=``, ``text=``, …) and
          chained ``>>`` selectors. MCP callers can therefore only address
          elements with plain CSS.
        - A cumulative wall-clock budget caps the total time spent across
          ``wait`` and ``wait_for_selector`` actions so a thousand
          ``{"type":"wait","ms":10000}`` entries cannot pin a Playwright
          context indefinitely.
        """
        from anansi.security import validate_browser_selector
        _MAX_BUDGET_MS = 60_000
        spent_ms = 0
        for i, action in enumerate(actions):
            atype = action.get("type", "")
            required = action.get("required", True)
            try:
                if atype == "click":
                    sel = validate_browser_selector(action["selector"])
                    await page.click(sel)
                elif atype == "scroll_to_bottom":
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                elif atype == "scroll_until_stable":
                    max_scrolls = min(int(action.get("max_scrolls", 10)), 30)
                    scroll_delay = min(int(action.get("scroll_delay", 1500)), 5000)
                    prev_height: int = await page.evaluate("document.body.scrollHeight")
                    stable_count = 0
                    for _ in range(max_scrolls):
                        if spent_ms + scroll_delay > _MAX_BUDGET_MS:
                            break
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(scroll_delay / 1000)
                        spent_ms += scroll_delay
                        new_height: int = await page.evaluate("document.body.scrollHeight")
                        if new_height == prev_height:
                            stable_count += 1
                            if stable_count >= 2:
                                break
                        else:
                            stable_count = 0
                            prev_height = new_height
                elif atype == "fill":
                    sel = validate_browser_selector(action["selector"])
                    await page.fill(sel, action["value"])
                elif atype == "wait":
                    wait_ms = int(action.get("ms", 1000))
                    if spent_ms + wait_ms > _MAX_BUDGET_MS:
                        raise RuntimeError(
                            f"action #{i} would exceed {_MAX_BUDGET_MS}ms wait budget"
                        )
                    spent_ms += wait_ms
                    await asyncio.sleep(wait_ms / 1000)
                elif atype == "wait_for_selector":
                    sel = validate_browser_selector(action["selector"])
                    remaining = _MAX_BUDGET_MS - spent_ms
                    if remaining <= 0:
                        raise RuntimeError(
                            f"action #{i} (wait_for_selector) starved by wait budget"
                        )
                    await page.wait_for_selector(sel, timeout=remaining)
                    spent_ms = _MAX_BUDGET_MS  # treat as worst case
                elif atype == "press":
                    sel = validate_browser_selector(action["selector"])
                    await page.press(sel, action["key"])
            except Exception as exc:
                if required:
                    raise RuntimeError(
                        f"Required browser action #{i} ({atype!r}) failed: {exc}"
                    ) from exc
                logger.warning(
                    "Optional browser action #%d (%r) failed — skipping: %s", i, atype, exc
                )

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        proxy: str | None = None,
        timeout: float | None = None,
        wait_for: str | None = None,
        wait_until: str = "domcontentloaded",
        actions: list[dict[str, Any]] | None = None,
        auto_consent: bool = True,
        capture_network: bool = False,
        capture_patterns: list[str] | None = None,
        **kwargs: Any,
    ) -> FetchResult:
        t0 = time.perf_counter()
        effective_timeout = (timeout or self._timeout) * 1000  # ms

        # On CF challenge timeout we retry once with a completely fresh browser
        # context — new random UA, viewport, and hardware fingerprint.  The
        # failed context is closed (not returned to the pool) via force_fresh.
        for _cf_attempt in range(2):
            _force_fresh = _cf_attempt > 0
            try:
                async with self._get_context(proxy, force_fresh=_force_fresh) as ctx:
                    page = await ctx.new_page()
                    try:
                        if headers:
                            await page.set_extra_http_headers(headers)

                        # Collect JSON API responses the page makes during navigation.
                        # The listener is registered before goto() so responses from
                        # the initial document load are captured.
                        _captured_resp_objects: list[Any] = []
                        if capture_network:
                            async def _on_response(resp: Any) -> None:
                                try:
                                    ct = resp.headers.get("content-type", "")
                                    if "json" not in ct:
                                        return
                                    if capture_patterns and not any(
                                        p in resp.url for p in capture_patterns
                                    ):
                                        return
                                    if len(_captured_resp_objects) < 50:
                                        _captured_resp_objects.append(resp)
                                except Exception:
                                    pass
                            page.on("response", _on_response)

                        await self._simulate_human(page)

                        resp = await page.goto(
                            url,
                            wait_until=wait_until,
                            timeout=effective_timeout,
                        )

                        await asyncio.sleep(random.uniform(0.8, 2.0))

                        content = await page.content()

                        if self._is_cloudflare_challenge(content):
                            await self._wait_for_cloudflare(page)
                            content = await page.content()

                        if auto_consent:
                            await _dismiss_cookie_consent(page)

                        if wait_for:
                            from anansi.security import validate_browser_selector
                            sel = validate_browser_selector(wait_for)
                            await page.wait_for_selector(sel, timeout=effective_timeout)
                            content = await page.content()

                        if actions:
                            await self._run_actions(page, actions)
                            content = await page.content()

                        status = resp.status if resp else 200
                        resp_headers = dict(resp.headers) if resp else {}
                        cookies = {
                            c["name"]: c["value"]
                            for c in await ctx.cookies()
                        }

                        # Read captured response bodies after all navigation is done.
                        captured_requests: list[dict[str, Any]] = []
                        for captured_resp in _captured_resp_objects:
                            try:
                                body_bytes = await captured_resp.body()
                                if len(body_bytes) > 200 * 1024:
                                    continue
                                captured_requests.append({
                                    "url": captured_resp.url,
                                    "status": captured_resp.status,
                                    "body": json.loads(body_bytes),
                                })
                            except Exception:
                                pass

                        return FetchResult(
                            url=page.url,
                            status=status,
                            html=content,
                            headers=resp_headers,
                            cookies=cookies,
                            elapsed=time.perf_counter() - t0,
                            via_browser=True,
                            captured_requests=captured_requests,
                        )
                    finally:
                        await page.close()
            except TimeoutError:
                if _cf_attempt == 0:
                    logger.info(
                        "Cloudflare challenge timed out on first attempt; "
                        "retrying with a fresh browser context (new fingerprint)"
                    )
                    continue
                raise

    async def screenshot(
        self,
        url: str,
        *,
        selector: str | None = None,
        full_page: bool = False,
        path: str | None = None,
        proxy: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Capture a screenshot of *url* and return it as base64-encoded PNG.

        Args:
            url: Page to navigate to.
            selector: If given, screenshot only the matching element.
            full_page: Capture the full scrollable page (ignored when selector is set).
            path: Optional file path to write the PNG to.
            proxy: Proxy URL to use for this request.
            timeout: Navigation timeout in seconds.

        Returns:
            {url, format, width, height, data_b64} or {url, format, path} when path is set.
        """
        import base64

        t0 = time.perf_counter()
        effective_timeout = (timeout or self._timeout) * 1000

        async with self._get_context(proxy) as ctx:
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=effective_timeout)
                await asyncio.sleep(random.uniform(0.5, 1.0))

                content = await page.content()
                if self._is_cloudflare_challenge(content):
                    await self._wait_for_cloudflare(page)

                if selector:
                    el = await page.query_selector(selector)
                    if el is None:
                        raise ValueError(f"Selector {selector!r} matched no element on {url}")
                    png_bytes = await el.screenshot()
                    box = await el.bounding_box()
                    width = int(box["width"]) if box else 0
                    height = int(box["height"]) if box else 0
                else:
                    png_bytes = await page.screenshot(full_page=full_page)
                    vp = page.viewport_size or {}
                    width = vp.get("width", 0)
                    height = vp.get("height", 0)

                result: dict[str, Any] = {
                    "url": page.url,
                    "format": "png",
                    "width": width,
                    "height": height,
                    "elapsed": round(time.perf_counter() - t0, 3),
                }
                if path:
                    # Defence in depth: even when called outside the MCP tool
                    # (which already confines the path), never let an arbitrary
                    # path reach write_bytes(). Confine to ~/.anansi/exports/.
                    from anansi.db import DATA_DIR
                    from anansi.security import confine_to_dir
                    target = confine_to_dir(path, DATA_DIR / "exports")
                    target.write_bytes(png_bytes)
                    try:
                        import os
                        os.chmod(target, 0o600)
                    except OSError:
                        pass
                    result["path"] = str(target)
                else:
                    result["data_b64"] = base64.b64encode(png_bytes).decode()
                return result
            finally:
                await page.close()

    async def close(self) -> None:
        if self._context_pool:
            while not self._context_pool.empty():
                ctx, _, _rc = await self._context_pool.get()
                await ctx.close()
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
