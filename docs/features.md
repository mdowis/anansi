# Capabilities

The full capability reference. For the marketing overview see the [README](../README.md); for
runnable usage see [Getting started](getting-started.md).

| | |
|---|---|
| **Self-healing parser** | CSS selectors are stored with confidence scores. When one breaks, four healing strategies run — fuzzy class matching, text-pattern regex, structural context, XPath fallback — and the winner is persisted for next time. |
| **Structured data extraction** | JSON-LD, Open Graph, and Microdata are extracted from every page automatically. Fields matched in schema.org markup skip CSS evaluation entirely — they're more stable and require no selector maintenance. |
| **TLS / HTTP-2 fingerprint mimicry** | Enterprise bot-detection (Cloudflare, Akamai, DataDome) fingerprints your TLS ClientHello *and* HTTP/2 SETTINGS/frame ordering before inspecting a single header. With `impersonate="chrome124"`, Anansi uses curl-cffi to reproduce both, plus per-host session warm-up and a graduated Akamai-block escalation ladder. Install the `tls` extra (see [Getting started](getting-started.md#install)); operator-gated, authorized use only. |
| **Auto browser upgrade** | Every HTTP response is checked for SPA markers, noscript redirects, and suspiciously low text density. JS shells trigger a silent retry with a stealth Playwright browser. The decision is cached per domain for the crawl session. |
| **Anti-bot & Cloudflare bypass** | The browser fetcher removes `webdriver` fingerprints, spoofs plugins, hardware concurrency, audio context, font measurements, battery API, and touch points, adds canvas/WebGL noise, auto-dismisses GDPR/cookie consent banners, and waits out Cloudflare Turnstile challenges automatically. |
| **Adaptive rate limiting** | A per-domain sliding window tracks error rates. A 429 immediately doubles the request gap and activates a circuit breaker. Sustained 5xx errors increase the gap further. Clean windows slowly decay back toward the base delay. |
| **Incremental crawling** | ETag, Last-Modified, and content MD5 are stored per URL. Re-crawls send conditional GET headers — 304 responses skip parsing entirely, and hash comparison catches changes even without server-side ETag support. Sitemap `<lastmod>` dates are used for a pre-flight filter that skips unchanged pages before a network request is even made. |
| **URL canonicalization** | Tracking parameters (`utm_*`, `fbclid`, `gclid`, and 25 others) are stripped before URLs enter the queue. Remaining parameters are sorted and fragments removed — so `?utm_source=twitter` and `?utm_source=facebook` are the same crawl target. |
| **Item validation** | Set `item_schema = MyPydanticModel` on a Spider and every yielded item is validated before persistence. Type coercion is automatic (`"49.99"` → `49.99`). Invalid items carry a `_validation_errors` key; valid/invalid counts and error rate appear in live crawl metrics. |
| **Concurrent crawler** | Pure asyncio, semaphore-gated workers, SQLite-backed URL queue. Crawls survive process restarts. Pause mid-run and resume days later with `Crawler.resume(crawl_id, MySpider)`. |
| **Proxy rotation** | HTTP/HTTPS/SOCKS5 with round-robin, random, or least-used strategies. Failed proxies are auto-quarantined and retested in the background. |
| **MCP server** | FastMCP server exposes 17 scraping tools — fetch, extract, crawl, screenshot, train/validate selectors, cancel, cache control, and more — so any LLM or tool-calling agent can drive a full crawl through a conversation. |

Also includes: JS interaction (click, fill, scroll, infinite-scroll loop, wait), network request interception (capture JSON API responses from SPAs), robots.txt compliance, sitemap discovery, content deduplication, auth/cookie support, configurable retries with `Retry-After` support, CSV/JSON/JSONL export.
