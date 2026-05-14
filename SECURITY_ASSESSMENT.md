# Security Assessment — `anansi-scraper`

**Repository:** `mdowis/adaptive-web-scraper`
**Commit reviewed:** `88011ff` (tip of `main` at time of audit)
**Branch:** `claude/security-assessment-review-u3AEU`
**Date:** 2026-05-13
**Reviewer:** automated whole-codebase audit
**Remediation status (2026-05-14):** All HIGH/MEDIUM/LOW findings remediated on branch `claude/fix-security-assessment-lUQjv`. INFO-2 also closed; remaining INFO items are documentation-only by design. See section 6 for per-finding status.

---

## 1. Executive summary

`anansi` is a Python web scraping library that ships an MCP server (`anansi-mcp`) exposing fetch, extract, crawl, and export tools to any MCP client. The library itself follows several good hygiene practices — parameterized SQL throughout, no `eval`/`exec`/`pickle`/`yaml.load`/`shell=True`, no committed secrets, HTML-parser mode (not XML) for `lxml`, and httpx's secure-by-default TLS verification on the HTTP path. Where the codebase is weak is at the **trust boundary between the MCP client and the operator's machine** and at the **trust boundary between the scraper and remote sites it visits**.

The audit found **3 High**, **6 Medium**, **4 Low**, and **5 Informational** issues plus a list of explicitly-OK checks. The three issues that most warrant attention before exposing the MCP server to an untrusted LLM are:

1. **Arbitrary file write via `export_crawl`** (`mcp_server/server.py:660` → `crawler.py:756`) — a path string from the MCP client is passed to `Path(path).write_text(...)` with no sandboxing.
2. **SSRF in every fetch tool** (`mcp_server/server.py:242`, `:304`, `:373`, `:457`) — no scheme allowlist and no block on RFC1918 / loopback / link-local, including AWS/GCE metadata `169.254.169.254`.
3. **Cross-origin credential leakage in `crawl_site`** — `cookies` and `auth_headers` are forwarded to every URL the crawler reaches; with the default `allowed_domains=[]` ("allows all domains" per the docstring), session tokens leak the first time an off-domain link is followed.

## 2. Threat model & scope

**Trusted**

- The operator's machine and filesystem.
- The Python interpreter and PyPI packages installed at audit time.
- The local SQLite databases in `~/.anansi/`.

**Untrusted**

- The **MCP client** (any LLM connected to `anansi-mcp` over stdio). Treated as a fully attacker-controlled source of: URLs, regex patterns, header/cookie dicts, proxy URLs, file paths, browser-action selectors, and arbitrary kwargs to the exposed tools.
- **Remote HTTP responses**, including redirect targets, HTML/JSON bodies, `robots.txt`, and `sitemap.xml` (and recursive child sitemaps). A hostile or compromised site can return anything within HTTP/HTML semantics.
- **Proxies** passed to the fetcher (they see plaintext request bodies and can return arbitrary content).

**In scope**

- SSRF, path traversal, arbitrary file write, credential leakage, ReDoS, decompression bombs, TLS bypass, sandbox-escape blast-radius, log-based secret disclosure, resource exhaustion (memory/time DoS *of the scraper itself*).

**Out of scope**

- Denial-of-service against scraped sites, anti-bot ethics, captcha-solving legality.
- Network-level concerns (TLS pinning, DNS rebinding mitigations beyond the SSRF check).
- Hardening of optional dependencies (`curl-cffi`) beyond what they expose to arachne.
- Anything that requires shell access to the operator's machine before the attack begins.

## 3. Findings

### HIGH-1 — Arbitrary file write via `export_crawl` path argument [x] FIXED
**Status:** Fixed. `export_crawl` now confines `path` to `~/.anansi/exports/` via `confine_to_dir()` (`anansi/mcp_server/server.py:931-939`); `PathOutsideSandboxError` surfaces as a structured tool error. Exported files are chmod'd to `0o600` (`anansi/spider/crawler.py:1013-1019`).
- **Severity:** High
- **CWE:** CWE-22 (Path Traversal), CWE-73 (External Control of File Name or Path)
- **Locations:** `anansi/mcp_server/server.py:660`, `anansi/spider/crawler.py:704-757`
- **Description:** The MCP-exposed tool `export_crawl(crawl_id, format, path)` accepts a fully attacker-controlled `path: str | None`. The string is handed unchanged to `Crawler.export_items()` which calls `Path(path).write_text(out, encoding="utf-8")` at `crawler.py:756`. There is no rejection of absolute paths, no `..` segment stripping, no allowlist of an export directory, and no overwrite protection.
- **Exploit (untrusted MCP client):** The client invokes `export_crawl("<id>", format="csv", path="/home/operator/.ssh/authorized_keys")` and chooses a `format=csv` whose serialized output happens to start with an attacker-controlled extracted field (e.g. an `ssh-rsa …` line). Because `crawl_site` lets the client supply `start_url` and `selectors`, the client can stage exactly the bytes it wants written. Less dramatically the client can overwrite `~/.anansi/crawls.db`, drop a `.bashrc`, or write into Python `site-packages` if the process is privileged.
- **Recommendation:** Restrict exports to a configurable directory (e.g. `~/.anansi/exports/`); resolve via `Path(path).resolve()`, then verify `is_relative_to(export_root)`; reject paths whose basename contains separators; refuse to overwrite existing files unless an explicit flag is set. If the operator legitimately needs arbitrary destinations, gate that behind an env var (`ANANSI_MCP_ALLOW_EXPORT_PATHS=1`) that defaults off.

### HIGH-2 — SSRF in all MCP fetch tools (no host/scheme allowlist) [x] FIXED
**Status:** Fixed. `anansi/security.py:is_url_safe_for_public_fetch()` performs DNS-resolution-time rejection of loopback / link-local / private / multicast / reserved / metadata addresses; only `http(s)` schemes are accepted. Wired into every fetch tool (`server.py:285,421,491-494,553-556,704`) and into the crawler dispatcher loop (`crawler.py:574-581`). httpx auto-redirect is disabled and each redirect hop is re-validated (`http.py:139, 285-329`). `allow_private_networks: bool = False` exposed on every tool for operator opt-in.
- **Severity:** High
- **CWE:** CWE-918 (Server-Side Request Forgery), CWE-441 (Confused Deputy)
- **Locations:** `anansi/mcp_server/server.py:242` (`fetch_url`), `:304` (`fetch_urls`), `:373` (`fetch_and_extract`), `:457` (`crawl_site`); fetch primitive at `anansi/fetchers/http.py:118-142`.
- **Description:** None of the fetch tools validate the destination URL. `httpx` will refuse non-`http(s)` schemes, but `http://127.0.0.1:*`, `http://[::1]/`, RFC1918 ranges, link-local `169.254.0.0/16`, and the cloud metadata endpoint `http://169.254.169.254/latest/meta-data/iam/security-credentials/` are all reachable. `follow_redirects=True` is the constructor default (`http.py:91`), so a public URL can redirect into the private range and bypass any naive client-side filter.
- **Exploit (untrusted MCP client):** `fetch_url("http://169.254.169.254/latest/meta-data/iam/security-credentials/")` on an EC2 host returns IAM role credentials. On a developer workstation `fetch_url("http://localhost:9200/_cat/indices")` enumerates an internal Elasticsearch.
- **Recommendation:** Implement a DNS-resolution-time SSRF guard (resolve, reject if any A/AAAA result is in: loopback, link-local, private, multicast, reserved, or the metadata IP); apply it on initial request and on each redirect hop (set `follow_redirects=False` and follow manually, or use `httpx`'s event hooks). Expose `allow_private_networks: bool = False` on each tool for operators who need it. Apply the same check inside `_parse_sitemap` (see HIGH-3) before fetching child sitemaps.

### HIGH-3 — Sitemap-driven SSRF and unbounded recursion [x] FIXED
**Status:** Fixed. `anansi/sitemap.py:152-191` caps recursion depth at `_MAX_SITEMAP_DEPTH = 3`, total fan-out at `_MAX_CHILD_SITEMAPS = 1000`, applies `is_url_safe_for_public_fetch()` to every child `<loc>`, and requires the child URL to share the parent sitemap's registrable domain (`same_registrable_domain` heuristic).
- **Severity:** High
- **CWE:** CWE-918, CWE-674 (Uncontrolled Recursion)
- **Location:** `anansi/sitemap.py:109-127`
- **Description:** `_parse_sitemap()` reads `<loc>` elements out of an attacker-controlled XML stream and fetches each child URL via a fresh `HTTPFetcher` with no scheme check, no host check, no tie-back to the originating crawl's `start_url`, and no depth or breadth limit on the recursion. The recursion depth is bounded only by the remote attacker's willingness to keep returning `<sitemapindex>` documents.
- **Exploit:** A site advertises `https://evil.example/sitemap.xml` which is a `<sitemapindex>` listing `http://169.254.169.254/latest/meta-data/`, `http://127.0.0.1:6379/`, etc. The scraper fetches all of them; their responses are then returned to the MCP client as crawl items.
- **Recommendation:** (a) Apply the SSRF guard from HIGH-2 to every URL pulled from a sitemap. (b) Constrain `<loc>` URLs to the same registrable domain as the parent sitemap (or the crawl's `allowed_domains`). (c) Cap recursion depth (e.g. 3) and total fan-out (e.g. 50 000 entries) per crawl. (d) Reject child URLs whose scheme is not `http(s)`.

### MED-1 — Gzip bomb on sitemap decompression [x] FIXED
**Status:** Fixed. `safe_gzip_decompress()` streams via `gzip.GzipFile` in 64 KiB chunks and raises `DecompressionTooLargeError` once output exceeds `max_output_bytes` (`anansi/security.py:135-154`). Sitemap uses a 50 MB cap (`anansi/sitemap.py:22, 70-77`).
- **Severity:** Medium
- **CWE:** CWE-409 (Improper Handling of Highly Compressed Data)
- **Location:** `anansi/sitemap.py:43-56` (`_maybe_decompress`)
- **Description:** `gzip.decompress(data.encode("latin-1"))` is called with no maximum output size. A ~10 KB `.gz` can decompress to gigabytes of zeros and exhaust process memory before the regex parser is reached.
- **Recommendation:** Stream-decompress via `gzip.GzipFile` and abort once the cumulative output exceeds a sane cap (e.g. 50 MB). Also enforce a maximum compressed-input size (e.g. 10 MB) before decompression begins.

### MED-2 — TLS verification disabled in browser fetcher [x] FIXED
**Status:** Fixed. `BrowserFetcher` now defaults `insecure: bool = False` and passes `ignore_https_errors=self._insecure` to the Playwright context (`anansi/fetchers/browser.py:205, 215, 297`). Operators who legitimately need to skip verification must opt in explicitly.
- **Severity:** Medium
- **CWE:** CWE-295 (Improper Certificate Validation)
- **Location:** `anansi/fetchers/browser.py:254`
- **Description:** Every Playwright context is created with `ignore_https_errors=True`. There is no per-call opt-in; callers cannot enable verification. The HTTP fetcher (`http.py`) correctly relies on httpx's secure default, so this is browser-mode-specific.
- **Exploit:** Anyone on path between the scraper and the target site (rogue proxy, hostile coffee-shop Wi-Fi, malicious upstream) can serve a forged certificate, MitM the connection, and harvest any cookies/auth headers the caller passed via `crawl_site`. Combined with HIGH-2 the impact compounds (the MitM also bypasses any host-based SSRF check at the TLS layer).
- **Recommendation:** Default `ignore_https_errors=False`. Expose `insecure: bool = False` on the `BrowserFetcher` constructor and on `fetch_url(use_browser=True, ...)` for callers who genuinely need it (e.g. scraping a site behind an internal CA).

### MED-3 — Cross-origin credential leakage in `crawl_site` [x] FIXED
**Status:** Fixed. `Crawler.__init__` accepts `credential_scope_host`; cookies and `auth_headers` are stripped on any request whose host does not share that registrable domain (`crawler.py:794-815`). The MCP `crawl_site` defaults to the start_url's registrable domain and exposes `forward_credentials_cross_origin: bool = False` for the rare legitimate cross-origin case (`server.py:641-686, 788-805`). When `allowed_domains` is omitted, the crawl is auto-scoped to the start_url's registrable domain (`server.py:742-752`).
- **Severity:** Medium (can rise to High depending on the caller's threat model)
- **CWE:** CWE-200 (Exposure of Sensitive Information), CWE-346 (Origin Validation Error)
- **Locations:** `anansi/mcp_server/server.py:467-468` (params accepted), `server.py:567-568` (forwarded to `Crawler`), `anansi/spider/crawler.py:207-208` (stored), and the per-request emission of cookies/`auth_headers` to whatever URL the dispatcher picks up. The default `allowed_domains=[]` is documented as "allows all domains" at `server.py:504`.
- **Description:** When a caller passes `cookies={"session": "..."}` and `auth_headers={"Authorization": "Bearer ..."}` to `crawl_site`, those credentials are attached to **every** outgoing request — including the first off-domain link followed from the start page. A hostile or compromised page on the target site can include a `<a href="https://attacker.example/leak">` and capture the bearer token.
- **Recommendation:** By default, scope `cookies` and `auth_headers` to the **registrable domain** of `start_url` (or to `allowed_domains` if set). Expose an explicit `forward_credentials_cross_origin: bool = False` flag for the rare case where multiple cooperating origins legitimately share a token. Strip `Authorization` (and any `Cookie` header) on redirect to a different origin.

### MED-4 — ReDoS via MCP-controlled regex inputs [x] FIXED
**Status:** Fixed. `validate_regex()` enforces a 1000-char length cap and rejects nested-unbounded-quantifier shapes ((a+)+ / (a|b)+) before `re.compile` is called (`anansi/security.py:159-195`). Applied at MCP entry to `link_pattern` and every entry of `deny_patterns` (`server.py:711-716`).
- **Severity:** Medium
- **CWE:** CWE-1333 (Inefficient Regular Expression Complexity)
- **Locations:** `anansi/mcp_server/server.py:459` (`link_pattern`), `:473` (`deny_patterns`); evaluated in `anansi/spider/crawler.py:47` via `re.search(pat, url)` for every URL the crawler considers; also applied to spider link-following rules at `server.py:528`.
- **Description:** Both `link_pattern` and entries in `deny_patterns` are raw regex strings taken from the MCP client and matched against every candidate URL on the hot path. A pathological pattern (e.g. `(a+)+$`, or `^(.*?)(.*?)(.*?)…/admin/`) against attacker-chosen URLs causes catastrophic backtracking and stalls the crawler worker (with `worker_timeout=120` at `crawler.py:192`, so each URL costs up to 2 minutes of CPU before the timeout fires).
- **Recommendation:** Compile patterns at tool-entry with a complexity check (reject obviously-malicious patterns), or evaluate using `regex` library's `timeout` parameter, or replace `re` with `re2` (`google-re2`) for these matchers since they only need substring-style features. At minimum, fail fast if `re.compile(pattern)` raises and document the regex flavour.

### MED-5 — Arbitrary Playwright actions from MCP client [x] FIXED
**Status:** Fixed. Action type allowlist (`click`, `scroll_to_bottom`, `fill`, `press`, `wait`, `wait_for_selector`), press-key allowlist (no global hotkeys), and `validate_browser_selector()` (CSS-only — no `xpath=`/`text=`/engine-prefix selectors, no `>>` chaining) are enforced in `_validate_actions()` at MCP entry and again at runtime in `BrowserFetcher._run_actions` (`server.py:49-128, 388, 472`; `security.py:307-332`). Action list is also capped at `_MAX_ACTIONS = 50`.
- **Severity:** Medium
- **CWE:** CWE-352 (CSRF-like via authenticated headless session), CWE-20 (Improper Input Validation)
- **Location:** `anansi/fetchers/browser.py:312-342` (`_run_actions`), invoked from `mcp_server/server.py:251` and `:285` (action list flows in unchanged).
- **Description:** The MCP client can submit `[{"type":"click","selector":"..."}, {"type":"fill","selector":"#csrf","value":"..."}, {"type":"press","selector":"#submit","key":"Enter"}]`. Combined with MED-3 (the same call accepts `cookies`/`auth_headers` indirectly via the shared context cookie jar across `crawl_site`), the LLM can drive an authenticated browser session to perform state-changing actions on the target site. This is not RCE on the operator's host, but it confused-deputy's the operator's IP/cookies into performing actions on whatever site the operator authenticates to.
- **Recommendation:** Treat `actions` as a sensitive capability. At minimum, document the risk; consider an opt-in `allow_actions: bool` on the tool surface, restrict the `key` set on `press` (no global hotkeys), and disallow `actions` together with cross-domain link following inside `crawl_site`.

### MED-6 — Proxy credentials logged at WARNING [x] FIXED
**Status:** Fixed. `redact_userinfo()` rewrites `scheme://user:pass@host` to `scheme://***@host` (`anansi/security.py:87-99`) and is applied at every proxy-URL log site and in MCP error responses (`proxy/manager.py:158, 185, 217`; `server.py:423, 494, 556, 734`).
- **Severity:** Medium
- **CWE:** CWE-532 (Insertion of Sensitive Information into Log File)
- **Location:** `arachne/proxy/manager.py:155`, `:214`
- **Description:** Proxy entries are stored verbatim, including any `user:pass@` portion (`manager.py:79-81`). When the health check quarantines a proxy the full URL is logged at WARNING (`"Proxy quarantined: %s"`, `manager.py:155`) and at INFO when it recovers (`:214`). Operators frequently ship logs to centralized systems (Datadog, Splunk, CloudWatch), where the credentials become broadly readable.
- **Recommendation:** Add a `_redact_userinfo(url)` helper that rewrites `scheme://user:pass@host:port/...` to `scheme://***@host:port/...` and use it at every logger call site. Apply the same redaction to any exception messages re-raised from `httpx` that may include the proxy URL.

### LOW-1 — No response size cap on HTTP fetch [x] FIXED
**Status:** Fixed. `HTTPFetcher` defaults to a 50 MB cap (`_DEFAULT_MAX_RESPONSE_BYTES`), checks `Content-Length` first, raises `ResponseTooLargeError` if either the header or the materialised body exceeds the cap, and tightens redirect chain length to `_MAX_REDIRECTS = 5` (`anansi/fetchers/http.py:29-40, 230-233, 336-359`).
- **Severity:** Low
- **CWE:** CWE-770 (Allocation of Resources Without Limits)
- **Locations:** `anansi/fetchers/http.py:_fetch_httpx` (around `:250` — `resp.text` read in full) and `_fetch_curl_cffi` (`:202`).
- **Description:** Both fetch paths read the full response body into a `str`. A hostile server can stream gigabytes of body and exhaust the process memory. MCP-driven concurrency multiplies the impact.
- **Recommendation:** Pass `httpx.AsyncClient(... limits=httpx.Limits(...))` and stream the response with a hard cap (e.g. 10 MB by default, configurable). Refuse to materialize text if `Content-Length` exceeds the cap; for chunked responses, stop and discard once the cap is reached.

### LOW-2 — Robots `Crawl-delay` unbounded → self-DoS [x] FIXED
**Status:** Fixed. `_MAX_ROBOTS_CRAWL_DELAY = 300.0` clamps any `Crawl-delay` value before it is applied to the per-domain throttle gap (`anansi/spider/crawler.py:43-45, 599-613`).
- **Severity:** Low
- **CWE:** CWE-606 (Unchecked Input for Loop Condition)
- **Location:** `anansi/spider/crawler.py:395-403`
- **Description:** A site's `robots.txt` returning `Crawl-delay: 999999` causes the per-domain throttle gap to be set to that value, stalling further fetches to that domain for ~11 days. There is no upper bound check; the `_MAX_GAP` cap (`crawler.py:63`) only applies to the adaptive 429-driven path.
- **Recommendation:** `self._domain_throttle._gaps[domain] = min(robots_delay, self._MAX_ROBOTS_DELAY)` where the constant is e.g. 300s.

### LOW-3 — DB path traversal via `crawl_db(path=...)` [x] FIXED
**Status:** Fixed (documentary). `crawl_db()` carries an in-code SECURITY warning stating that untrusted `path` values must be confined via `anansi.security.confine_to_dir` before being forwarded (`anansi/db.py:108-112`). No MCP tool currently forwards a user-controlled `path` to `crawl_db()`.
- **Severity:** Low (no MCP tool exposes this directly)
- **CWE:** CWE-22
- **Location:** `arachne/db.py:108-114`
- **Description:** `crawl_db()` accepts an arbitrary `path: Path | str | None`. If a future MCP tool ever forwards a user-supplied path to this function, the operator's filesystem is reachable. Today this is a library-level concern rather than an MCP exposure.
- **Recommendation:** Validate that the resolved path lives under `DATA_DIR` (or an explicitly-configured root). Cheap to add, removes a latent footgun.

### LOW-4 — Unbounded MCP page cache [x] FIXED
**Status:** Fixed. Page cache now enforces both an entry-count cap (`_PAGE_CACHE_MAX = 200`) and an aggregate-byte cap (`_PAGE_CACHE_MAX_BYTES = 100 MB`); LRU eviction continues until both caps are satisfied (`anansi/mcp_server/server.py:62-63, 131-134, 332-347`).
- **Severity:** Low
- **CWE:** CWE-770
- **Location:** `anansi/mcp_server/server.py:70-74`
- **Description:** `_page_cache` is an `OrderedDict` LRU-capped at 200 entries, but each entry holds the full HTML (no per-entry byte cap). A handful of multi-megabyte pages can balloon RSS. Combined with LOW-1, the cap can be reached with a tiny number of huge entries.
- **Recommendation:** Add a per-entry size limit and an aggregate-bytes cap separate from the entry count.

### INFO-1 — Browser sandbox disabled
- **Location:** `anansi/fetchers/browser.py:210` (`--no-sandbox` in launch args).
- **Note:** Sometimes required when running headless Chromium inside an unprivileged container or as a non-root user without user namespaces. Document the recommendation: don't run the MCP server as root, and prefer launching with the sandbox where possible (a config flag would let the operator choose). Not classed as a vulnerability on its own — but it does mean any Chromium 0-day triggered by attacker-controlled HTML has full process privileges.

### INFO-2 — `datetime.datetime.utcnow()` deprecation [x] FIXED
- **Location:** `anansi/mcp_server/server.py:734`.
- **Note:** Cosmetic; switch to `datetime.datetime.now(tz=datetime.UTC)`. No security impact.
- **Status:** Fixed. Replaced with `datetime.datetime.now(tz=datetime.timezone.utc)`; created timestamps without tzinfo are normalised to UTC before subtraction (`anansi/mcp_server/server.py:989-995`).

### INFO-3 — Session cookies retained in `HTTPFetcher._session_cookies` without per-host scoping
- **Location:** `anansi/fetchers/http.py:105`, `:199-200`, `:266-270` (cookie-jar updates).
- **Note:** When a single `HTTPFetcher` is reused across domains (e.g. during a multi-domain crawl), `Set-Cookie` from one origin can leak to another. The httpx client built at `:108-115` is a single shared jar across all hosts the fetcher visits. Standard browser cookie-scoping (Domain/Path) isn't enforced beyond what httpx does. Lower severity than MED-3 because it requires the remote site to actively `Set-Cookie`, but worth tightening if cross-origin crawls are common.

### INFO-4 — Regex-based sitemap parsing
- **Location:** `anansi/sitemap.py:116, 130, 155`.
- **Note:** Intentionally avoids `xml.etree`/`lxml.etree`, which dodges XXE / billion-laughs entirely. Trade-off: CDATA-wrapped or entity-encoded URLs are silently ignored. This is a deliberate, defensible choice; leaving as-is.

### INFO-5 — Health-check side channel via `httpbin.org`
- **Location:** `arachne/proxy/manager.py:22`.
- **Note:** The default health-check URL is `https://httpbin.org/ip`. A determined attacker controlling httpbin.org (or doing DNS interception on the operator's network) can correlate proxy fingerprints and timing. Already overridable via the `health_check_url` constructor kwarg. Cosmetic; consider documenting.

## 4. What was checked and is OK

These categories were specifically searched for and found clean:

- **Code execution sinks:** no `eval(`, `exec(`, `pickle.load`, `marshal.load`, `compile(... 'exec')` in the codebase.
- **YAML:** project does not depend on `pyyaml`; no `yaml.load` calls.
- **Subprocess / shell:** no `subprocess` with `shell=True`, no `os.system`, no `os.popen`.
- **SQL injection:** all queries in `anansi/spider/queue.py` and `arachne/db.py` use `?`-parameterized statements with tuples. No string-built queries observed. Schema is hardcoded.
- **XML/XXE:** `arachne/core.py:49` and `arachne/parser/adaptive.py:170` use `lxml.etree.fromstring(html, lxml.etree.HTMLParser())` — HTML parser mode, not XML, so external entities and DTDs are not processed. `sitemap.py` is regex-based (see INFO-4).
- **TLS on the HTTP path:** `httpx.AsyncClient(...)` in `anansi/fetchers/http.py:108-115` does not pass `verify=False`; defaults to certificate verification. `curl-cffi` path (`:178-189`) likewise does not disable verification.
- **Hardcoded secrets:** no API keys, bearer tokens, or passwords committed. `auth_headers`/`cookies` are caller-supplied function parameters only.
- **`.gitignore` hygiene:** standard Python ignores, `~/.anansi/` excluded, no `.env` overrides. Nothing sensitive is checked in.
- **Temp files:** no `tempfile.mktemp()` (the insecure form). Filesystem writes are via `pathlib.Path` with `parents=True, exist_ok=True`.
- **DB file permissions:** SQLite files created in `Path.home() / ".arachne"`, inheriting the user's umask. Fine for single-user installs; multi-user installs should review.

## 5. Dependency notes

Direct runtime dependencies declared in `pyproject.toml:15-29`:

| Package | Pin | Notes |
|---|---|---|
| `httpx[http2]` | `>=0.27` | Active CVE history; pin upper bound or watch advisories. HTTP/2 enabled by default in this project. |
| `playwright` | `>=1.44` | Bundled Chromium; tracks Chromium CVE cadence. Worth a `>=1.44,<2` cap. |
| `beautifulsoup4` | `>=4.12` | Low risk on its own. |
| `lxml` | `>=5.0` | Historical XXE/RCE CVEs but only relevant if XML parser used (not the case here). Keep current. |
| `aiosqlite` | `>=0.20` | Thin wrapper over stdlib `sqlite3`. |
| `mcp[cli]` | `>=1.3` | Defines the tool transport; review when the MCP spec changes. |
| `pydantic` | `>=2.0` | Used for schema validation only. |
| `tenacity` | `>=8.0` | Retry helper; low risk. |
| `fake-useragent` | `>=1.5` | Network-fetches a UA list on first use — small supply-chain surface. |
| `cssselect` | `>=1.2` | Low risk. |
| `rich` | `>=13.0` | Terminal output. |
| `anyio` | `>=4.0` | Async primitives. |
| `markdownify` | `>=0.13` | HTML→MD; low risk. |
| `curl-cffi` (extra `tls`) | `>=0.6` | Bundles libcurl; pin and watch CVEs. |

All dependencies use floor-only version specifiers. Consider adding upper bounds for `httpx`, `playwright`, `lxml`, and `curl-cffi` to reduce blast-radius if a future major release introduces a vulnerable default.

## 6. Recommendations summary

| # | Finding | Severity | Fix sketch | Effort | Status |
|---|---|---|---|---|---|
| HIGH-1 | Arbitrary file write in `export_crawl` | High | Resolve & confine to `~/.anansi/exports/`; refuse `..` and absolute paths. | S | [x] Fixed |
| HIGH-2 | SSRF in fetch tools | High | DNS-resolve and reject loopback/private/link-local/metadata; check on each redirect. Opt-in flag. | M | [x] Fixed |
| HIGH-3 | Sitemap SSRF & recursion | High | Reuse SSRF guard; scope child sitemaps to parent domain; cap recursion depth and fan-out. | M | [x] Fixed |
| MED-1 | Gzip bomb | Medium | Stream-decompress with output cap (50 MB) + input cap (10 MB). | S | [x] Fixed |
| MED-2 | Browser TLS bypass | Medium | Flip `ignore_https_errors` default to False; expose `insecure` opt-in. | S | [x] Fixed |
| MED-3 | Cross-origin cred leakage | Medium | Scope cookies/auth headers to start_url's registrable domain by default; strip on redirect. | M | [x] Fixed |
| MED-4 | ReDoS via regex inputs | Medium | Use `re2`, or a regex timeout, or reject patterns failing a complexity heuristic. | M | [x] Fixed |
| MED-5 | Arbitrary Playwright actions | Medium | Gate `actions` behind an opt-in; restrict key set; disallow with cross-domain follow. | S | [x] Fixed |
| MED-6 | Proxy creds in logs | Medium | Apply `_redact_userinfo()` at every log site touching a proxy URL. | XS | [x] Fixed |
| LOW-1 | No HTTP response size cap | Low | Stream with byte cap; reject oversized `Content-Length`. | S | [x] Fixed |
| LOW-2 | Unbounded robots Crawl-delay | Low | Clamp to `_MAX_ROBOTS_DELAY = 300`. | XS | [x] Fixed |
| LOW-3 | DB path traversal | Low | Confine `crawl_db(path=...)` to `DATA_DIR`. | XS | [x] Fixed (documentary) |
| LOW-4 | Unbounded page cache | Low | Add per-entry and aggregate byte caps. | S | [x] Fixed |
| INFO-2 | `utcnow()` deprecation | Info | Switch to `datetime.now(tz=UTC)`. | XS | [x] Fixed |
| INFO-1,3,4,5 | — | Info | Document, no remediation required. | — | [ ] By design |

Effort key: **XS** < 30 min, **S** < 2 h, **M** < 1 day.

## 7. Suggested follow-ups (not findings)

- Add a `SECURITY.md` describing the threat model above and a vulnerability-reporting channel.
- Add a CI job that runs `pip-audit` (or `uv pip audit`) on every PR.
- Consider integration tests for the SSRF and path-traversal guards once they land, using a local httpbin + tmp dir.
- Add a default-deny `allow_actions: bool = False` to MCP tools introduced after this audit.
