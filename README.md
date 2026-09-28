# shein-scraper

![release](https://img.shields.io/github/v/release/2scraper/shein-scraper?sort=semver)
![tests](https://github.com/2scraper/shein-scraper/actions/workflows/tests.yml/badge.svg)
![canary](https://github.com/2scraper/shein-scraper/actions/workflows/canary.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![licence](https://img.shields.io/badge/licence-MIT-green)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)
![local-first](https://img.shields.io/badge/local--first-yes-success)

shein.com fashion-marketplace product scraper: a search term, a category, or
a single product URL in, a flat list of products out. Three engines
(Playwright primary, Selenium and Puppeteer/pyppeteer for parity), JSON or
CSV output, an open, documented `Product` schema. Part of the
[2scraper](https://github.com/2scraper) family — same output contract, exit
codes, and family modules as `stockx-scraper` / `skyscanner-scraper` /
`lidl-scraper` / `perplexity-scraper`.

## Read this before trusting a run

**Written 2026-09-21 from a real, live browser capture** (a real Chromium
browser navigating real shein.com pages, not a static fetch or a guess).
Unlike most of this family's first builds, nearly everything below is
CONFIRMED, not assumed:

- **Search URL, confirmed real**: `https://us.shein.com/pdsearch/{query}/`.
  No tracking query params are required — a plain URL of this shape loads
  the same results a real click-through does.
- **Category URL, confirmed real** (read off a real product page's own
  `BreadcrumbList` JSON-LD): `https://us.shein.com/{Category Name}-c-
  {numeric-id}.html`, e.g. `Women Jeans-c-1934.html`.
- **Product URL, confirmed real**: `https://us.shein.com/{slug}-p-
  {goods_id}.html`, e.g. `https://us.shein.com/dsbayvkj-p-33704388.html` —
  `goods_id` is the part that matters; `{slug}` looks decorative.
- **A search-results page carries NO `ItemList`/`Product` JSON-LD** — only
  a plain `BreadcrumbList` (confirmed live). The real, primary data source
  is a JS global embedded directly in the page's own server-rendered HTML:
  `window.gbRawData.results.bffProductsInfo.products` — a rich, confirmed-
  real array (id, name, brand, category, three price tiers, discount
  percent, rating, review count, stock/clearance/quickship flags, and more)
  with no extra request needed for the first batch. `shein_parser.py`'s
  `extract_gb_raw_data()` is this repo's primary extraction path.
- **A product-DETAIL page carries a real, clean schema.org `ProductGroup`
  JSON-LD block** (`name`/`brand`/`productGroupID`/`image[]`, plus
  `hasVariant`: one `Product` node per size with its own `offers.price`) —
  confirmed live against the example URL above. A `--url` pointed directly
  at a product page auto-routes to this parser instead of the listing one
  (see "Two URL modes" below).
- **A real bot-mitigation incident was hit and captured, twice confirmed in
  one session**: a cold browser context's first request to `/pdsearch/
  jeans/` was redirected to shein.com's own in-house risk gateway
  (`/risk/challenge?captcha_type=909&redirection=...&risk-id=...`), not a
  third-party vendor domain. **It was NOT reproducible on the very next
  request in the same session** — a second, different request rendered
  cleanly with no challenge — which reads as a per-context/fingerprint risk
  score, not a blanket block. `shein_parser.BOT_CHALLENGE_MARKERS` is the
  ONLY detection path for this incident (the generic Cloudflare/reCAPTCHA/
  hCaptcha/PerimeterX/DataDome marker list does not catch it — see "Known
  limitations").
- **Still unconfirmed**: whether scrolling a search page past its first
  server-rendered batch (~20 products) grows `window.gbRawData` in place,
  triggers a separate request this parser would need to intercept, or needs
  a different trigger entirely. `--sort`'s real query-parameter shape is
  also unconfirmed and is recorded on the run without being wired into the
  URL yet. Neither has had a live engine run against it — see `TESTING.md`.
- **The architecture — exit codes, the output contract, dedupe, credential
  redaction, CLI validation, all three engines importing cleanly, and the
  shared crash-safety wrapper around parsing (CLAUDE.md §6/§10)** — is real
  and tested, same as every other family member. `smoke_test.py` includes a
  fixture built from the real captured incident above
  (`tests/fixtures/shein_risk_challenge_real.html`), alongside synthetic
  fixtures for the confirmed-real `gbRawData`/JSON-LD shapes — see
  `smoke_test.py`'s own module docstring for which is which.

## Local-first

Like the rest of the family, this does **not** require 2Captcha's paid
Scraping Browser API to run. The default is an ordinary local headless
Chromium, no proxy, no key, no account. `--proxy` / `--cdp-endpoint` /
`--fingerprint` are opt-in power options for volume, a specific exit
country, or a consistent device identity — useful here in particular given
the confirmed, context-sensitive risk-scoring behavior above, but never
applied automatically.

## Install

Pick one engine (installing more than one into the same environment is not
supported — see "Engines" below):

```bash
pip install -r requirements-playwright.txt && playwright install chromium   # primary
pip install -r requirements-selenium.txt                                    # needs a matching chromedriver
pip install -r requirements-puppeteer.txt                                   # pyppeteer — see its own warning below
```

Copy `.env.example` to `.env` — leave it blank for a normal first run (see
"Local-first" above) and fill in what you use later. `python3 env_config.py`
shows what was picked up without ever printing a secret.

## Usage

```bash
# a product search
python3 playwright_scraper.py --query "summer dress" --format json --out shein_results.json

# a category path copied from shein.com's own navigation
python3 playwright_scraper.py --category "Women Jeans-c-1934.html"

# a single product page (auto-routes to the JSON-LD product parser)
python3 playwright_scraper.py --url "https://us.shein.com/dsbayvkj-p-33704388.html"

# a full search/category URL directly (escape hatch — bypasses --query/--category)
python3 playwright_scraper.py --url "https://us.shein.com/pdsearch/jeans/"

# with 2Captcha's Scraping Browser API (opt-in — see "Local-first" above)
python3 playwright_scraper.py --query "summer dress" --cdp-endpoint "$SHEIN_CDP_ENDPOINT"
```

`selenium_scraper.py` and `puppeteer_scraper.py` accept the identical flag
set and produce the identical output contract — see "Engines" for the two
places they genuinely can't behave the same as Playwright.

### Two URL modes

A single `--url` flag transparently supports two different real page types,
each parsed a different confirmed-real way:

- A search or category listing URL (`/pdsearch/{query}/` or
  `/{Category}-c-{id}.html`) → the `window.gbRawData` listing path, same as
  `--query`/`--category`.
- A single product-detail URL (`/{slug}-p-{goods_id}.html`) → the
  schema.org `ProductGroup` JSON-LD path, returning that one product.

`_resolve_start_url()` in each engine makes this call from the URL shape
alone (`-p-` plus `.html` and no `pdsearch` segment); a `smoke_test.py`
check asserts all three engines route the same URL the same way. A `--url`
matching a robots.txt-disallowed path (`/user/`, `/cart/`, `/geetest/`,
`/atomic/`, `/abt/userinfo`, and a few narrower exact paths) is refused
outright, never silently requested.

### Flags

`--url --query --category --sort --max-results --max-scrolls --stall-rounds
--scroll-delay --format --out --retries --retry-delay --proxy --proxy-file
--proxy-shuffle --proxy-block-retries --twocaptcha-key --captcha-api
--solve-captcha --min-score --cdp-endpoint --fingerprint --fp-tags
--fp-country --scraper-api --scraper-api-timeout --scraper-api-url
--allow-empty --dump-html --headless/--headful`

Identical across all three engines — a `smoke_test.py` check asserts the
three parsers' flag sets never drift apart. `--fingerprint`/`--fp-tags`/
`--fp-country` apply to all three engines: each sets whatever user agent
the 2Captcha Fingerprint API returns via its own driver's real primitive
(Playwright's `new_context(user_agent=...)`, pyppeteer's
`page.setUserAgent()`, Chrome's own `--user-agent=` switch under Selenium)
— see "What this repo deliberately does NOT apply from a fingerprint"
below. `--captcha-api` overrides the 2Captcha REST base URL (testing only).
`--min-score` is 2Captcha's own `minScore` field on a `RecaptchaV3Task`
request (0.3 default, matching the rest of the family). `--sort` is
accepted and recorded on the run only — see "Read this before trusting a
run" above for why it isn't wired into the URL yet.

**`--scraper-api`** (added 2026-09-22) is the odd one out: every other flag
above still launches a local or `--cdp-endpoint` browser this process
drives itself; `--scraper-api` instead sends ONE browserless HTTP call to
2Captcha's Scraper API (`scraper.2captcha.com`, a separate product from
the Scraping Browser API `--cdp-endpoint` talks to — see
`scraper_api_client.py`'s module docstring) and 2Captcha's own
infrastructure fetches and renders the page for us. `--proxy`/
`--cdp-endpoint`/`--fingerprint`/`--max-scrolls`/`--stall-rounds`/
`--scroll-delay` are all ignored in this mode (logged as a warning, not
silently dropped) — a single static fetch has no scroll loop and brings
its own exit IP/device. Live-tested against real shein.com on all three
engines, 2026-09-22: real product-page HTML does come back, and this
repo's own `/risk/challenge` interstitial shows up here too sometimes
(`--block-retries` helps the same way it does on the browser path), but
there is no documented way to pin which country/locale 2Captcha's own
infrastructure exits through — a clean, unblocked response landed on
shein.com's Netherlands storefront in every live test today, which this
repo's US/English-tuned parser correctly reports as zero products rather
than miscounting. See `CHANGELOG.md` for the full write-up, including the
untested `cdpurl` escape hatch (feeding a `--cdp-endpoint` session's CDP
URL into the Scraper API call to pin geography) left for a future pass.

### Family flags that don't apply here — and why

- **`--pages`**: a shein.com listing is treated as a single scroll-based
  results page here (see "Pagination" below), the same structural reason
  lidl-scraper omits it — `--max-scrolls`/`--stall-rounds` are this repo's
  actual equivalent.
- **`--concurrency` / `--proxy-rotate`**: same reasoning as the rest of the
  family — a single scroll-based page has no independently-addressable
  units to parallelize or rotate an exit between. `proxy_pool.ProxyPool.
  worker_view()` is still ported verbatim per the family's "copy the core,
  verbatim" rule (§7) and stays tested, for if a future feature (e.g.
  running several search terms as a batch) introduces an actual
  parallelizable unit.
- **`--zip` / `--store-id`**: shein.com is a single global marketplace
  storefront (per-country pricing via the `us.shein.com` subdomain, not a
  physical-store network like lidl-scraper's Lidl US) — there is no
  per-store selection to record.

### What this repo deliberately does NOT apply from a fingerprint

`fingerprint_client.py` only ever extracts and applies the user agent from
a 2Captcha Fingerprint API profile — never a locale or timezone, for the
same reason the rest of the family states: this session could not get a
confirmed field name for either from 2Captcha's own public reference, and a
previous family member shipped a *fabricated* locale that went unnoticed
for months. Omitting a signal honestly beats guessing it.

Credentials belong in `.env` / `SHEIN_PROXY` / `TWOCAPTCHA_KEY` — never as
literal `--proxy`/`--twocaptcha-key` text on a shared or logged command
line if you can avoid it.

## Output contract

`Product` (`output_writer.py`) — family-common columns first, fashion-
listing columns after:

```
sku, source, category, title, brand, price, currency, price_source, product_url,
image_url, scraped_at,
original_price, discount_pct, rating, review_count, store_code, in_stock,
is_clearance, quickship
```

`sku` is `goods_id` — shein.com's own numeric product identifier, confirmed
real and stable across a product's color/size variants — falling back to a
deterministic fingerprint of `product_url` only when it can't be extracted.
Note this is a DIFFERENT id scheme than `goods_sn` (a search card's own
"SKU" string) or a product-detail page's per-variant JSON-LD `sku` (one per
size); `goods_id` was chosen because it's the one identifier confirmed
stable across both a search-result card and that same product's detail-page
URL. `brand` is genuinely variable (SHEIN is a marketplace of many in-house
and third-party labels, unlike lidl-scraper's single private-label
storefront). `price` is the actual charged price (`salePrice.amount`);
`original_price`/`discount_pct` carry the "was" side, left `null` when a
card has no real discount rather than duplicating the current price.
`price_source` is `embedded_json` (from `window.gbRawData`) or `dom`
(fallback), mirroring which extraction path actually produced the row —
never a defaulted guess. `sample_output.json`/`sample_output.csv` are
clearly fictional rows (see the file headers) using the confirmed-real
field shapes above, not a live capture — no full engine run against the
live site has been made yet (see `TESTING.md`).

**Exit codes**: `0` complete · `1` crash · `2` bad usage · `3` blocked ·
`4` zero products (and nothing was written) · `5` remote API error · `6`
partial. Every completed/partial run writes a `<out>.meta.json` sidecar
with `status`, `pages_completed` (scroll rounds, here), `failed_pages` and
`price_confirmed_pct` — **except** a failed/empty/blocked/remote-API-error
run, which writes no sidecar and no output at all, so it can never
overwrite a previous good run (`--allow-empty` opts out of the "don't
write an empty result" half of that guard only — see `output_writer.
finish_run`'s docstring for the exact precedence rule and why products
being present never launders a blocked/remote-API-error run into
"complete").

## Pagination

Confirmed real: a search page's first server-rendered `window.gbRawData`
batch holds 20 products, out of a total that can run into the tens of
thousands (`results.sum`). Each engine scroll-loops in viewport-sized
steps, tracks `previous_product_count` for stall detection (per
`--stall-rounds`), and dedupes by `sku`. **Whether scrolling actually grows
`gbRawData` with genuinely new products past the first batch is
unconfirmed** (see "Read this before trusting a run") — every engine's
scroll loop is written defensively around this uncertainty rather than
assuming either answer, and the run degrades to `partial` (exit 6) if
fewer than `min(total_available, --max-results)` rows were captured, never
a plausible-looking `complete` result.

## Engines

Playwright is primary; Selenium and pyppeteer are parity copies — all three
agree on exit codes and the `Product` schema via the shared `output_writer.
finish_run()`. Real, stated limits (identical to the rest of the family's,
since these are properties of the drivers, not the site):

- **Selenium cannot use an authenticated remote CDP endpoint.**
  chromedriver's `debuggerAddress` takes a bare `host:port`; the Scraping
  Browser API's `ws://login:pass@host:port` shape needs an authenticated
  WebSocket upgrade, which only Playwright's `connect_over_cdp` and
  pyppeteer's `connect` support. `selenium_scraper.py` refuses a
  credentialed `--cdp-endpoint` outright (exit 2).
- **Selenium's `--proxy-server` cannot authenticate at all.** A `--proxy`
  with credentials has them stripped before reaching Chrome, with a loud
  warning — never a silent no-op.
- **pyppeteer is effectively unmaintained** (its own README points at
  Playwright) — shipped for parity, not as a recommendation.
- Install **exactly one** engine per environment — Playwright and pyppeteer
  declare mutually unsatisfiable `pyee` pins, and pyppeteer collides with
  Selenium's `urllib3` pin. Use a venv per engine, same as
  `.github/workflows/tests.yml`'s `engine-smoke` job.

## Known limitations

- **The `/risk/challenge` redirect is the only detection path for its own
  incident** — `shein_parser.BOT_CHALLENGE_MARKERS` (`/risk/challenge`,
  `captcha_type=909`), not `captcha_solver.GENERIC_BOT_CHALLENGE_MARKERS`.
  The generic list has no "geetest" entry, and the captured block page's
  own content is a normal-looking SHEIN shell, not vendor-identifiable
  widget markup — so unlike every other family member's incident, these
  extra markers are load-bearing, not corroboration.
- **GeeTest is a suspect, not the confirmed answer — reCAPTCHA v2 is ALSO
  real and live on this site, and there's a third, undetermined layer too**
  (re-investigated 2026-09-21, later the same day, prompted directly by
  Roman asking whether GeeTest is really it and whether other vendors
  might be in play — see `shein_parser.py`'s module docstring for the
  full write-up). A fresh live session found: (1) Google reCAPTCHA v2 is
  genuinely, concretely confirmed on shein.com — real script tags
  (`google.com/recaptcha/api.js`), a live `window.grecaptcha` v2 object,
  and a real sitekey (`window.gbCommonInfo.GOOGLE_VERIFY_SITEKEY`) — much
  stronger evidence than GeeTest's three circumstantial signals, though
  its `GOOGLE_VERIFY` naming points more at account/login anti-abuse than
  the generic search-page wall this repo scrapes. This exposed a real,
  now-fixed detection gap: the sitekey lives only in a JS config
  variable, never in the static `<div class="g-recaptcha"
  data-sitekey="...">` markup `identify_widget()` used to require —
  `captcha_solver.py` now also matches reCAPTCHA's own distinctive
  sitekey shape anywhere on the page, gated on the v2 loader being
  present (see `_RECAPTCHA_SITEKEY_ANYWHERE_RE`). (2) A third,
  previously-undocumented risk/fingerprinting layer, apparently
  proprietary and branded "Armor" (`armor.ltwebstatic.com`,
  `sc.ltwebstatic.com/.../devices/fpv2.7.js` calling `/devices/v3/
  profile/web` and `/risk/verify/identity/validation/publish/sign/rule`),
  runs on every page — obfuscated, no plaintext vendor string found in
  it, so genuinely UNDETERMINED whether it's in-house or a white-labeled
  third party. The likely shape: this layer silently scores every
  request and decides whether to show `/risk/challenge` at all, with
  whichever widget (if any) appears there as a step-up behind it — which
  still hasn't been captured live, for any vendor, despite five more
  rapid category searches in this session that didn't reproduce the
  redirect. Net: GeeTest is still the best-supported specific guess for
  `/risk/challenge` itself, but it's demonstrably not the only
  captcha-shaped thing on this site, and this repo's coverage of the one
  OTHER concretely-confirmed vendor is now solid rather than blind to it.
- **`captcha_solver.py` can now build a 2Captcha task for GeeTest** (added
  2026-09-21 — `CaptchaType.GEETEST_V3`/`GEETEST_V4`), since shein.com's
  gateway is suspected (not confirmed — see above) to use it. One real gap
  remains: the widget-detection patterns in `identify_widget()` are
  UNCONFIRMED best-effort from GeeTest's own public docs, not a real
  shein.com capture — this incident's actual challenge widget was never
  reached, only the redirect page.
- **Captcha token injection on a locally-launched browser is now
  implemented for every widget type this repo recognizes** (added
  2026-09-21, later the same day — `captcha_solver.build_injection_script()`
  plus all three engines' `_maybe_solve_captcha` calling it from their
  "solved" branch), closing a real gap the GeeTest work above had
  surfaced without closing: previously every engine got a solved
  token/solution back from 2Captcha but only logged it, never wrote it
  into the page. Read the caveat carefully before trusting this against a
  real run: each widget's injection uses that widget's own STANDARD,
  publicly-documented client-integration convention (a hidden
  `g-recaptcha-response` textarea for reCAPTCHA v2, a
  `cf-turnstile-response` field for Turnstile, named `geetest_challenge`/
  `geetest_validate`/`geetest_seccode` fields for GeeTest v3, and so on —
  see `captcha_solver.build_injection_script`'s own docstring for the
  full list) — **not** anything confirmed against a real shein.com widget
  capture, which has never happened for ANY type on this site. reCAPTCHA
  v3 has no such convention at all (it's invisible; the token is
  typically consumed the instant the site's own JS resolves
  `grecaptcha.execute()`, often straight into an XHR, never read back off
  a DOM element) so `build_injection_script()` deliberately returns
  `None` for it rather than guess at site-specific consumption code,
  which this shared module's own no-site-knowledge charter says doesn't
  belong here — a caller still gets the raw token back in that case, for
  a caller with actual site-specific knowledge to use. A failed injection
  degrades to a logged warning, never a crash.
  Over `--cdp-endpoint` (the Scraping Browser API), none of this local
  injection code runs at all — 2Captcha's own `Captcha.setAutoSolve` CDP
  domain solves AND injects entirely inside their infrastructure — but
  that extension's own confirmed widget coverage (live, 2026-09-14:
  Turnstile, Amazon WAF, Yandex SmartCaptcha, Lemin) did not include
  GeeTest in the one capture that confirmed it, so whether a real GeeTest
  challenge on shein.com gets auto-solved over CDP is itself unconfirmed,
  not just the local path.
- **Fixed 2026-09-22: `scrape_product_page()` (the `--url` path pointed at
  a single product page, in every engine) now attempts captcha solving.**
  Previously only `scrape_search()`'s scroll loop called
  `_maybe_solve_captcha` — a `/risk/challenge` redirect hit while fetching
  a single product page was detected as `blocked` (the URL check still
  ran) but no 2Captcha solve was ever attempted for it. Fixing this
  surfaced a second, more important bug that would have shipped alongside
  a naive fix: `_maybe_solve_captcha`'s "are products already present, so
  don't bother solving" check defaults to `sp.count_result_cards`, which
  is SEARCH-page-shaped and returns `0` on literally every product page —
  wiring that default straight into `scrape_product_page()` would have
  meant every normal product-page scrape either logged a false
  "unidentified widget" warning (thanks to the confirmed-real, site-wide
  reCAPTCHA v2 loader — see the sitekey finding above) or, worse, actually
  spent a real 2Captcha solve on a page that was never blocked at all,
  whenever a real sitekey also happened to be on the page. Fixed by
  passing a product-page-shaped check instead (did
  `sp.parse_product_page()` already find real data?), and — since there's
  no scroll/round loop here to pick an injected solution up naturally the
  way `scrape_search()`'s next round does — a single re-fetch of the page
  after a successful solve, so the attempt can actually change the
  outcome instead of being purely theatrical. `smoke_test.py` gained both
  a structural check (every engine's `scrape_product_page()` calls
  `_maybe_solve_captcha` AND passes its own `count_product_links`) and a
  behavioral one (proves the old default would have skipped solving on a
  genuinely blocked product page, and proves the fix correctly skips
  solving on a genuinely fine one).
- **`playwright_scraper.py` HAS now been run live against the real site**
  (Roman's own machine, 2026-09-21, not blocked by this repo's own
  execution environments the way earlier attempts were — see
  `TESTING.md`) — and it immediately found a real, new failure shape:
  `--query "summer dress"` returned exit `4` ("empty") on a
  freshly-launched, cookie-less browser context, even though the exact
  same URL returned a completely normal 20-product page seconds later
  through a browser session that already had shein.com cookies. Not
  `/risk/challenge`, not a `>=400` status, not a parsing bug —
  `--dump-html`'s capture had an empty `<title>` and zero
  `bffProductsInfo`/`pdsearch` markers anywhere in ~1.58MB, consistent
  with (not proven to be) shein.com serving a different page entirely for
  a request from a bare cookie jar. See `shein_parser.py`'s module
  docstring, "First real engine run" section, for the full write-up. This
  correctly does NOT get reported as `blocked` (there's no bot-mitigation
  marker at all — calling it that would misrepresent what happened), but
  it also wasn't diagnosable from the log alone before this: every
  engine's "zero products, not blocked" warning now logs
  `shein_parser.diagnose_unexpected_page()`'s output (the page's real
  `<title>` and whether search-page markers are present) plus the final
  URL, and — a real parity gap this exposed — Selenium and Puppeteer had
  NO such warning at all before (only Playwright did); all three do now.
  **Resolved minutes later**: a re-run with the new diagnostic logging
  gave the actual final URL — `https://us.shein.com/risk/action/limit?
  risk-id=...` — a THIRD real incident under SHEIN's own `/risk/` gateway
  family, and a much simpler explanation than the cookie-jar theory
  above: the path name and the complete absence of any captcha-shaped
  content on it point at a plain RATE LIMIT, most plausibly tripped by
  this repo's own recent testing (several rapid CLI runs, plus an
  earlier research session's many rapid browser-tool navigations) rather
  than anything cookie-related. `BOT_CHALLENGE_MARKERS` and a new
  `RISK_GATEWAY_URL_MARKERS` tuple now cover `/risk/action/limit`
  alongside `/risk/challenge` in every engine's URL check, so this
  reports as `blocked` (exit 3) going forward instead of silently
  `empty` (exit 4). `diagnose_unexpected_page()` stays in place — it's
  what surfaced the URL that made this diagnosable, and it's still
  useful for whatever next "zero products, not blocked" case isn't
  covered by a known marker yet. See `shein_parser.py`'s module
  docstring for the full write-up.
- **A fourth real block shape, confirmed 2026-09-22**: a plain HTTP `403`
  whose body is SHEIN's own genuine "outOfService" page (real
  `img.ltwebstatic.com` asset, `<title>outOfService</title>`, "System
  Updating" copy, a per-request `EVENT ID:`), captured live via `--dump-html`
  on a `--cdp-endpoint` run. Not a captcha, not `/risk/challenge`, not
  `/risk/action/limit` — but already handled correctly with no code change:
  the existing `status >= 400` check reports it as `blocked` (exit 3), same
  as the other three shapes. Treat `/risk/challenge`, `/risk/action/limit`,
  this "outOfService" 403, and any other `>=400` as one family of "SHEIN
  declined to serve this request" outcomes — which one you hit on a given
  run looks rotated/randomized, not something a fixed URL or header check
  alone predicts. See `shein_parser.py`'s module docstring for the full
  write-up.
- **Correction to the GeeTest-over-CDP coverage claim above**: the same
  `--dump-html` capture shows 2Captcha's Scraping Browser extension
  injecting `geetest/interceptor.js` AND `geetest_v4/interceptor.js` into
  every page (alongside the previously-confirmed Turnstile/Amazon WAF/
  Yandex/Lemin set, plus arkoselabs/recaptcha/keycaptcha/mt_captcha/
  captchafox). So GeeTest is at least watched-for by the extension, which
  the 2026-09-14 capture didn't happen to show — but interceptor presence
  is not the same as a confirmed solve; no page with a live GeeTest widget
  has gone through this CDP session yet. Treat CDP-side GeeTest support as
  "plausible, no longer unlisted" rather than "confirmed" or "absent."
- **Added 2026-09-22: `--block-retries` (default `2`) — retry a blocked,
  zero-product outcome on the SAME browser/proxy/CDP session before giving
  up**, instead of declaring failure on the first hit. This follows a
  measured pattern from sibling family member `etsy-scraper` (see
  https://github.com/2scraper): against its own DataDome-protected site,
  one profile was refused twice and cleared from the third attempt
  onward — "retry before you rotate." It will NOT fix the `captcha_type=909`
  incident below — that one keeps the browser on the gateway URL with no
  path forward no matter how many times the page reloads on the same
  session, confirmed on two separate real hits.
- **Fixed 2026-09-22: a stale-text false positive in the captcha-detection
  round loop.** With `TWOCAPTCHA_KEY` finally configured, a live
  `/risk/challenge?captcha_type=909` hit gave this repo's own solve path
  its first real chance to run, live — and the widget markup STILL wasn't
  captured. SHEIN's redirect target is embedded as literal text in the
  page's own SSR state (an `"originalUrl"` field — "where this session
  came from"), which can keep matching `BOT_CHALLENGE_MARKERS` on later
  scroll rounds even after the browser has moved to a completely
  different, unrelated page (confirmed live: SHEIN's own homepage — zero
  captcha widgets, zero challenge iframes, only ad-tracking pixels). This
  produced five misleading `"detected_unidentified_widget"` warnings in
  one real run. All three engines now corroborate a post-round-0 marker
  match against that round's own current URL before trusting it. See
  `shein_parser.py`'s module docstring for the full write-up.
- **Scroll-driven pagination growth is unconfirmed** — see "Pagination"
  above.
- **DOM fallback selectors are unverified guesses** (`# TODO: verify live`
  in `shein_parser.py`) — never needed against the real site so far, since
  `window.gbRawData` was present on every page captured.

## Development

```bash
python3 smoke_test.py     # or: pytest tests/test_smoke.py
```

Passes with **no** engine library installed at all (each engine guards its
driver import behind a module-level `try/except ImportError`).

**Testing against the live site**: see [`TESTING.md`](TESTING.md). A first
live Playwright run, Selenium/pyppeteer parity, scroll-pagination growth,
and the DOM fallback path remain the highest-value live checks.

## License

MIT — see `LICENSE`.
