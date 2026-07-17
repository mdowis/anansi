# Architecture

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
    └── server.py        # FastMCP server — 17 LLM-callable tools
```
