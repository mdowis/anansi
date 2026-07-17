# MCP server (LLM integration)

Anansi ships a **FastMCP** server that exposes all scraping capabilities as tools any LLM can call over stdio transport.

> **Windows note:** Claude Desktop and most MCP clients on Windows spawn the server with a restricted PATH that often excludes `Python313\Scripts\`, so `anansi-mcp` may not be found. Use `python -m anansi.mcp_server.server` in any config where it fails.

## Start the server

```bash
anansi-mcp
# or
python -m anansi.mcp_server.server
```

## Tools

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
| `cancel_crawl` | Permanently cancel a running or paused crawl (irreversible; distinct from `pause_crawl`) |
| `screenshot_url` | Capture a PNG screenshot of any page via headless browser; returns base64 or saves to file |
| `train_selector` | Manually teach the parser a correct CSS/XPath/text selector for a URL pattern at confidence 1.0 |
| `validate_selector` | Test CSS selectors against a live page without affecting stored confidence scores |
| `clear_cache` | Invalidate the in-memory page cache (all entries, or a single URL) |

## `fetch_url` parameters

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
| `impersonate` | `null` | curl-cffi TLS/HTTP-2 fingerprint target (e.g. `"chrome124"`); falls back to `ANANSI_IMPERSONATE` env var; per-request, overrides the instance default |
| `bot_profile` | `null` | Present as a known crawler: `"googlebot"` or `"googlebot-mobile"`. Pins the crawler User-Agent + minimal headers (and, in a crawl, evaluates robots.txt as that crawler). Independent of `impersonate`. |
| `capture_network` | `false` | **Browser only.** Intercept JSON API responses the page makes during load/actions. Returns raw payloads in `captured_requests` — ideal for API-first SPAs. Bypasses cache. |
| `capture_patterns` | `null` | URL substrings to filter captured responses (e.g. `["/api/", "/graphql"]`). Max 20 entries. Requires `capture_network=true`. |

## Handling large pages

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

## `fetch_and_extract` example

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

## Browser interactions (`actions`)

Pass an `actions` list with `use_browser=true` for dynamically loaded content. Actions execute in order after page load.

| Type | Required fields | Optional fields | Description |
|---|---|---|---|
| `click` | `selector` | — | Click a CSS-matched element |
| `fill` | `selector`, `value` | — | Type text into an input |
| `press` | `selector`, `key` | — | Press a key while an element is focused |
| `scroll_to_bottom` | — | — | Scroll to the bottom of the page (single shot) |
| `scroll_until_stable` | — | `max_scrolls` (1–30, default 10), `scroll_delay` (100–5000 ms, default 1500) | Scroll repeatedly until page height stops changing — handles infinite-scroll feeds, product listings, and lazy-loaded content. Stops when height is stable for 2 consecutive checks, or when the 60 s action budget is hit. |
| `wait` | `ms` | — | Pause for N milliseconds |
| `wait_for_selector` | `selector` | — | Wait until a CSS selector appears in the DOM |

```
# Infinite scroll — load all items automatically
fetch_url(url="https://example.com/feed", use_browser=true, actions=[
    {"type": "scroll_until_stable", "max_scrolls": 15, "scroll_delay": 1500},
])

# Submit a search form
fetch_url(url="https://example.com/search", use_browser=true, format="text", actions=[
    {"type": "fill", "selector": "input[name=q]", "value": "web scraping"},
    {"type": "press", "selector": "input[name=q]", "key": "Enter"},
    {"type": "wait_for_selector", "selector": ".results"},
])
```

## Network request interception (`capture_network`)

Many modern sites (React, Next.js, Vue, Nuxt) render a minimal HTML shell and load all actual data via XHR/fetch API calls. `capture_network=true` registers a response listener _before_ navigation and collects every JSON API response the page makes — bypassing HTML parsing entirely.

```
fetch_url(
    url="https://shop.example.com/products",
    use_browser=true,
    capture_network=true,
    capture_patterns=["/api/products", "/graphql"],
    actions=[{"type": "scroll_until_stable"}],
)
# → {
#     "url": "https://...", "status": 200, "via_browser": true,
#     "captured_requests": [
#       {"url": "https://shop.example.com/api/products?page=1", "status": 200,
#        "body": {"items": [...], "total": 240}},
#       ...
#     ],
#     "content": "...",   # HTML shell (often minimal)
#   }
```

- Capped at 50 responses, 200 KB each (larger responses are silently skipped)
- `capture_patterns` filters by URL substring; omit to capture all JSON responses
- Results bypass the page cache (each call re-fetches and re-intercepts)

## Client configuration

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
pip install "anansi-scraper[openai] @ git+https://github.com/mdowis/anansi"
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

See [`examples/05_mcp_chatgpt_usage.py`](../examples/05_mcp_chatgpt_usage.py) for a runnable end-to-end example.
