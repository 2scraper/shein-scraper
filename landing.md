# SHEIN Scraper by 2scraper

**Open-source fashion-marketplace product scraper for shein.com — three engines, a local browser or 2Captcha's Scraping Browser API, SHEIN's bot check solved automatically.**

Pull search, category, or single-product results — title, brand, price, discount, rating, review count, stock/clearance flags, image, product link — straight from SHEIN into JSON or CSV.

[**View source on GitHub →**](https://github.com/2scraper/shein-scraper)

---

## Before you scrape: official channels

Check SHEIN's own site and app for anything a formal integration or public feed already covers your use case. This scraper exists for everything outside that: price checks, personal shopping-research tooling, and use cases a formal partnership doesn't fit.

## What to expect

Listings are read from the page's own embedded product data (`window.gbRawData`), product pages from their schema.org JSON-LD. SHEIN's own bot check (`/risk/challenge`: an "I am human" checkbox, then an image grid or an icon puzzle) is solved automatically through 2Captcha. On 2026-09-30, 3 of 5 fresh US Scraping Browser profiles passed it; SHEIN refused the other two outright, and the run reports that (exit 3) instead of returning bad data. Listings beyond the first ~20 products are not yet verified. Details in the [README](https://github.com/2scraper/shein-scraper#readme).

## What you get

- Free, open-source scraper, one script per engine — **Playwright** (recommended), **Selenium**, and **Puppeteer** (via pyppeteer), all producing the identical output schema and exit codes
- Search by term, browse a category, or fetch a single product page — one `--url`/`--query`/`--category` flag set auto-routes to the right parser
- Reads shein.com's own confirmed-real embedded JS state first, with a schema.org JSON-LD path for single products and a DOM fallback
- Fashion-marketplace fields: original price, discount %, rating, review count, seller/store code, and clearance/quickship flags
- JSON and CSV export, with a documented `Product` schema and a `.meta.json` sidecar on every completed/partial run
- Optional 2Captcha integration, wired in but never required to get started

## 2Captcha products, when you want them

| Product | What it's for |
|---|---|
| **Captcha solving — [2captcha.com](https://2captcha.com)** | Detects a challenge, decides whether it's actually blocking you (not just present), solves it |
| **Scraping Browser API — 2captcha.com** | A remote browser session over CDP with its own proxy, fingerprint and captcha auto-solve bundled — `--cdp-endpoint` |
| **Browser fingerprints — 2captcha Fingerprint API** | Pin a specific OS/browser/country fingerprint for a locally-launched browser |
| **Proxies — 2captcha.com/proxy** (2prx.com is the same product, different name) | Drop credentials into `.env`, rotated automatically with per-exit failure tracking |

## Who this is for

Shopping-price researchers, deal-tracking tools, and anyone who wants SHEIN search/category/product results in a script rather than a browser tab. The core search and product-detail parsing is grounded in a real, live capture (see above) — a first end-to-end run of this repo's own engines and confirming scroll-pagination growth are the two pieces still a documented work in progress (see README).

## Get started

```bash
git clone https://github.com/2scraper/shein-scraper.git
cd shein-scraper
pip install -r requirements-playwright.txt && playwright install chromium
cp .env.example .env   # TWOCAPTCHA_KEY and SHEIN_CDP_ENDPOINT go here

python3 playwright_scraper.py --query "summer dress" --format json --out shein_results.json
```

Full setup, CLI reference, and configuration details in the [repository README](https://github.com/2scraper/shein-scraper#readme).

---

**Need it running at scale, with proxies, fingerprints, and captcha solving already configured?**
[Talk to us →](https://2captcha.com/contact) · Proxies by [2captcha.com/proxy](https://2captcha.com/proxy) · Scraping Browser API & captcha solving by [2captcha.com](https://2captcha.com)
