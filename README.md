# shein-scraper

![release](https://img.shields.io/github/v/release/2scraper/shein-scraper?sort=semver)
![tests](https://github.com/2scraper/shein-scraper/actions/workflows/tests.yml/badge.svg)
![canary](https://github.com/2scraper/shein-scraper/actions/workflows/canary.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![licence](https://img.shields.io/badge/licence-MIT-green)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)

**Scrape SHEIN product listings into clean JSON or CSV.** Give it a search
term, a category or a product URL; get back one row per product: title,
brand, current and original price, discount, rating, review count, stock
and shipping flags, image and product link.

It is built for price monitoring and catalogue research on
[us.shein.com](https://us.shein.com):

- **Real data, not scraped markup.** Listings come from the page's own
  embedded product data (`window.gbRawData`), product pages from their
  schema.org JSON-LD, so prices and ids are the site's own values.
- **Gets past SHEIN's bot check.** SHEIN's own `/risk/challenge` gate is
  handled automatically: the "I am human" checkbox, the 3x3 "select the
  images matching the icon" grid, and the "click the icons in order"
  puzzle, the last two solved through [2Captcha](https://2captcha.com).
- **Honest results.** A blocked, rate-limited or partial run says so in
  its exit code and a `.meta.json` file next to the output. A run that
  finds nothing never overwrites your last good data.
- **Three browser engines** (Playwright, Selenium, Puppeteer) running one
  shared scraping loop, a local browser or 2Captcha's **Scraping Browser
  API** over CDP, rotating proxies, and a run-to-run diff tool.

## Quick start

```bash
git clone https://github.com/2scraper/shein-scraper.git
cd shein-scraper
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-playwright.txt
playwright install chromium
cp .env.example .env        # add your keys here, never on the command line

python3 playwright_scraper.py --query "summer dress" --max-results 20
```

Results land in `shein_results.json`, with `shein_results.json.meta.json`
beside it.

**Recommended setup for regular use:** a 2Captcha Scraping Browser API
profile in `SHEIN_CDP_ENDPOINT` and a key in `TWOCAPTCHA_KEY`. SHEIN
challenges or rate-limits plain local browsers quickly; a Scraping
Browser profile that has passed the challenge once keeps its cookies and
usually scrapes without further challenges.

## Examples

```bash
# a search
python3 playwright_scraper.py --query "linen pants" --max-results 60

# a category, as it appears in shein.com's own URLs
python3 playwright_scraper.py --category "Women Jeans-c-1934.html" --format csv --out jeans.csv

# a single product page (read from its JSON-LD)
python3 playwright_scraper.py --url "https://us.shein.com/dsbayvkj-p-33704388.html"

# through the Scraping Browser API (endpoint taken from SHEIN_CDP_ENDPOINT in .env)
python3 playwright_scraper.py --query "summer dress" --max-results 20

# the same with the other engines
python3 puppeteer_scraper.py --query "summer dress"
python3 selenium_scraper.py  --query "summer dress"      # local browser only, see "Engines"

# compare two runs of the same search
python3 diff_runs.py monday.json tuesday.json
```

## Sample output

A real row from a run on 2026-09-30 (`sample_output.json` has three):

```json
{
  "sku": "458057728",
  "source": "shein.com",
  "category": "Women Mini Dresses",
  "title": "Aloruh Women's Solid Color Sleeveless Mini Dress, Suitable For Beach Vacation,Dresses For Women Summer",
  "brand": "Aloruh",
  "price": 13.03,
  "currency": "USD",
  "price_source": "embedded_json",
  "product_url": "https://us.shein.com/Aloruh-Women-s-Solid-Color-Sleeveless-Mini-Dress-Suitable-For-Beach-Vacation-Dresses-For-Women-Summer-p-458057728.html",
  "image_url": "https://img.ltwebstatic.com/v4/j/pi/2026/05/02/f8/17776852344d34295349aad8f43f4fbd32e5a672d8_thumbnail_405x552.jpg",
  "scraped_at": "2026-09-30T12:56:49Z",
  "original_price": 20.89,
  "discount_pct": 38.0,
  "rating": 4.62,
  "review_count": 1001,
  "store_code": "4534970445",
  "in_stock": true,
  "is_clearance": false,
  "quickship": false
}
```

| Field | Meaning |
|---|---|
| `sku` | SHEIN's `goods_id`, the same across a product's colours and sizes |
| `category` | SHEIN's category name for the product |
| `price` / `currency` | what the customer pays now |
| `original_price` / `discount_pct` | the "was" price and the discount; `null` when there is no discount |
| `price_source` | where the price came from: `embedded_json` (listing data), `json_ld` (product page) or `json_ld_min_variant` (lowest variant price of a product page) |
| `rating` / `review_count` | average rating and number of reviews; `rating` is `null` when there are no reviews |
| `store_code` | the seller's store id |
| `in_stock`, `is_clearance`, `quickship` | SHEIN's own flags |

## Getting past SHEIN's bot check

SHEIN sends suspicious visitors to its own risk gateway instead of the
page. The scraper recognises both gates:

- **`/risk/challenge`**, a verification step. The scraper clicks the "I
  am human" checkbox (free). If SHEIN escalates, it screenshots the
  puzzle and sends it to 2Captcha: the image grid as a `GridTask`, the
  icon sequence as a `CoordinatesTask`. It clicks the answer and repeats
  with a fresh puzzle if SHEIN refuses it (`--risk-challenge-rounds`,
  default 5).
- **`/risk/action/limit`**, a rate limit. There is nothing to solve. The
  run stops with exit 3 and `stop_reason: rate_limited`, or, with
  `--rate-limit-cooldown 300`, waits and retries once.

What to expect, measured on 2026-09-30 with fresh US Scraping Browser
profiles: **3 of 5 profiles passed the challenge**, each on its third
round. SHEIN usually refuses the first correct answer or two. The other
two profiles were refused on every answer, including correct ones: SHEIN
had flagged the profile itself, and only a different profile helps. The
run then exits 3 rather than returning bad data.

Paid solves are capped per run with `--max-solves` (default 8, `0` never
pays). The count actually spent is recorded as `solves_spent` in the
`.meta.json`.

## Run results and exit codes

Every run writes `<out>` and `<out>.meta.json` (status, `stop_reason`,
product count, `total_results` reported by SHEIN, whether `--max-results`
capped it, solves spent, and a hash of the output file). A run that
collects nothing writes neither, so it never replaces your previous good
file.

| Exit | Meaning |
|---|---|
| `0` | complete |
| `6` | partial: rows were written, but the run did not finish cleanly — `stop_reason` says why (`blocked`, `rate_limited`, `remote_api_error`, `failed_pages`, `rejected_rows`) |
| `3` | blocked or rate-limited, no rows |
| `4` | the page loaded but had no products |
| `5` | a remote service failed (Scraping Browser, Scraper API), no rows |
| `2` | bad usage |
| `1` | crash (a bug; please report it) |

## Comparing runs

`diff_runs.py old.json new.json` compares two complete runs by `sku`:
products added, removed, price changed. It refuses comparisons that would
mislead: two runs of different searches, markets or `--sort`, or a
`.meta.json` that does not match its file. A currency difference is
reported as `currency_changed`, never as a price change. When a run was
capped by `--max-results`, a product missing from the new run is listed as
`left_selection` (it dropped out of the top N), not as removed.
`--fail-on-change` exits 1 on a real price change, for use in a pipeline.

## Options

Same flags for all three engines. Credentials go in `.env`
(`TWOCAPTCHA_KEY`, `SHEIN_PROXY`, `SHEIN_CDP_ENDPOINT`), never on the
command line.

| Option | Default | |
|---|---|---|
| `--query` / `--category` / `--url` | | what to scrape (`--url` wins; it can be a search, category or product URL) |
| `--max-results` | 30 | products to collect |
| `--format` / `--out` | json / `shein_results.<format>` | output format and path |
| `--max-scrolls` / `--stall-rounds` / `--scroll-delay` | 20 / 4 / 1.5s | how far to scroll a listing, and when to stop if nothing new appears |
| `--cdp-endpoint` | `SHEIN_CDP_ENDPOINT` | connect to a Scraping Browser API profile instead of launching a browser |
| `--proxy` / `--proxy-file` / `--proxy-shuffle` | `SHEIN_PROXY` | one proxy or a rotating pool (`http://login:pass@host:port` or `host:port:login:pass`) |
| `--solve-captcha` | when-blocked | `off` / `when-blocked` / `always` |
| `--risk-challenge-rounds` | 5 | puzzle rounds per SHEIN challenge (0 = don't solve) |
| `--max-solves` | 8 | paid 2Captcha solves per run (0 = never pay) |
| `--block-retries` | 2 | retries on the same session after a block with no products |
| `--rate-limit-cooldown` | 0 | seconds to wait and retry once after a rate limit (e.g. 300) |
| `--retries` / `--retry-delay` / `--delay-jitter` | 2 / 3s / 0.3 | navigation retries, and randomised waits |
| `--fingerprint` / `--fp-tags` / `--fp-country` | off | apply a 2Captcha Fingerprint API user agent (local browsers only) |
| `--scraper-api` | off | fetch through 2Captcha's Scraper API instead of a browser (no scrolling: one page per run) |
| `--scraper-api-cdp` | off | route `--scraper-api` through a Scraping Browser profile (`--scraper-api-country`, `--scraper-api-profile-id`, `--scraper-api-account-id`) |
| `--dump-html` | off | save the page HTML (and puzzle screenshots) next to the output, for debugging |
| `--allow-empty` | off | write an output file even when nothing was found |
| `--headless` / `--headful` | headless | show the browser window |
| `--sort` | relevance | recorded in the results only; not yet sent to SHEIN |

Run any engine with `--help` for the full list.

## Engines

- **Playwright** (`playwright_scraper.py`) is the recommended engine,
  with local Chromium or the Scraping Browser API.
- **Puppeteer** (`puppeteer_scraper.py`, via pyppeteer) supports the same
  two modes. pyppeteer itself is no longer maintained.
- **Selenium** (`selenium_scraper.py`) runs a local Chrome only.
  chromedriver cannot authenticate a Scraping Browser endpoint, and its
  `--proxy-server` cannot use a proxy password (the credentials are
  stripped, with a warning).

Install one engine per virtualenv (`requirements-playwright.txt`,
`requirements-selenium.txt`, `requirements-puppeteer.txt`). Their
dependencies conflict with each other.

All three engines share one scraping loop (`page_flow.py`), so they agree
on results, exit codes and when money is spent. Docker:
`docker build -t shein-scraper .` gives an image with Playwright and
Chromium.

## Known limitations

- **us.shein.com only.** Other SHEIN country sites use different
  locales and have not been tested.
- **Listings beyond the first ~20 products are unverified.** The page
  embeds its first batch of about 20 products, and every live run so far
  asked for 20 or fewer. The scraper scrolls for more and stops when
  nothing new appears, but whether scrolling actually loads further
  products has not been confirmed. The run warns, and reports `partial`,
  when it collects fewer than SHEIN says exist.
- **`--sort` is not sent to SHEIN yet.** Its URL parameter has not been
  confirmed.
- **Profiles wear out.** A Scraping Browser endpoint's credentials last
  about a day (HTTP 401 afterwards, with a clear message). After roughly
  ten runs in a row a profile may be rate-limited for a few minutes.
- **The bot check is not always solvable** — see above.

## Development

```bash
python3 smoke_test.py            # 97 offline checks, no network, no engine needed
python3 .github/ci_checks.py     # credential scan
```

CI runs the offline suite on Python 3.9 and 3.12, installs the built
wheel outside the checkout, builds the Docker image and launches Chromium
in it, and runs each engine in its own virtualenv. `TESTING.md` describes
live testing with real credentials; `CHANGELOG.md` has the history.

## Licence

MIT, see `LICENSE`.
