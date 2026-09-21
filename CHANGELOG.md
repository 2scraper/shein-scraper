# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/) as closely as a CLI toolkit can manage. A patch
release means "fixes", not that every flag and default is frozen — a
behaviour-changing default gets called out explicitly in its entry below
rather than being a silent violation of that.

## [Unreleased]

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
