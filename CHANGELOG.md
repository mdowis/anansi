# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-07-19

Efficiency pass across the crawler, parser, fetchers, and MCP server (issues
#12–#26). All changes are backward compatible; the minor bump reflects the new
public helper methods added below.

### Added

- `Crawler.get_crawl(crawl_id)` and `Crawler.count_items(crawl_id)` — single-row /
  `COUNT(*)` lookups that avoid scanning every crawl or loading every item.
- `SQLiteQueue.push_batch(...)` — batched `(url, callback, priority, meta)` enqueue.
- `AdaptiveParser.extract_with_structured(...)` — parse once, return both the
  selector fields and the full structured payload.

### Changed

- **Crawler**: persistent per-page batching of link enqueue and item writes
  (`executemany`); atomic single-statement `pop` (`UPDATE … RETURNING` with the
  visited check folded in); `Response` memoizes its parsed BeautifulSoup/lxml
  trees so `css()`/`xpath()`/`follow_links` parse once; page HTML hashed once;
  `url_cache` read once per fetch; O(1) LRU for the per-domain throttle maps.
- **Parser**: one shared lxml tree per document across XPath attempts and healing;
  `extract()` skips unused microdata/SPA extraction; memoized fuzzy class matching;
  assorted hot-path cleanups.
- **Fetchers**: HTTP clients pooled per proxy and curl-cffi sessions reused across
  requests/retries; browser context pool LRU-bounded; sitemap traversal reuses one
  fetcher; `RobotsCache` gains per-origin single-flight and a bounded cache.
- **MCP server**: `fetch_and_extract`/`extract` parse once; `export_crawl` and
  `crawl_metrics` use the new single-row/count helpers and concurrent counts;
  HTML→text/markdown conversion runs off the event loop (`asyncio.to_thread`).

### Removed

- Dead adaptive-concurrency bookkeeping (`_current_concurrency` / `_outcome_window`)
  that was computed per fetch but never applied.

### CI

- Added a `concurrency` group so superseded workflow runs are cancelled.

## [1.0.1] - 2026-07-17

### Changed

- **Persistent SQLite connections**: `crawl_db()` / `selector_db()` now reuse a
  cached connection per (event loop, database path) — opened, schema-initialised,
  and migrated exactly once — instead of opening a fresh connection and replaying
  the full schema on every operation. A single page fetch previously triggered many
  connect-and-reinitialise cycles; these are now amortised to one. Call
  `anansi.db.close_all()` at shutdown to release pooled connections (the MCP server
  and CLI do this automatically).
- **Shared headless browser in the MCP server**: browser-backed tools
  (`fetch_url(use_browser=true)`, `screenshot_url`, and browser escalation) now reuse
  a long-lived `BrowserFetcher` per bot profile instead of launching a fresh Chromium
  on every call, eliminating repeated multi-second browser startups. Pooled browsers
  are closed on server shutdown via a lifespan hook.

Both changes are internal performance improvements with no public API change.

## [1.0.0] - 2026-07-15

First stable release. From this version on, the public API surface exported from
`anansi/__init__.py` follows semantic versioning — breaking changes will bump the
major version.

### Added

- **Coherent personas** (`anansi.persona`): a `Persona` model and
  `build_persona(seed=, mobile=)` that bundle User-Agent, platform, viewport,
  screen, locale, timezone, WebGL, hardware, and touch into one internally
  consistent identity, drawn from a curated real-device catalog and deterministic
  under a seed. Both the HTTP and browser fetchers drive their headers /
  fingerprint from the same persona.
- **Shared protection detection** (`anansi.protection`): `detect_protection()`
  classifies a response into a vendor (Cloudflare, Akamai, DataDome) and a kind
  (challenge, hard block, CAPTCHA, JS shell) so every layer reacts consistently.
- **Vendor-aware escalation** (`escalate_protection`): Cloudflare challenge →
  browser (no longer gated on `result.ok`); Cloudflare hard block → returned
  as-is; Akamai → impersonated TLS retry then browser; DataDome → browser. Wired
  into both the crawler and the MCP single-shot fetch path.
- **Sticky browser sessions**: `BrowserFetcher` pools contexts by
  `(domain, proxy, persona)` and preserves earned cookies (e.g. a solved
  `cf_clearance` / Akamai `_abck`) across requests instead of starting cold.
- **Target-aware proxy scoring** (`ProxyManager`): records which proxies succeed
  against which domains/vendors; `next(domain=, vendor=)` prefers proven proxies,
  penalises recent hard blocks, and falls back to round-robin for cold targets.
- **CAPTCHA interface** (`anansi.captcha`): `CaptchaSolver` protocol,
  `NullCaptchaSolver`, and `detect_captcha()` for reCAPTCHA, hCaptcha, Turnstile,
  DataDome, and FunCaptcha. Detection-only by default; an opt-in solver hook on
  `BrowserFetcher` surfaces unsolved challenges as `manual_required` without
  looping. No solving provider ships with Anansi.

### Changed

- The browser stealth fingerprint (screen, WebGL, `navigator.languages`,
  hardware, touch, platform) is now rendered from the active persona instead of
  hardcoded constants; the fixed 1920×1080 screen spoof was removed.
- Playwright contexts take their locale, timezone, viewport, and UA from the
  persona; the unconditional `geolocation` permission grant was removed.
- In a crawl, one coherent persona is bound per host and reused across the HTTP
  fetcher and any browser escalation.

### Fixed

- Cloudflare challenge click loop now exits deterministically after a single
  click instead of continuing to scan remaining frames (the previous
  timestamp-equality check never matched).

### Notes

- These features harden *browser and identity* fingerprinting; they do not
  manufacture source-IP authenticity. Hard blocks and enterprise IP-reputation
  scoring still require a better IP (residential/ISP proxy) or a manual solve.
