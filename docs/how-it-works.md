# How it works

The internals behind self-healing extraction, browser auto-upgrade, and adaptive rate limiting.

## Extraction pipeline

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

## Auto browser upgrade

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

## Adaptive rate limiting

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
