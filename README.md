!https://repository-images.githubusercontent.com/1238896536/d711cc76-8358-4a4a-9160-341131498877

> *The spider that learns.*

Every web scraper starts working. The question is how long before it breaks.

**Anansi is built on a different assumption: the web is adversarial and unstable, and your scraper should handle that without your involvement.**

When a site changes its layout, Anansi finds the data anyway and remembers the fix: CSS selectors are scored by confidence and healed automatically. When a page needs a browser to render, it switches to one silently. When bot detection gets in the way, it mimics Chrome's TLS fingerprint at the network level, the layer most scrapers never think about. When you re-crawl, unchanged pages are skipped before a request is even made. When extraction goes wrong, Pydantic validation catches it immediately instead of letting garbage accumulate in your database.

The result: a crawler that handles hostile sites, survives redesigns, and gets better the longer it runs. Ships with an **MCP server** so any LLM can drive a full crawl through a conversation.

---

## Capabilities

| | |
|---|---|
| **Self-healing parser** | CSS selectors are stored with confidence scores. When one breaks, four healing strategies run — fuzzy class matching, text-pattern regex, structural context, XPath fallback — and the winner is persisted for next time. |
| **Structured data extraction** | JSON-LD, Open Graph, and Microdata are extracted from every page automatically. Fields matched in schema.org markup skip CSS evaluation entirely — they're more stable and require no selector maintenance. |
| **TLS fingerprint mimicry** | Enterprise bot-detection (Cloudflare, Akamai, DataDome) fingerprints your TLS ClientHello before inspecting a single header. With `impersonate="chrome124"`, Anansi uses curl-cffi to produce a byte-for-byte copy of Chrome's TLS handshake. Install with `pip install "anansi[tls]"`. |
| **Auto browser upgrade** | Every HTTP response is checked for SPA markers, noscript redirects, and suspiciously low text density. JS shells trigger a silent retry with a stealth Playwright browser. The decision is cached per domain for the crawl session. |
| **Anti-bot & Cloudflare bypass** | The browser fetcher removes `webdriver` fingerprints, spoofs plugins and hardware concurrency, adds canvas/WebGL noise, and waits out Cloudflare Turnstile challenges automatically. |
| **Adaptive rate limiting** | A per-domain sliding window tracks error rates. A 429 immediately doubles the request gap and activates a circuit breaker. Sustained 5xx errors increase the gap further. Clean windows slowly decay back toward the base delay. |
| **Incremental crawling** | ETag, Last-Modified, and content MD5 are stored per URL. Re-crawls send conditional GET headers — 304 responses skip parsing entirely, and hash comparison catches changes even without server-side ETag support. Sitemap `<lastmod>` dates are used for a pre-flight filter that skips unchanged pages before a network request is even made. |
| **URL canonicalization** | Tracking parameters (`utm_*`, `fbclid`, `gclid`, and 25 others) are stripped before URLs enter the queue. Remaining parameters are sorted and fragments removed — so `?utm_source=twitter` and `?utm_source=facebook` are the same crawl target. |
| **Item validation** | Set `item_schema = MyPydanticModel` on a Spider and every yielded item is validated before persistence. Type coercion is automatic (`"49.99"` → `49.99`). Invalid items carry a `_validation_errors` key; valid/invalid counts and error rate appear in live crawl metrics. |
| **Concurrent crawler** | Pure asyncio, semaphore-gated workers, SQLite-backed URL queue. Crawls survive process restarts. Pause mid-run and resume days later with `Crawler.resume(crawl_id, MySpider)`. |
| **Proxy rotation** | HTTP/HTTPS/SOCKS5 with round-robin, random, or least-used strategies. Failed proxies are auto-quarantined and retested in the background. |
| **MCP server** | FastMCP server exposes 12 scraping tools — fetch, extract, crawl, pause, resume, export, metrics — so any LLM or tool-calling agent can drive a full crawl through a conversation. |

Also includes: JS interaction (click, fill, scroll, wait), robots.txt compliance, sitemap discovery, content deduplication, auth/cookie support, configurable retries with `Retry-After` support, CSV/JSON/JSONL export.

---

## Install

```bash
pip install git+https://github.com/mdowis/adaptive-web-scraper.git@claude/adaptive-scraper-framework-N7SSd

# For browser-based fetching (Cloudflare bypass, JS rendering):
playwright install chromium

# For TLS fingerprint mimicry (curl-cffi impersonation):
pip install "anansi-scraper[tls]"
```

**Windows:** `pip` is often not on PATH. Use `py -m pip install ...` instead. If `py` isn't found either, download Python from [python.org](https://python.org) and check **"Add Python to PATH"** during setup.

---

## How it works

### Extraction pipeline

```
    Per field:
         │
    ┌────▼──────────────────────────┐
    │  Structured data pre-pass     │  JSON-LD / Open Graph / Microdata
    │                               │  matched fields skip all CSS work
    └────┬──────────────────────────┘
         │ field not in structured data
    ┌────▼──────────────────────────┐
    │  Try known selectors          │  ordered by confidence score (SQLite)
    │  Try primary selector         │
    └────┬──────────────────────────┘
         │ all fail
    ┌────▼──────────────────────────┐
    │  Healing strategies           │
    │  1. Text-pattern match        │  regex on element text
    │  2. Attribute fuzzy match     │  Levenshtein-similar CSS classes
    │  3. Structural context        │  parent/sibling navigation
    │  4. XPath fallback            │  CSS→XPath conversion
    └────┬──────────────────────────┘
         │ winner (score ≥ 0.5)
    ┌────▼──────────────────────────┐
    │  Persist new selector         │  confidence stored in SQLite
    │  Success: score × 1.05 + 0.02 │  cap 1.0
    │  Failure: score × 0.85 − 0.05 │  floor 0.0
    │  Unused >7d: score × 0.99/day │
    └───────────────────────────────┘
```

### Auto browser upgrade

```
    HTTP fetch
         │
    ┌────▼──────────────────────┐
    │  Domain cached as JS?     │──Yes──► BrowserFetcher directly
    └────┬──────────────────────┘
         │ No
    ┌────▼──────────────────────┐
    │  needs_browser(html)?     │  SPA markers (React/Vue/Next/Nuxt/Angular)
    │                           │  noscript redirect · text/HTML < 3%
    └────┬──────────┬───────────┘
         │ No       │ Yes
         │          ▼
         │   BrowserFetcher retry ──► cache domain for session
         │
    ┌────▼──────────────────────┐
    │  Return HTTP result       │
    └───────────────────────────┘
```

Disable with `auto_browser=False`, or force browser on a specific request with `meta["use_browser"] = True`.

### Adaptive rate limiting

```
    After each fetch:
         │
    ├── status 429 ──────────────► gap × 2 (cap 60 s) + 30 s circuit breaker
    │
    ├── window full, error rate > 30% ──► gap × 1.5
    │
    └── window full, error rate < 5%  ──► gap × 0.95  (floor = base delay)
```

Disable with `adaptive_rate_limiting=False`.

---

## Quickstart

### Extract structured data from a product page

```python
import asyncio
from anansi import AdaptiveParser
from anansi.parser.adaptive import SelectorConfig

async def main():
    html = ...  # fetched HTML

    parser = AdaptiveParser()
    data = await parser.extract(html, {
        # JSON-LD fields like "name" and "price" are pulled from structured
        # data automatically — the CSS selectors below are only used as fallback
        "name":  SelectorConfig("h1.product-title", expected_pattern=r"\w+"),
        "price": SelectorConfig(".price-tag", expected_pattern=r"\$[\d,.]+"),
        "sku":   ".product-sku",
    }, url="https://shop.example.com/product/42")

    print(data)
    # {"name": "Widget Pro", "price": "$49.99", "sku": "WGT-001"}

    # Raw structured data is also available directly
    structured = await parser.extract_structured(html)
    print(structured["json_ld"])   # [{"@type": "Product", "name": "Widget Pro", ...}]
    print(structured["open_graph"]) # {"title": "Widget Pro", "image": "https://..."}

asyncio.run(main())
```

### Run a resilient concurrent crawl

```python
from pydantic import BaseModel
from anansi import Crawler, ProxyManager
from anansi.core import Item, Request, Response
from anansi.spider.spider import Spider

class ProductItem(BaseModel):
    title: str
    price: float        # "49.99" strings are auto-coerced
    sku: str | None = None

class ShopSpider(Spider):
    name = "shop"
    start_urls = ["https://shop.example.com/products"]
    item_schema = ProductItem   # validate every yielded item against this model

    async def parse(self, response: Response):
        for link in response.css("a.product-link"):
            yield Request(response.urljoin(link["href"]), callback="parse_product")

    async def parse_product(self, response: Response):
        yield Item({"title": response.css("h1")[0].get_text(), "url": response.url})

pm = ProxyManager(["http://proxy1:8080", "socks5://proxy2:1080"])

crawler = Crawler(
    ShopSpider,
    concurrency=10,
    delay=0.5,
    max_pages=1000,
    proxy_manager=pm,
    domain_delay=1.0,             # minimum gap between requests to same domain
    respect_robots=True,          # honour robots.txt (default True)
    cookies={"session": "..."},   # for login-protected sites
    auto_browser=True,            # detect and upgrade JS shells (default True)
    adaptive_rate_limiting=True,  # back off on errors, recover on clean runs (default True)
    conditional_get=True,         # skip unchanged pages on re-crawl (default True)
    canonicalize_urls=True,       # strip tracking params before queuing (default True)
)

async for item in crawler.run():
    print(item.data)

# Pause from another coroutine, resume later (even after process restart):
crawler.pause()
resumed = await Crawler.resume(crawler.crawl_id, ShopSpider, concurrency=10)
async for item in resumed.run():
    print(item.data)

# Export everything to CSV:
await Crawler.export_items(crawler.crawl_id, fmt="csv", path="/tmp/products.csv")
```

### TLS fingerprint mimicry

```python
from anansi.fetchers.http import HTTPFetcher

# Requires: pip install "anansi-scraper[tls]"
async with HTTPFetcher(impersonate="chrome124") as f:
    result = await f.fetch("https://bot-protected-site.com")
    print(result.html)
```

Without `[tls]` installed, Anansi logs a warning and falls back to standard httpx automatically — no code change required.

### CLI

```bash
# Fetch and print as markdown
anansi fetch https://example.com --output markdown

# Use browser (Cloudflare bypass, JS rendering)
anansi fetch https://protected-site.com --browser

# List all recorded crawls
anansi crawls

# Start the MCP server
anansi mcp
```

More examples in [`/examples`](examples/).

---

## MCP Server (LLM Integration)

Anansi ships a **FastMCP** server that exposes all scraping capabilities as tools any LLM can call over stdio transport.

> **Windows note:** Claude Desktop and most MCP clients on Windows spawn the server with a restricted PATH that often excludes `Python313\Scripts\`, so `anansi-mcp` may not be found. Use `python -m anansi.mcp_server.server` in any config where it fails.

### Start the server

```bash
anansi-mcp
# or
python -m anansi.mcp_server.server
```

### Tools

| Tool | Description |
|---|---|
| `fetch_url` | Fetch a single page — HTML, text, or markdown; supports chunking, browser mode, and browser actions |
| `fetch_urls` | Fetch multiple URLs concurrently in one call |
| `fetch_and_extract` | Fetch and extract structured fields (CSS + structured data) in one call |
| `extract` | Extract structured data from an HTML string with adaptive selectors |
| `crawl_site` | Launch a background crawl; returns a `crawl_id` immediately |
| `get_crawl_items` | Retrieve persisted items from a crawl (paginated) |
| `export_crawl` | Export items as JSONL, JSON, or CSV |
| `crawl_metrics` | Live stats: pages/sec, error rate, unchanged pages, queue depth, item validation counts |
| `pause_crawl` | Pause a running crawl |
| `resume_crawl` | Resume a paused crawl (same process) |
| `list_crawls` | List all crawls and their state |
| `selector_health` | Inspect learned selector confidence scores for a URL pattern |

### `fetch_url` parameters

| Parameter | Default | Description |
|---|---|---|
| `url` | required | The URL to fetch |
| `use_browser` | `false` | Use headless browser (bypasses Cloudflare, renders JS) |
| `proxy` | `null` | Proxy URL — `"http://user:pass@host:port"` |
| `wait_for_selector` | `null` | Wait for this CSS selector before returning (browser only) |
| `timeout` | `30.0` | Request timeout in seconds |
| `format` | `"html"` | Output format: `"html"`, `"text"`, or `"markdown"` |
| `chunk_size` | `null` | Max characters per chunk — `null` returns the full page |
| `chunk_index` | `0` | Which chunk to return (0-indexed) |
| `actions` | `null` | Browser interactions to run after page load (see below) |

### Handling large pages

Raw HTML is often 500 kB–2 MB. Three strategies, simplest to most granular:

**Switch format** — strips markup (typically 5–10× smaller):
```
fetch_url(url="https://example.com/article", format="text")
fetch_url(url="https://example.com/docs",    format="markdown")
```

**Chunk** — splits at DOM or paragraph boundaries; page is cached 5 min so subsequent chunks cost nothing:
```
fetch_url(url="https://example.com", format="markdown", chunk_size=20000, chunk_index=0)
# → {content: "...", chunk_index: 0, total_chunks: 4}
fetch_url(url="https://example.com", format="markdown", chunk_size=20000, chunk_index=1)
```

**Extract only what you need** — target specific fields with `fetch_and_extract` or `extract` and never download the full page content.

### `fetch_and_extract` example

```
fetch_and_extract(
    url="https://shop.example.com/product/1",
    selectors={"title": "h1.product-title", "price": ".price", "sku": ".sku"},
)
# → {
#     "url": "https://...", "status": 200, "elapsed": 0.42,
#     "data": {"title": "Widget Pro", "price": "$49.99", "sku": "WGT-001"},
#     "structured_data": {
#       "json_ld": [{"@type": "Product", "name": "Widget Pro", "price": "49.99"}],
#       "open_graph": {"title": "Widget Pro", "image": "https://..."},
#       "microdata": []
#     }
#   }
```

Fields matched in JSON-LD or Open Graph appear in `data` directly — CSS selectors are not evaluated for them. `structured_data` always contains the raw metadata.

### Browser interactions (`actions`)

Pass an `actions` list with `use_browser=true` for dynamically loaded content. Actions execute in order after page load.

| Type | Required fields | Description |
|---|---|---|
| `click` | `selector` | Click a CSS-matched element |
| `fill` | `selector`, `value` | Type text into an input |
| `press` | `selector`, `key` | Press a key while an element is focused |
| `scroll_to_bottom` | — | Scroll to the bottom of the page |
| `wait` | `ms` | Pause for N milliseconds |
| `wait_for_selector` | `selector` | Wait until a CSS selector appears in the DOM |

```
# Infinite scroll
fetch_url(url="https://example.com/feed", use_browser=true, actions=[
    {"type": "scroll_to_bottom"},
    {"type": "wait", "ms": 2000},
    {"type": "scroll_to_bottom"},
    {"type": "wait", "ms": 1500},
])

# Submit a search form
fetch_url(url="https://example.com/search", use_browser=true, format="text", actions=[
    {"type": "fill", "selector": "input[name=q]", "value": "web scraping"},
    {"type": "press", "selector": "input[name=q]", "key": "Enter"},
    {"type": "wait_for_selector", "selector": ".results"},
])
```

### Client configuration

**Claude Code:**
```bash
claude mcp add anansi -- anansi-mcp
```

**Claude Desktop / Cursor / Windsurf** — add to the client's MCP config file:
```json
{ "mcpServers": { "anansi": { "command": "anansi-mcp" } } }
```

**If `anansi-mcp` is not found** (common on Windows where the Scripts directory isn't on PATH):
```json
{ "mcpServers": { "anansi": { "command": "python", "args": ["-m", "anansi.mcp_server.server"] } } }
```

**Any LLM via Python:**
```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

server = StdioServerParameters(command="anansi-mcp")
async with stdio_client(server) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool("fetch_url", {"url": "https://example.com"})
```

**LangChain:**
```python
from langchain_mcp_adapters.tools import load_mcp_tools
# load_mcp_tools(session) returns standard LangChain Tool objects
```

**ChatGPT Desktop App** — open Settings → Connectors → Add MCP Server and paste:
```json
{ "command": "anansi-mcp", "args": [], "env": {} }
```

**ChatGPT / OpenAI Agents SDK (programmatic):**
```bash
pip install "anansi-scraper[openai]"
```
```python
from agents import Agent, Runner
from agents.mcp import MCPServerStdio

async with MCPServerStdio(params={"command": "anansi-mcp", "args": []}) as server:
    agent = Agent(name="Scraper", instructions="Use Anansi tools.", mcp_servers=[server])
    result = await Runner.run(agent, "Fetch https://example.com and summarise it.")
    print(result.final_output)
```

**Remote SSE transport** (for web-based ChatGPT or shared team access):
```bash
# Start Anansi as an HTTP server
anansi-mcp --transport sse --host 0.0.0.0 --port 8000
```
Then point ChatGPT Desktop (or the Agents SDK) at `http://<host>:8000/sse`:
```json
{ "url": "http://localhost:8000/sse" }
```
```python
from agents.mcp import MCPServerSse
async with MCPServerSse(params={"url": "http://localhost:8000/sse"}) as server:
    ...
```

See [`examples/05_mcp_chatgpt_usage.py`](examples/05_mcp_chatgpt_usage.py) for a runnable end-to-end example.

---

## Architecture

```
anansi/
├── core.py              # Request, Response, Item, Spider base
├── db.py                # SQLite schema (selectors.db, crawls.db, url_cache)
├── fetchers/
│   ├── base.py          # BaseFetcher, FetchResult
│   ├── http.py          # HTTPFetcher — httpx/curl-cffi, retry, UA rotation, TLS mimicry
│   ├── browser.py       # BrowserFetcher — Playwright, stealth JS, Cloudflare bypass
│   └── smart.py         # needs_browser() — JS shell detection heuristics
├── parser/
│   ├── adaptive.py      # AdaptiveParser — structured pre-pass + self-healing selectors
│   ├── strategies.py    # text_match, attribute_fuzzy, structural, xpath_fallback
│   └── structured.py    # extract_jsonld, extract_opengraph, extract_microdata
├── proxy/
│   └── manager.py       # ProxyManager — rotation, health checks, quarantine
├── sitemap.py           # SitemapEntry, iter_sitemap_entries — <lastmod> aware
├── spider/
│   ├── spider.py        # Spider base class, @rule, item_schema, sitemap filtering
│   ├── queue.py         # SQLiteQueue — URL canonicalization, persistent queue
│   └── crawler.py       # Crawler — adaptive throttle, validation, conditional GET
├── utils/
│   └── url.py           # canonicalize_url — tracking param stripping, param sort
└── mcp_server/
    └── server.py        # FastMCP server — 12 LLM-callable tools
```

---

## License

MIT
