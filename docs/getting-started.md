# Getting started

Install Anansi, run your first fetch and crawl, and use the CLI.

## Install

The distribution name is `anansi-scraper`; the import package is `anansi`. It
is installed from this Git repository (not yet published to PyPI), so the
optional extras use pip's `extras @ git+URL` syntax:

```bash
# Core install
pip install "git+https://github.com/mdowis/anansi"

# For browser-based fetching (Cloudflare bypass, JS rendering):
playwright install chromium

# With the TLS-fingerprint-mimicry extra (curl-cffi impersonation):
pip install "anansi-scraper[tls] @ git+https://github.com/mdowis/anansi"

# With the OpenAI / ChatGPT Agents SDK extra:
pip install "anansi-scraper[openai] @ git+https://github.com/mdowis/anansi"
```

Once installed, the MCP server is available as the `anansi-mcp` console script
or via `python -m anansi.mcp_server.server`, and the CLI as `anansi`.

**Windows:** `pip` is often not on PATH. Use `py -m pip install ...` instead. If `py` isn't found either, download Python from [python.org](https://python.org) and check **"Add Python to PATH"** during setup.

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

For the anti-bot features (TLS fingerprint mimicry, personas, Googlebot impersonation,
vendor-aware escalation), see [Anti-bot & identity](anti-bot.md). For the extraction,
browser-upgrade, and rate-limiting internals, see [How it works](how-it-works.md).

## CLI

```bash
# Fetch and print as markdown
anansi fetch https://example.com --output markdown

# Use browser (Cloudflare bypass, JS rendering)
anansi fetch https://protected-site.com --browser

# Fetch presenting as Googlebot
anansi fetch https://example.com --as-googlebot
anansi fetch https://example.com --bot-profile googlebot-mobile

# List all recorded crawls
anansi crawls

# Start the MCP server
anansi mcp
```

More examples in [`/examples`](../examples/).
