# Anansi Anti-Scraping Capability Upgrade Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Upgrade Anansi’s anti-scraping effectiveness by adding coherent personas, sticky browser sessions, Cloudflare HTTP-response escalation, vendor-aware protection classification, smarter proxy scoring, and a first-class CAPTCHA/human-loop interface.

**Architecture:** Keep the current layered fetch model (HTTP → impersonated HTTP → browser) but make it stateful and vendor-aware. Introduce a shared `Persona` model and `ProtectionDetection` classifier used by HTTP fetcher, browser fetcher, crawler escalation, and proxy scoring so decisions are consistent across layers.

**Tech Stack:** Python 3.11, httpx, curl-cffi, Playwright, pytest, pytest-asyncio, respx, existing Anansi crawler/fetcher modules.

---

## Current context / assumptions

- Repository root: `/Users/nullscribe/tmp-review/anansi`
- Main fetcher modules:
  - `anansi/fetchers/http.py`
  - `anansi/fetchers/browser.py`
  - `anansi/fetchers/smart.py`
  - `anansi/fetchers/escalate.py`
  - `anansi/spider/crawler.py`
  - `anansi/proxy/manager.py`
- Current anti-bot strengths already present:
  - TLS impersonation in `HTTPFetcher`
  - Cloudflare wait/click flow in `BrowserFetcher`
  - Akamai escalation in crawler/fetch ladder
  - per-host HTTP cookie continuity and origin warm-up
  - browser network capture
- Verified current baseline:
  - `pytest tests/test_http_fetcher.py tests/test_browser_fetcher.py tests/test_cloudflare_bypass.py tests/test_robustness_improvements.py tests/test_bot_profiles.py -q`
  - expected current baseline: `56 passed`

## Scope boundaries

This plan intentionally does **not** include:
- implementing a third-party CAPTCHA solving provider in the first pass
- trying to defeat every advanced vendor at parity with anti-detect browsers
- building a full public-suffix-list dependency unless current heuristics prove insufficient
- introducing invasive product/API changes before internal abstractions settle

## Success criteria

1. HTTP and browser fetches can share a coherent persona model.
2. Browser fingerprint surfaces are internally consistent (UA/platform/viewport/screen/locale/timezone/etc.).
3. The crawler can classify Cloudflare challenge pages from HTTP responses and escalate immediately.
4. Browser sessions can persist per domain/proxy/persona instead of always resetting.
5. Protection detection supports at least Cloudflare, Akamai, and DataDome as explicit classes.
6. Proxy selection prefers historically successful proxies for a target/vendor, not just live ones.
7. CAPTCHA challenges can be surfaced through a clean internal interface, even if only manual/no-op initially.
8. Existing tests still pass and new tests cover the new behavior.

---

# Proposed approach

Build this in six phases:

1. **Shared abstractions** — persona and protection-detection models.
2. **HTTP path upgrades** — coherent persona use and Cloudflare response detection.
3. **Browser path upgrades** — sticky sessions and fingerprint consistency.
4. **Crawler escalation upgrades** — vendor-aware ladder and session handoff.
5. **Proxy intelligence** — target-aware proxy scoring.
6. **CAPTCHA interface** — explicit hooks without hard-wiring a provider.

Order matters: avoid retrofitting tests or public interfaces twice.

---

# Files likely to change

## New files
- `anansi/persona.py`
- `anansi/protection.py`
- `anansi/captcha.py`
- `tests/test_persona.py`
- `tests/test_protection_detection.py`
- `tests/test_proxy_scoring.py`
- `tests/test_captcha.py`

## Modified files
- `anansi/__init__.py`
- `anansi/fetchers/http.py`
- `anansi/fetchers/browser.py`
- `anansi/fetchers/smart.py`
- `anansi/fetchers/escalate.py`
- `anansi/spider/crawler.py`
- `anansi/proxy/manager.py`
- `anansi/mcp_server/server.py` (only if new knobs are exposed)
- `README.md`
- `tests/test_http_fetcher.py`
- `tests/test_browser_fetcher.py`
- `tests/test_cloudflare_bypass.py`
- `tests/test_robustness_improvements.py`
- `tests/test_smart.py`
- `tests/test_session_cookies.py`

---

# Step-by-step plan

## Phase 1 — Shared abstractions

### Task 1: Add persona data model

**Objective:** Create a single persona abstraction that can drive both HTTP and browser requests coherently.

**Files:**
- Create: `anansi/persona.py`
- Modify: `anansi/__init__.py`
- Test: `tests/test_persona.py`

**Implementation details:**
- Add a `Persona` dataclass with fields:
  - `user_agent`
  - `ua_family`
  - `platform`
  - `viewport`
  - `screen`
  - `locale`
  - `timezone_id`
  - `accept_language`
  - `hardware_concurrency`
  - `device_memory`
  - `webgl_vendor`
  - `webgl_renderer`
  - `max_touch_points`
  - `mobile`
- Add `build_persona(...)` helper to generate a coherent persona bundle.
- Start with a small curated allowlist of realistic personas rather than free-form randomization.
- Keep generation deterministic when a seed is provided so tests remain stable.

**Step 1: Write failing tests**
- Add tests asserting:
  - screen width/height matches viewport family expectations
  - mobile personas never produce desktop-only combos
  - locale/timezone fields are present
  - deterministic seed returns same persona twice

**Step 2: Run test to verify failure**

Run:
```bash
cd /Users/nullscribe/tmp-review/anansi
source .venv-review/bin/activate
pytest tests/test_persona.py -q
```

Expected: FAIL — module or symbols missing.

**Step 3: Write minimal implementation**
- Implement `Persona` and `build_persona`.
- Export from `anansi/__init__.py` only after tests are green.

**Step 4: Run test to verify pass**

Run:
```bash
pytest tests/test_persona.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add anansi/persona.py anansi/__init__.py tests/test_persona.py
git commit -m "feat: add coherent persona model"
```

---

### Task 2: Add protection detection model

**Objective:** Centralize vendor/challenge/block classification into one shared module.

**Files:**
- Create: `anansi/protection.py`
- Test: `tests/test_protection_detection.py`
- Modify: `anansi/__init__.py`

**Implementation details:**
- Add:
  - `ProtectionVendor` enum
  - `ProtectionKind` enum (`NONE`, `JS_SHELL`, `CHALLENGE`, `BLOCK`, `CAPTCHA`)
  - `ProtectionDetection` dataclass
- Add `detect_protection(html, status, headers, cookies=None, url=None)`.
- Support first-pass detection for:
  - Cloudflare
  - Akamai
  - DataDome
  - generic CAPTCHA indicators
- Keep `needs_browser()` in `smart.py`, but make it callable from or subordinate to the new classifier.

**Step 1: Write failing tests**
- Cases:
  - Cloudflare challenge page → `vendor=CLOUDFLARE`, `kind=CHALLENGE`
  - Cloudflare 1020 block → `kind=BLOCK`
  - Akamai edge body markers → `vendor=AKAMAI`
  - `Set-Cookie: datadome=...` or body markers → `vendor=DATADOME`
  - plain HTML page → `vendor=NONE`

**Step 2: Run test to verify failure**

Run:
```bash
pytest tests/test_protection_detection.py -q
```

Expected: FAIL.

**Step 3: Write minimal implementation**
- Keep heuristics conservative and explicit.
- Reuse existing Akamai and Cloudflare logic where possible instead of duplicating string lists.

**Step 4: Run test to verify pass**

Run:
```bash
pytest tests/test_protection_detection.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add anansi/protection.py anansi/__init__.py tests/test_protection_detection.py
git commit -m "feat: add shared protection detection"
```

---

## Phase 2 — HTTP path upgrades

### Task 3: Teach HTTPFetcher to use personas

**Objective:** Replace ad hoc UA/header rotation with coherent persona-driven headers.

**Files:**
- Modify: `anansi/fetchers/http.py`
- Test: `tests/test_http_fetcher.py`
- Test: `tests/test_persona.py`

**Implementation details:**
- Add optional constructor args:
  - `persona: Persona | None = None`
  - `persona_seed: int | None = None`
- If no bot profile is supplied, create/use a persona instead of random UA/header fragments.
- `_build_headers()` should pull from persona values.
- Keep bot profiles as an override path with pinned crawler headers.
- Avoid regressing impersonation or cookie behavior.

**Step 1: Write failing tests**
- Assert `Accept-Language` comes from persona.
- Assert repeated requests on same fetcher reuse same persona unless explicitly rotated.
- Assert bot profile still bypasses persona-driven browser headers.

**Step 2: Run targeted tests**

Run:
```bash
pytest tests/test_http_fetcher.py -q
```

Expected: FAIL on new assertions.

**Step 3: Implement minimal changes**
- Inject persona at init.
- Remove direct dependence on `_USER_AGENTS` for the normal path, or narrow it to persona catalog generation only.

**Step 4: Run tests**

Run:
```bash
pytest tests/test_http_fetcher.py tests/test_persona.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add anansi/fetchers/http.py tests/test_http_fetcher.py tests/test_persona.py
git commit -m "feat: drive http fetcher identity from personas"
```

---

### Task 4: Add Cloudflare HTTP-response detection and escalation hook

**Objective:** Detect Cloudflare challenge/block pages directly from HTTP responses, not only inside browser fetches.

**Files:**
- Modify: `anansi/protection.py`
- Modify: `anansi/fetchers/escalate.py`
- Modify: `anansi/spider/crawler.py`
- Test: `tests/test_smart.py`
- Test: `tests/test_cloudflare_bypass.py`

**Implementation details:**
- Extend the escalation ladder to handle Cloudflare as a first-class case.
- Distinguish:
  - solvable challenge → escalate to browser
  - hard block → return blocked result and guidance / allow proxy rotation upstream
- Do not rely on `result.ok` for Cloudflare escalation eligibility.

**Step 1: Write failing tests**
- Add crawler-level tests where a 403/503 CF challenge response escalates to browser.
- Add tests ensuring hard-block pages do not burn full challenge timeouts when already identifiable from HTTP.

**Step 2: Run tests**

Run:
```bash
pytest tests/test_smart.py tests/test_cloudflare_bypass.py -q
```

Expected: FAIL.

**Step 3: Implement minimal changes**
- Add `detect_cloudflare_*` via shared detection layer.
- Update `_do_fetch()` in crawler to escalate before the `result.ok` JS-shell branch.

**Step 4: Run tests**

Run:
```bash
pytest tests/test_smart.py tests/test_cloudflare_bypass.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add anansi/protection.py anansi/fetchers/escalate.py anansi/spider/crawler.py tests/test_smart.py tests/test_cloudflare_bypass.py
git commit -m "feat: escalate cloudflare challenges from http responses"
```

---

## Phase 3 — Browser path upgrades

### Task 5: Replace hardcoded stealth values with persona-bound values

**Objective:** Make browser fingerprint surfaces internally consistent with the chosen persona.

**Files:**
- Modify: `anansi/fetchers/browser.py`
- Test: `tests/test_browser_fetcher.py`
- Test: `tests/test_robustness_improvements.py`

**Implementation details:**
- Refactor `_STEALTH_JS` into a template rendered from persona values.
- Drive these from persona instead of hardcoded constants:
  - `navigator.languages`
  - `navigator.hardwareConcurrency`
  - `navigator.deviceMemory`
  - `screen.width`, `screen.height`, `colorDepth`
  - WebGL vendor/renderer
  - `maxTouchPoints`
  - optionally `navigator.platform`
  - optionally `navigator.userAgentData` shim if feasible without excess fragility
- Remove the fixed `1920x1080` screen spoof.

**Step 1: Write failing tests**
- Assert rendered stealth JS includes persona-specific viewport/screen values.
- Assert different persona inputs produce different JS payloads.

**Step 2: Run tests**

Run:
```bash
pytest tests/test_browser_fetcher.py tests/test_robustness_improvements.py -q
```

Expected: FAIL.

**Step 3: Implement minimal changes**
- Add `persona` support to `BrowserFetcher`.
- Replace `_make_stealth_js(hw, mem)` with `_make_stealth_js(persona)`.

**Step 4: Run tests**

Run:
```bash
pytest tests/test_browser_fetcher.py tests/test_robustness_improvements.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add anansi/fetchers/browser.py tests/test_browser_fetcher.py tests/test_robustness_improvements.py
git commit -m "feat: bind browser stealth fingerprint to personas"
```

---

### Task 6: Make browser context creation persona-consistent

**Objective:** Ensure Playwright context options match persona instead of fixed locale/timezone/permissions.

**Files:**
- Modify: `anansi/fetchers/browser.py`
- Test: `tests/test_browser_fetcher.py`

**Implementation details:**
- Set:
  - `user_agent` from persona
  - `viewport` from persona
  - `locale` from persona
  - `timezone_id` from persona
- Remove unconditional `permissions=["geolocation"]`.
- Leave permissions empty by default unless later required by explicit action.
- Preserve bot-profile override behavior.

**Step 1: Write failing tests**
- Assert context creation receives persona locale/timezone.
- Assert geolocation permission is not always granted.

**Step 2: Run tests**

Run:
```bash
pytest tests/test_browser_fetcher.py -q
```

Expected: FAIL.

**Step 3: Implement minimal changes**
- Update `_get_context()` creation path.

**Step 4: Run tests**

Run:
```bash
pytest tests/test_browser_fetcher.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add anansi/fetchers/browser.py tests/test_browser_fetcher.py
git commit -m "feat: align browser context options with persona"
```

---

### Task 7: Introduce sticky browser sessions by domain/proxy/persona

**Objective:** Persist browser-earned state for challenge-heavy targets instead of clearing it every checkout.

**Files:**
- Modify: `anansi/fetchers/browser.py`
- Test: `tests/test_browser_fetcher.py`
- Test: `tests/test_cloudflare_bypass.py`

**Implementation details:**
- Replace the single anonymous pool with keyed pooling, e.g.:
  - `(registrable_domain, proxy_key, persona_id)`
- Add an optional fetch arg like `session_key` or `origin_key`.
- Only clear cookies/permissions for contexts reused across unrelated session keys.
- Keep `force_fresh=True` semantics for challenge retry.
- Ensure stale/flagged contexts are not returned to the pool after hard failures.

**Step 1: Write failing tests**
- Same session key reuses browser state.
- Different session key does not inherit cookies.
- `force_fresh=True` still bypasses reuse.

**Step 2: Run tests**

Run:
```bash
pytest tests/test_browser_fetcher.py tests/test_cloudflare_bypass.py -q
```

Expected: FAIL.

**Step 3: Implement minimal changes**
- Introduce a keyed pool structure.
- Preserve existing max-age and max-requests retirement logic.

**Step 4: Run tests**

Run:
```bash
pytest tests/test_browser_fetcher.py tests/test_cloudflare_bypass.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add anansi/fetchers/browser.py tests/test_browser_fetcher.py tests/test_cloudflare_bypass.py
git commit -m "feat: add sticky browser sessions for protected domains"
```

---

### Task 8: Fix Cloudflare click-loop control-flow bug

**Objective:** Remove the fragile equality check in the frame-click loop and replace it with explicit click-state tracking.

**Files:**
- Modify: `anansi/fetchers/browser.py`
- Test: `tests/test_cloudflare_bypass.py`

**Implementation details:**
- Replace `if _last_click_at == now` logic with a local `clicked = True/False` flag.
- Make loop exits obvious and testable.

**Step 1: Write failing test**
- Add a fake page/frame test proving the loop exits once a click is performed.

**Step 2: Run test**

Run:
```bash
pytest tests/test_cloudflare_bypass.py -q
```

Expected: FAIL.

**Step 3: Implement minimal fix**
- Only touch the loop control flow.

**Step 4: Run test**

Run:
```bash
pytest tests/test_cloudflare_bypass.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add anansi/fetchers/browser.py tests/test_cloudflare_bypass.py
git commit -m "fix: make cloudflare click loop exit deterministic"
```

---

## Phase 4 — Crawler and escalation upgrades

### Task 9: Thread persona and browser session keys through crawler fetch flow

**Objective:** Ensure crawler fetches on the same protected domain can use coherent, sticky identity across HTTP and browser layers.

**Files:**
- Modify: `anansi/spider/crawler.py`
- Modify: `anansi/fetchers/http.py`
- Modify: `anansi/fetchers/browser.py`
- Test: `tests/test_session_cookies.py`
- Test: `tests/test_crawl_cookie_continuity.py`

**Implementation details:**
- Add a crawler-level mapping from host/domain to persona.
- Reuse that persona for per-host HTTP fetchers and browser escalations.
- Generate browser session keys from domain + proxy + persona.
- Preserve browser→HTTP cookie handoff behavior.

**Step 1: Write failing tests**
- Escalation from HTTP to browser on same host uses same persona/session family.
- Browser-earned cookies remain useful when dropping back to HTTP.

**Step 2: Run tests**

Run:
```bash
pytest tests/test_session_cookies.py tests/test_crawl_cookie_continuity.py -q
```

Expected: FAIL.

**Step 3: Implement minimal changes**
- Add helper(s) in crawler for host persona lookup and session-key derivation.

**Step 4: Run tests**

Run:
```bash
pytest tests/test_session_cookies.py tests/test_crawl_cookie_continuity.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add anansi/spider/crawler.py anansi/fetchers/http.py anansi/fetchers/browser.py tests/test_session_cookies.py tests/test_crawl_cookie_continuity.py
git commit -m "feat: preserve coherent identity across crawler escalation paths"
```

---

### Task 10: Generalize escalation ladder to vendor-aware playbooks

**Objective:** Replace one-off Akamai logic with a shared playbook model that can branch by detected protection vendor.

**Files:**
- Modify: `anansi/fetchers/escalate.py`
- Modify: `anansi/spider/crawler.py`
- Modify: `anansi/protection.py`
- Test: `tests/test_smart.py`
- Test: `tests/test_http_fetcher.py`

**Implementation details:**
- Introduce a simple internal playbook mapping:
  - Cloudflare challenge → browser
  - Cloudflare hard block → return and let proxy layer react
  - Akamai block → impersonated retry → browser
  - DataDome challenge/block → prefer sticky residential proxy + browser, if available; otherwise return detected state clearly
- Keep the first version modest: explicit branches, no over-engineered strategy objects unless necessary.

**Step 1: Write failing tests**
- Add a DataDome detection → escalation behavior test.
- Add regression test for existing Akamai path.

**Step 2: Run tests**

Run:
```bash
pytest tests/test_smart.py tests/test_http_fetcher.py -q
```

Expected: FAIL.

**Step 3: Implement minimal changes**
- Use `ProtectionDetection` from `anansi/protection.py`.
- Avoid changing public return shapes more than necessary.

**Step 4: Run tests**

Run:
```bash
pytest tests/test_smart.py tests/test_http_fetcher.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add anansi/fetchers/escalate.py anansi/spider/crawler.py anansi/protection.py tests/test_smart.py tests/test_http_fetcher.py
git commit -m "feat: add vendor-aware escalation playbooks"
```

---

## Phase 5 — Proxy intelligence

### Task 11: Extend ProxyManager stats model for target-aware outcomes

**Objective:** Track which proxies actually work for which targets/vendors, not just whether they are alive.

**Files:**
- Modify: `anansi/proxy/manager.py`
- Test: `tests/test_proxy_scoring.py`

**Implementation details:**
- Add per-proxy metrics such as:
  - total successes/failures
  - successes by domain
  - successes by vendor
  - challenge rate
  - hard-block rate
  - last_success_at
- Add optional arguments to reporting methods, e.g.:
  - `report_success(proxy_url, domain=None, vendor=None)`
  - `report_failure(proxy_url, domain=None, vendor=None, hard_block=False)`
- Preserve backward compatibility with existing no-arg usage.

**Step 1: Write failing tests**
- Assert stats update with domain/vendor information.
- Assert old calling forms still work.

**Step 2: Run tests**

Run:
```bash
pytest tests/test_proxy_scoring.py -q
```

Expected: FAIL.

**Step 3: Implement minimal changes**
- Keep the public API compatible.
- Do not alter health-check loop behavior yet.

**Step 4: Run tests**

Run:
```bash
pytest tests/test_proxy_scoring.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add anansi/proxy/manager.py tests/test_proxy_scoring.py
git commit -m "feat: record target-aware proxy outcomes"
```

---

### Task 12: Add proxy selection scoring by domain/vendor

**Objective:** Make proxy selection prefer historically effective proxies for a given protected target.

**Files:**
- Modify: `anansi/proxy/manager.py`
- Modify: `anansi/spider/crawler.py`
- Test: `tests/test_proxy_scoring.py`

**Implementation details:**
- Add `next(domain=None, vendor=None)` selection logic.
- Score candidates using:
  - healthy status
  - domain success rate
  - vendor success rate
  - recent hard-block penalty
  - usage balancing to avoid over-burning a single good proxy
- In crawler, pass domain and detected vendor when available.

**Step 1: Write failing tests**
- Proxy with better historical score for domain/vendor should be selected over generic healthy proxy.
- Round-robin fallback remains when no history exists.

**Step 2: Run tests**

Run:
```bash
pytest tests/test_proxy_scoring.py -q
```

Expected: FAIL.

**Step 3: Implement minimal changes**
- Start with a transparent weighted score formula kept in one helper.

**Step 4: Run tests**

Run:
```bash
pytest tests/test_proxy_scoring.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add anansi/proxy/manager.py anansi/spider/crawler.py tests/test_proxy_scoring.py
git commit -m "feat: prefer historically effective proxies by target"
```

---

## Phase 6 — CAPTCHA interface

### Task 13: Add internal CAPTCHA abstraction

**Objective:** Make CAPTCHA handling explicit instead of burying vendor-specific behavior inside browser logic.

**Files:**
- Create: `anansi/captcha.py`
- Modify: `anansi/protection.py`
- Modify: `anansi/fetchers/browser.py`
- Test: `tests/test_captcha.py`

**Implementation details:**
- Add:
  - `CaptchaVendor` enum
  - `CaptchaChallenge` dataclass
  - `CaptchaResult` dataclass
  - `CaptchaSolver` protocol / base class
  - `NullCaptchaSolver`
- Support detection-only first.
- Browser fetcher should be able to surface a detected challenge to the solver interface.

**Step 1: Write failing tests**
- Generic CAPTCHA challenge detection yields a structured object.
- Null solver returns a “not solved / manual required” result without crashing.

**Step 2: Run tests**

Run:
```bash
pytest tests/test_captcha.py -q
```

Expected: FAIL.

**Step 3: Implement minimal changes**
- Keep provider integration out of scope for this task.

**Step 4: Run tests**

Run:
```bash
pytest tests/test_captcha.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add anansi/captcha.py anansi/protection.py anansi/fetchers/browser.py tests/test_captcha.py
git commit -m "feat: add captcha abstraction layer"
```

---

### Task 14: Wire CAPTCHA interface into browser fetch flow behind an opt-in hook

**Objective:** Allow future manual/provider-backed CAPTCHA solving without changing fetch architecture again.

**Files:**
- Modify: `anansi/fetchers/browser.py`
- Modify: `anansi/spider/crawler.py`
- Modify: `anansi/mcp_server/server.py` (only if exposing a config knob)
- Test: `tests/test_captcha.py`

**Implementation details:**
- Add optional `captcha_solver` to `BrowserFetcher` and crawler/browser creation sites.
- When protection detection says `kind=CAPTCHA`, invoke solver.
- If unsolved, return clearly and do not loop forever.

**Step 1: Write failing tests**
- Solver called when CAPTCHA is detected.
- Unsolved result exits gracefully.

**Step 2: Run tests**

Run:
```bash
pytest tests/test_captcha.py -q
```

Expected: FAIL.

**Step 3: Implement minimal changes**
- Keep default behavior as “no solver configured”.

**Step 4: Run tests**

Run:
```bash
pytest tests/test_captcha.py -q
```

Expected: PASS.

**Step 5: Commit**

```bash
git add anansi/fetchers/browser.py anansi/spider/crawler.py anansi/mcp_server/server.py tests/test_captcha.py
git commit -m "feat: route captcha challenges through solver interface"
```

---

## Phase 7 — Documentation and integration validation

### Task 15: Update README and usage examples

**Objective:** Document the new concepts so users understand how to use personas, sticky sessions, proxy scoring, and CAPTCHA hooks.

**Files:**
- Modify: `README.md`

**Implementation details:**
- Add short subsections for:
  - personas and identity coherence
  - vendor-aware detection/escalation
  - sticky browser sessions
  - proxy scoring behavior
  - CAPTCHA solver hook
- Be explicit about limitations:
  - UA spoofing does not equal source-IP authenticity
  - hard blocks still require better IPs or manual solve paths

**Step 1: Write docs updates**
- Keep examples concise and grounded in actual API names added above.

**Step 2: Verify README examples are consistent**
- Manually check imports and parameter names against implementation.

**Step 3: Commit**

```bash
git add README.md
git commit -m "docs: explain anti-scraping identity and escalation upgrades"
```

---

### Task 16: Run full targeted regression suite

**Objective:** Verify the upgraded behavior without regressing current anti-bot functionality.

**Files:**
- No code changes required unless regressions are found.

**Validation commands:**

```bash
cd /Users/nullscribe/tmp-review/anansi
source .venv-review/bin/activate
pytest \
  tests/test_persona.py \
  tests/test_protection_detection.py \
  tests/test_proxy_scoring.py \
  tests/test_captcha.py \
  tests/test_http_fetcher.py \
  tests/test_browser_fetcher.py \
  tests/test_cloudflare_bypass.py \
  tests/test_robustness_improvements.py \
  tests/test_smart.py \
  tests/test_session_cookies.py \
  tests/test_crawl_cookie_continuity.py \
  tests/test_bot_profiles.py -q
```

**Expected:** all pass.

**Optional broader regression:**

```bash
pytest tests -q
```

**Commit only if fixes were needed:**

```bash
git add [changed files]
git commit -m "test: fix regressions in anti-scraping upgrade"
```

---

# Testing strategy

## Unit tests to add
- `tests/test_persona.py`
- `tests/test_protection_detection.py`
- `tests/test_proxy_scoring.py`
- `tests/test_captcha.py`

## Existing tests to extend
- `tests/test_http_fetcher.py`
- `tests/test_browser_fetcher.py`
- `tests/test_cloudflare_bypass.py`
- `tests/test_robustness_improvements.py`
- `tests/test_smart.py`
- `tests/test_session_cookies.py`
- `tests/test_crawl_cookie_continuity.py`

## Highest-risk regression areas
- cookie continuity in impersonated HTTP path
- bot-profile behavior overriding persona generation
- Cloudflare stale-guard / force-fresh behavior
- browser context retirement and pooling semantics
- backward compatibility of `ProxyManager.next()` / report methods

---

# Risks / tradeoffs

## Risk 1: Overcomplicated abstraction too early
**Mitigation:** keep v1 playbooks as explicit `if/elif` branches backed by shared detection structs; only generalize further if duplication becomes painful.

## Risk 2: Persona realism introduces fragile tests
**Mitigation:** use deterministic seed support and curated persona bundles instead of unconstrained randomness.

## Risk 3: Sticky browser sessions can leak state across targets
**Mitigation:** key pooling by domain/proxy/persona and keep hard caps on age/request count; never share across unrelated session keys.

## Risk 4: DataDome support may be mostly detection, not bypass
**Mitigation:** document that clearly; detection + better proxy/session strategy is still a real improvement.

## Risk 5: CAPTCHA hook invites unsupported expectations
**Mitigation:** make the default implementation explicit: detection-only plus graceful unsolved results unless a solver is configured.

---

# Open questions

1. Should persona selection be fully internal, or exposed as a public advanced API?
2. Do you want `BrowserFetcher` sticky sessions always on, or only on detected protected domains?
3. Should proxy scoring persist to SQLite across runs, or stay in-memory for v1?
4. Should `mcp_server` expose persona/session controls now, or wait until abstractions stabilize?
5. Do you want a second-pass plan specifically for provider-backed CAPTCHA solving after this lands?

---

# Recommended implementation order if you want highest value first PRs

## PR 1 — Identity coherence foundation
- Task 1
- Task 2
- Task 3
- Task 5
- Task 6

## PR 2 — Better Cloudflare / escalation behavior
- Task 4
- Task 8
- Task 9
- Task 10

## PR 3 — Proxy intelligence
- Task 11
- Task 12

## PR 4 — CAPTCHA architecture + docs
- Task 13
- Task 14
- Task 15
- Task 16

---

# Final handoff

This plan is intentionally structured so each task is small, reviewable, and test-first. The first milestone to implement is **PR 1** because coherent identity will unlock the biggest real anti-bot improvement without depending on vendor-specific heroics.
