<img src="https://repository-images.githubusercontent.com/1238896536/d711cc76-8358-4a4a-9160-341131498877">

> *The spider that learns.*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://python.org)
[![MCP-ready](https://img.shields.io/badge/MCP-ready-8A2BE2.svg)](docs/mcp.md)

**A self-healing web scraper for hostile sites — and it's driveable by any LLM.**

Every scraper starts working. The question is how long before it breaks. Anansi is built on a
different assumption: the web is adversarial and unstable, and your scraper should handle that
without your involvement.

When a site changes its layout, Anansi finds the data anyway and **remembers the fix**. When a
page needs a browser to render, it **switches to one silently**. When bot detection gets in the
way, it presents a **coherent identity** — matched TLS fingerprint, persona, and headers — that
works to slip past detection instead of tripping it. And when you re-crawl, unchanged pages are
skipped before a request is even made. The result is a crawler that survives redesigns, handles
hostile sites, and gets better the longer it runs.

Ships with an **[MCP server](docs/mcp.md)** so any LLM can drive a full crawl through conversation.

---

## Highlights

- **Selectors that repair themselves** — CSS selectors carry confidence scores; when one breaks, four healing strategies compete and the winner is persisted. [How it works →](docs/how-it-works.md)
- **A browser only when you need one** — every response is checked for JS shells and silently retried in a stealth Playwright browser, cached per domain. [How it works →](docs/how-it-works.md)
- **A coherent anti-bot identity** — matched TLS/HTTP-2 fingerprint, persona, and headers, with vendor-aware handling of Cloudflare, Akamai, and DataDome. [Anti-bot & identity →](docs/anti-bot.md)
- **Re-crawls that skip the unchanged** — ETag, Last-Modified, content hashing, and sitemap `<lastmod>` filtering skip pages before the request goes out. [Capabilities →](docs/features.md)
- **Data you can trust** — attach a Pydantic `item_schema` and every scraped item is validated and coerced before it hits your database. [Getting started →](docs/getting-started.md)
- **Driveable by any LLM** — a FastMCP server exposes 17 tools so an agent can fetch, extract, crawl, and screenshot through conversation. [MCP server →](docs/mcp.md)

See the full [capability reference](docs/features.md) for everything else — proxy rotation, adaptive rate limiting, URL canonicalization, JS interaction, network capture, and more.

---

## Install

```bash
# Core install
pip install "git+https://github.com/mdowis/anansi"

# For browser-based fetching (Cloudflare bypass, JS rendering):
playwright install chromium

# For TLS-fingerprint mimicry (curl-cffi impersonation):
pip install "anansi-scraper[tls] @ git+https://github.com/mdowis/anansi"
```

Full install matrix, extras, and Windows notes are in [Getting started](docs/getting-started.md#install).

---

## Quickstart

```python
from pydantic import BaseModel
from anansi import Crawler
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

# Self-healing, browser auto-upgrade, adaptive rate limiting, and incremental
# re-crawls are all on by default.
crawler = Crawler(ShopSpider, concurrency=10, max_pages=1000)

async for item in crawler.run():
    print(item.data)
```

More examples — structured-data extraction, pausing and resuming, proxies, exporting — in
[Getting started](docs/getting-started.md).

---

## Drive it from any LLM

Anansi ships a [FastMCP](docs/mcp.md) server exposing 17 scraping tools over stdio or SSE, so an
LLM agent can run a full crawl through conversation:

```bash
# Register with Claude Code
claude mcp add anansi -- anansi-mcp
```

Works with Claude Code, Claude Desktop, Cursor, Windsurf, ChatGPT, LangChain, and the OpenAI
Agents SDK. Setup for each is in the [MCP server guide](docs/mcp.md).

---

## Documentation

- **[Getting started](docs/getting-started.md)** — install, first fetch and crawl, the CLI.
- **[Capabilities](docs/features.md)** — the full feature reference.
- **[How it works](docs/how-it-works.md)** — self-healing extraction, browser auto-upgrade, and adaptive rate limiting internals.
- **[Anti-bot & identity](docs/anti-bot.md)** — TLS fingerprint mimicry, coherent personas, crawler impersonation, vendor-aware escalation, sticky sessions, proxy scoring, CAPTCHA, operator controls.
- **[MCP server](docs/mcp.md)** — run the server and drive Anansi from any LLM (all client configs).
- **[Architecture](docs/architecture.md)** — package layout at a glance.

---

## Legal / Acceptable Use

Anansi is a powerful scraping tool. **You are solely responsible for how you use it.** Before
scraping any site, ensure you have the right to access and use the data and that you comply with
the site's Terms of Service, its `robots.txt`, applicable rate limits, and all relevant laws
(including computer-misuse statutes such as the CFAA and data-protection law such as GDPR/CCPA).

The anti-bot, TLS-fingerprint-impersonation, and Cloudflare-handling features are intended for
**authorized** testing, research, and scraping of content you have the right to access — not for
circumventing access controls without permission. See [`DISCLAIMER.md`](DISCLAIMER.md) for the
full statement and [Anti-bot & identity](docs/anti-bot.md#operator-controls) for operator
controls.

---

## License

Licensed under the Apache License, Version 2.0 — see [`LICENSE`](LICENSE) and
[`NOTICE`](NOTICE). Use of this software is additionally subject to the acceptable-use terms in
[`DISCLAIMER.md`](DISCLAIMER.md).
