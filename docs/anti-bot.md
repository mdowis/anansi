# Anti-bot & identity

Anansi's identity-coherence and anti-bot subsystem: TLS/HTTP-2 fingerprint mimicry, coherent
personas, crawler impersonation, vendor-aware detection and escalation, sticky sessions,
target-aware proxy scoring, and CAPTCHA handling.

> These features raise the bar on *browser and identity* fingerprinting for **authorized**
> testing, research, and scraping of content you have the right to access. They do **not**
> manufacture source-IP authenticity — see [Limitations](#limitations) and
> [`DISCLAIMER.md`](../DISCLAIMER.md).

## TLS fingerprint mimicry

```python
from anansi.fetchers.http import HTTPFetcher

# Requires the tls extra: pip install "anansi-scraper[tls] @ git+https://github.com/mdowis/anansi"
async with HTTPFetcher(impersonate="chrome124") as f:
    result = await f.fetch("https://bot-protected-site.com")
    print(result.html)

# Per-request profile rotation — vary the TLS fingerprint across requests
# to avoid a fixed JA3/JA4 hash being flagged across sessions.
async with HTTPFetcher(impersonate="chrome124") as f:
    r1 = await f.fetch("https://example.com/page1", impersonate="chrome131")
    r2 = await f.fetch("https://example.com/page2", impersonate="safari18_0")
    r3 = await f.fetch("https://example.com/page3", impersonate=None)   # plain httpx
```

Without `[tls]` installed, Anansi logs a warning and falls back to standard httpx automatically — no code change required.

## Impersonating crawlers (Googlebot)

Some sites serve their full, ungated content to search-engine crawlers for SEO
while gating browsers behind consent walls or JS shells. A **bot profile** makes
Anansi present as a known crawler: it pins the `User-Agent` to that crawler's
string, sends its accurate (minimal) header set — dropping the browser-only
`Sec-Fetch-*` / `DNT` / `Upgrade-Insecure-Requests` headers — and, in a crawl,
evaluates `robots.txt` against that crawler's agent token (e.g. `Googlebot`)
instead of `*`. Built-in profiles: `googlebot`, `googlebot-mobile`.

```python
from anansi.fetchers.http import HTTPFetcher
from anansi.spider.crawler import Crawler

# Single fetch presenting as Googlebot
async with HTTPFetcher(bot_profile="googlebot") as f:
    result = await f.fetch("https://example.com/article")

# Whole crawl as Googlebot — robots.txt is obeyed as "Googlebot"
crawler = Crawler(MySpider, bot_profile="googlebot")
```

`bot_profile` is independent of `impersonate`: it changes the presented identity
(UA + headers), not the TLS fingerprint. Combine them if a site checks both.

> **Note:** this only spoofs the User-Agent. Sites that verify crawlers by
> reverse DNS / source-IP ranges will still see a non-Google address — spoofing
> the UA does not place you on Google's network. Use responsibly and within the
> site's Terms of Service.

## Coherent personas (identity consistency)

Anti-bot systems don't just read your `User-Agent` — they cross-check it against
`navigator.platform`, screen size, viewport, WebGL vendor, hardware
concurrency, language, and timezone. A macOS UA next to a `Win32` platform or a
4K screen behind a phone UA is an instant tell. A **persona** bundles all of
these into one internally consistent identity, and both the HTTP and browser
fetchers drive their headers/fingerprint from the *same* persona.

```python
from anansi import build_persona
from anansi.fetchers.http import HTTPFetcher
from anansi.fetchers.browser import BrowserFetcher

persona = build_persona(seed=42)          # deterministic with a seed
http = HTTPFetcher(persona=persona)        # Accept-Language matches the UA
browser = BrowserFetcher(persona=persona)  # viewport/screen/WebGL/timezone all agree
```

Without a persona, each fetcher builds one automatically. In a crawl, one
coherent persona is bound per host and reused across the HTTP fetcher and any
browser escalation, so a site sees a single stable identity rather than a fresh
random fingerprint at every layer.

## Vendor-aware detection & escalation

A shared classifier (`anansi.protection.detect_protection`) identifies the
protection **vendor** (Cloudflare, Akamai, DataDome) and **kind** (solvable
challenge, hard block, CAPTCHA, or plain JS shell) from a single HTTP response —
so the crawler can react *before* wasting a full browser challenge timeout:

- **Cloudflare challenge** (arrives as 403/503) → escalate straight to a browser.
- **Cloudflare hard block** (Error 1020, "you have been blocked") → returned
  as-is; a browser on the same IP hits the same WAF rule, so it needs a cleaner
  IP or residential proxy instead.
- **Akamai** → impersonated TLS retry, then a browser that can run the sensor JS.
- **DataDome** → browser, ideally behind a sticky residential proxy.

```python
from anansi import detect_protection

d = detect_protection(html, status, headers, cookies)
print(d.vendor, d.kind, d.needs_browser, d.is_hard_block)
```

## Sticky browser sessions

Solving a Cloudflare challenge or minting an Akamai `_abck` cookie is expensive.
`BrowserFetcher` can keep the browser context (and its earned cookies) alive per
`(domain, proxy, persona)` instead of starting cold on every request, so a
challenge-heavy domain is solved once and reused:

```python
result = await browser.fetch(url, session_key="shop.example.com|noproxy|<persona>")
```

The crawler derives this key automatically. Unrelated session keys never share
state, and `force_fresh=True` (used after a challenge timeout) always bypasses
reuse with a brand-new fingerprint.

## Target-aware proxy scoring

`ProxyManager` records which proxies actually *work* for which targets, not just
whether they're alive. `next(domain=..., vendor=...)` then prefers proxies with
a proven track record against that site/vendor, penalises ones recently
hard-blocked there, and falls back to plain round-robin for cold targets.
Reporting is backward compatible — `report_success`/`report_failure` still work
with no extra arguments.

```python
pm.report_success(proxy, domain="shop.com", vendor="cloudflare")
pm.report_failure(proxy, domain="shop.com", vendor="cloudflare", hard_block=True)
best = pm.next(domain="shop.com", vendor="cloudflare")
```

## CAPTCHA solver hook

CAPTCHA handling is an explicit, opt-in interface rather than hidden browser
logic. By default Anansi is **detection-only**: it recognises reCAPTCHA,
hCaptcha, Turnstile, DataDome, and FunCaptcha and surfaces them, but never
attempts to solve. Pass a `CaptchaSolver` to attempt solving (manual queue,
human-in-the-loop, or a commercial provider you wire up yourself):

```python
from anansi import CaptchaSolver, CaptchaResult, NullCaptchaSolver
from anansi.fetchers.browser import BrowserFetcher

class MySolver:                       # implements the CaptchaSolver protocol
    async def solve(self, challenge) -> CaptchaResult:
        ...                           # your provider / human loop here

browser = BrowserFetcher(captcha_solver=MySolver())   # or NullCaptchaSolver()
```

An unsolved CAPTCHA returns the page as-is with `manual_required` set — it never
loops forever. No solving provider ships with Anansi.

## Limitations

These features raise the bar on *browser and identity* fingerprinting; they do
**not** manufacture source-IP authenticity.

- Spoofing a UA/persona does not change your IP. Enterprise bot management
  (Cloudflare Enterprise, DataDome) scores IP reputation before any solvable
  challenge — datacenter IPs are often blocked outright.
- Hard blocks still require a better IP (residential/ISP proxy) or a manual
  solve path; no amount of fingerprint tuning bypasses them.
- DataDome support is primarily detection plus a better proxy/session strategy,
  not a guaranteed bypass.
- Always operate within the target site's Terms of Service and the law.

## Operator controls

These environment variables are read once at process start and are **not** settable
by an MCP/LLM client — only by whoever runs the server:

| Variable | Default | Effect when set to `1`/`true` |
|---|---|---|
| `ANANSI_ALLOW_PRIVATE_NETWORKS` | off | Allows fetches/crawls to resolve to loopback, RFC1918, link-local, and cloud-metadata addresses. Off by default so the untrusted LLM cannot reach internal services (SSRF). Enable only on a trusted, isolated host. |
| `ANANSI_DISABLE_ANTIBOT` | off | Disables **all** anti-bot evasion: stealth-JS injection, the Cloudflare-challenge wait, curl-cffi TLS/HTTP-2 impersonation, the per-host session warm-up, the browser→HTTP cookie hand-off, and the Akamai escalation ladder. Block *detection* still runs so callers get an honest blocked status. Always wins over `ANANSI_IMPERSONATE`. |
| `ANANSI_IMPERSONATE` | unset | Default curl-cffi TLS/HTTP-2 impersonation target applied to HTTP fetches (e.g. `chrome124`). Must be an allowlisted target; an invalid value fails loud at startup. A per-call `impersonate=` argument (also allowlist-validated) overrides it. |

### Surviving Akamai / edge bot-managers (authorized use)

Akamai Bot Manager blocks via TLS JA3/JA4 fingerprint, HTTP/2 frame-ordering
fingerprint, and behavioral scoring of cold (cookie-less, no-`Referer`)
requests — block pages show `Reference #…` / `errors.edgesuite.net` and a
`Server: AkamaiGHost` header. Recommended operator recipe:

1. Install the `tls` extra and set `ANANSI_IMPERSONATE=chrome124` (replays a
   real Chrome TLS **and** HTTP/2 fingerprint — the single biggest lever).
2. Leave the per-host session warm-up and `Referer` continuity on (default)
   so behavioral scoring sees a warm session.
3. Supply **residential or mobile** proxies via the existing proxy support
   for the hardest tier — datacenter IPs are heavily penalized.
4. Allow browser escalation (`use_browser` / the automatic ladder) so the
   Akamai sensor JS can run when impersonation alone is insufficient.

**Honest limit:** the highest Akamai tier validates `_abck` via sensor JS and
also blocks headless Chromium. Even with impersonation + browser + warm-up it
may remain unreliable without residential/mobile egress, and sometimes even
then. Anansi makes a best effort and reports an honest blocked status when it
cannot get through. These features are for **authorized** scraping only — see
[`DISCLAIMER.md`](../DISCLAIMER.md); `ANANSI_DISABLE_ANTIBOT=1` turns all of it
off.
