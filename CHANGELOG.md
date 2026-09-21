# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/) as closely as a CLI toolkit can manage. A patch
release means "fixes", not that every flag and default is frozen — a
behaviour-changing default gets called out explicitly in its entry below
rather than being a silent violation of that.

## [Unreleased]

### Added — 2026-09-21 (later still, same day), captcha solution injection into the page
- Closes the gap the GeeTest work below flagged without fixing: every
  engine's `_maybe_solve_captcha` got a solved token/solution back from
  2Captcha but only logged "Captcha solved..." — nothing ever wrote it
  into the page. Prompted by a direct follow-up question after that
  entry shipped: "ну нужно чтобы в любых случаях использование решение
  гитест было" (roughly: the GeeTest solution needs to actually get
  used/applied, not just obtained). Since the underlying gap was
  identical for every widget type this repo recognizes, not just
  GeeTest, it's closed generically for all of them.
- `captcha_solver.build_injection_script(captcha_type, solution) ->
  Optional[str]` builds (never runs) the JavaScript that writes a solved
  captcha back into the page. It stays inside this shared module's
  existing "no site knowledge" charter, reinterpreted slightly: no
  page-execution primitive (`page.evaluate` / `execute_script`) crosses
  the module's boundary, only JS *source text* — the engine, which
  already speaks its own driver's dialect, is the one that actually runs
  it. All three engines (`playwright_scraper.py` — `await
  page.evaluate(script)`; `puppeteer_scraper.py` — same, pyppeteer's
  async `page.evaluate`; `selenium_scraper.py` — synchronous
  `driver.execute_script("return " + script)`, since `execute_script`
  needs an explicit `return` to hand a value back, unlike the other two)
  now call it from their "solved" branch, wrapped in try/except so a
  failed injection degrades to a logged warning, never crashes the run.
- Each widget type gets its own STANDARD, publicly-documented
  client-integration convention — **not** anything confirmed against a
  real shein.com (or any family-site) widget capture, which has never
  happened for ANY type: Turnstile writes `cf-turnstile-response` (field
  + `data-callback`); reCAPTCHA v2 writes `g-recaptcha-response` (field +
  `data-callback`); hCaptcha writes `h-captcha-response` (field +
  `data-callback`); GeeTest v3 writes named `geetest_challenge`/
  `geetest_validate`/`geetest_seccode` fields, trying both prefixed and
  unprefixed solution keys since 2Captcha's own docs are inconsistent
  about naming; GeeTest v4 (weakest-confidence branch — no standard field
  convention documented anywhere) stashes the solution on
  `window.__2captcha_geetest_v4_solution`, dispatches a
  `CustomEvent('geetest_v4_solved', ...)`, and calls
  `window.geetest_validate_callback(...)` IF that global happens to
  exist. reCAPTCHA v3 gets no injection script at all — it's invisible,
  and the token is typically consumed the instant the SITE'S OWN
  JavaScript resolves `grecaptcha.execute()`'s promise (often straight
  into an XHR, never read back off a DOM element), so guessing at
  site-specific consumption code would violate this module's own
  no-site-knowledge charter; `build_injection_script()` returns `None`
  for it on purpose, and the raw token is still returned to the caller
  for anyone with actual site-specific knowledge to use.
- Two new permanent `smoke_test.py` checks (52/52 total, up from 50):
  one exercising `build_injection_script()`'s JS text/field names per
  widget type (including the GeeTest JSON-decode path and the
  `RECAPTCHA_V3 -> None` case), one grepping all three engine files to
  assert they actually import and call it from their "solved" branch —
  parity regression protection, same pattern as this file's existing
  "all engines check the confirmed-real /risk/challenge redirect" check.
- Discovered, not fixed, while wiring this in: `scrape_product_page()` —
  the `--url` path pointed at a single product page, in every engine —
  never calls `_maybe_solve_captcha` at all. Only `scrape_search()`'s
  scroll loop attempts a captcha solve; a `/risk/challenge` redirect hit
  while fetching a single product page is still correctly detected as
  `blocked` (the URL check runs independently) but no 2Captcha solve is
  ever attempted for it. See README "Known limitations".
- Over `--cdp-endpoint`, none of this local-injection code runs — see
  the GeeTest entry below for the CDP `Captcha.setAutoSolve` path, which
  is unaffected by this change.

### Added — 2026-09-21 (later still), GeeTest support in captcha_solver.py
- `captcha_solver.py` gained `CaptchaType.GEETEST_V3`/`GEETEST_V4`,
  detection patterns in `identify_widget()`, and a `_task_payload()` branch
  that builds the correct 2Captcha task (`GeeTestTaskProxyless`/
  `GeeTestV4TaskProxyless`, with `gt`/`challenge` or `captchaId` — no
  `websiteKey` at all, unlike every other type this module already
  supported). Prompted by shein.com's `/risk/challenge` incident
  correlating with GeeTest (three circumstantial signals, see
  `shein_parser.py`'s module docstring) and a direct question about why
  GeeTest wasn't already supported given this repo's `--cdp-endpoint` path
  solves captchas.
- Fixed a real crash this surfaced in `scraper_api_client.solve_and_wait()`:
  it only ever returned `solution.token`/`solution.gRecaptchaResponse` and
  raised `TwoCaptchaError` on anything else — correct for every widget it
  previously supported (a single opaque token), but GeeTest's solution is
  several fields together (v3: `challenge`/`validate`/`seccode`; v4:
  `captcha_id`/`lot_number`/`pass_token`/`gen_time`/`captcha_output`), so
  every successful GeeTest solve would have raised instead of returning.
  Now falls back to a JSON-encoded solution dict when there's no single
  token field, with a caller-facing docstring explaining the shape
  difference.
- Documented at the time, not fixed yet in this entry (a real,
  pre-existing, family-wide gap, not new): no engine in this family
  actually injects a solved token back into a locally-launched
  (non-`--cdp-endpoint`) page — `_maybe_solve_captcha` only logs
  "solved". This GeeTest work makes the detection/task-building plumbing
  correct up to that point; the injection step remains open for every
  widget type, not just GeeTest, and needs real captured widget markup
  (still never seen, for GeeTest or any other type, on this site) to
  implement correctly rather than guess at.
  **Closed later the same day** — see the "captcha solution injection
  into the page" entry above (it's listed first because it's newer);
  the "needs real captured widget markup to implement correctly" framing
  turned out to not be a hard blocker: each widget's own standard, public
  convention was enough to implement the injection itself, just not
  enough to CONFIRM it against shein.com's real markup, which is still
  unconfirmed and called out plainly in that entry.
- `identify_widget()`'s new GeeTest patterns are explicitly marked
  UNCONFIRMED (built from GeeTest's own public integration docs, not a
  shein.com capture) — this incident's actual challenge widget was never
  reached, only the `/risk/challenge` redirect shell. See README "Known
  limitations".

### Verified live, 2026-09-21 (later the same day) — real end-to-end parse of live data
- `shein_parser.parse_search_results()` was run for the first time against
  a genuinely fresh, real `window.gbRawData` snapshot — fetched live via
  the built-in browser-rendering tool navigating
  `https://us.shein.com/pdsearch/dress/` (no `/risk/challenge` redirect
  this time), not a hand-written fixture. All 10 of the first batch's
  products parsed cleanly (`source_used=gb_raw_data`), and the resulting
  `Product` rows round-tripped through `output_writer.finish_run()` as a
  clean `status=complete`, exit `0`, `price_confirmed_pct: 1.0` run.
  Real example row: `{"sku": "460570094", "title": "Louniche Women's
  Summer Blue And White Stripe Round Neck Waist Gathered Maxi Dress...",
  "brand": "Louniche", "price": 9.93, "original_price": 15.89,
  "discount_pct": 38.0, "rating": 4.23, "review_count": 300, "in_stock":
  true}`.
- Saved as `tests/fixtures/shein_search_live_dress_20260921.json` (a real
  capture, not synthetic — see its own `_provenance` field) with a new
  permanent `smoke_test.py` regression check
  (`parse_search_results against a REAL live capture...`) asserting all
  10 rows parse to plausible products and the `finish_run()` round-trip
  stays clean, so this doesn't silently regress.
- **Still NOT closed by this**: this exercised `shein_parser.py`'s parsing
  logic against real data, not this repo's own `playwright_scraper.py` /
  `selenium_scraper.py` / `puppeteer_scraper.py` end-to-end (browser
  launch, navigation, scroll loop, retries). Attempting that live run
  found that both this repo's cloud build environment and the linked
  device's own shell currently have network egress policies that block
  `us.shein.com` directly (`403 blocked-by-allowlist`) AND block
  downloading the Chromium binary Playwright itself needs
  (`cdn.playwright.dev` also `blocked-by-allowlist`) — so a full engine
  run needs either a network policy change or a different execution
  environment, not a code fix. See `TESTING.md` for what to try.

### Added — initial build, fifth member of the 2scraper family
- First build of `shein-scraper`, following `stockx-scraper` /
  `skyscanner-scraper` / `lidl-scraper` / `perplexity-scraper`'s established
  shape: three engine scripts (`playwright_scraper.py` primary,
  `selenium_scraper.py`, `puppeteer_scraper.py`), the same family-shared,
  no-site-knowledge modules ported near-verbatim from `lidl-scraper`
  (`output_writer.py`, `proxy_pool.py`, `captcha_solver.py`,
  `fingerprint_client.py`, `scraper_api_client.py`, `diff_runs.py`,
  `env_config.py` — per CLAUDE.md §7), and `shein_parser.py` as this repo's
  own site knowledge.
- Unlike most of this family's first builds, `shein_parser.py` is grounded
  in a real, live browser capture from the start, not a guess awaiting live
  correction: the confirmed-real search URL (`/pdsearch/{query}/`), category
  URL (`{Category}-c-{id}.html`), product URL (`{slug}-p-{goods_id}.html`),
  the primary `window.gbRawData` embedded-state data source for a listing
  page (a JS global, not JSON-LD — a search page's only JSON-LD is a plain
  `BreadcrumbList`), the product-detail page's real schema.org
  `ProductGroup` JSON-LD block, and a real, twice-confirmed bot-mitigation
  incident (`/risk/challenge?captcha_type=909&...`, SHEIN's own in-house
  risk gateway, not reproducible on an immediate retry in the same
  session). See README "Read this before trusting a run" and
  `shein_parser.py`'s own module docstring for the full write-up.
- `_extract_balanced_json_assignment()`: a brace/quote/escape-aware scanner
  (not a naive regex) for pulling `window.gbRawData = {...}` out of raw
  HTML — needed because the assignment's value can contain arbitrarily
  nested braces inside quoted string fields (e.g. a product title), which
  would truncate a lazy regex at the first unrelated `}`. A dedicated
  `smoke_test.py` check exercises this against a fixture with a
  deliberately brace-containing title.
- A new architectural pattern for this family: a single `--url` flag
  auto-routes between two different real page types and parsers —
  `_resolve_start_url()` detects a product-detail URL (`-p-{id}.html`,
  no `pdsearch` segment) and routes it to the JSON-LD single-product parser
  (`parse_product_page()`) instead of the `window.gbRawData` listing parser
  used for a search/category URL. All three engines agree on this routing;
  a `smoke_test.py` check asserts it.
- `Product` schema extended with fashion-marketplace-specific fields not
  present in any prior sibling repo: `original_price`, `discount_pct`,
  `rating`, `review_count`, `store_code`, `in_stock`, `is_clearance`,
  `quickship` — SHEIN is a multi-seller marketplace with per-listing
  rating/review counts and fulfilment flags that neither grocery items,
  flight itineraries, nor marketplace resale listings needed to represent
  the same way.
- `shein_parser.BOT_CHALLENGE_MARKERS` (`/risk/challenge`, `captcha_type=
  909`) is, unlike every prior family incident, the ONLY detection path for
  its incident — `captcha_solver.GENERIC_BOT_CHALLENGE_MARKERS` does not
  catch it on its own, since SHEIN's own gateway page carries no vendor-
  identifiable widget markup and the generic list has no "geetest" entry.
  `tests/fixtures/shein_risk_challenge_real.html` is a scrubbed real
  capture of the incident; a `smoke_test.py` check asserts the generic
  detector alone does NOT flag it, to keep this distinction from silently
  regressing.
- `robots.txt`-confirmed disallowed paths (`/user/`, `/cart/`, `/geetest/`,
  `/atomic/`, `/abt/userinfo`, and a few narrower exact paths) are refused
  outright by every engine's `_resolve_start_url()`, even for a
  human-supplied `--url` — never silently requested.

### Family-shared modules, ported unchanged in substance from lidl-scraper
- `env_config.ENV_KEYS` renamed to this repo's own `SHEIN_PROXY` /
  `SHEIN_CDP_ENDPOINT` / `SHEIN_URL` (kept in sync with `.env.example`,
  verified by a `smoke_test.py` check per CLAUDE.md §17).
- `output_writer.py`'s exit codes, `STATUS_BY_EXIT` map, and
  `finish_run()` precedence logic are byte-for-byte identical to every
  prior family member — only `Product`'s site-specific tail differs.
- `diff_runs.py`'s description string updated to name this repo; its
  sku-diff logic is unchanged and unit-tested against two real
  `finish_run()` outputs in `smoke_test.py`.

### Known open items (see README "Known limitations" / `TESTING.md`)
- No engine here has been run live against the real site yet — the research
  above comes from a browser-rendering tool, not this repo's own code. A
  first live engine run is the highest-value remaining check.
- Whether scrolling a search page past its first `window.gbRawData` batch
  (confirmed 20 products) actually grows it with new products, fires a
  separate request, or needs a different trigger is unconfirmed.
- `--sort`'s real query-parameter shape is unconfirmed and not yet wired
  into `search_url()`.
- DOM fallback selectors in `shein_parser.py` are unverified guesses,
  never needed against the real site so far.

[Unreleased]: https://github.com/2scraper/shein-scraper/compare/main...HEAD
