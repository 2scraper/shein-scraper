#!/usr/bin/env python3
"""shein_parser.py — this IS the shein.com site knowledge (the fashion-
marketplace-family analog of lidl-scraper's lidl_parser.py, skyscanner-
scraper's flight_parser.py, stockx-scraper's product_parser.py, and
perplexity-scraper's page_parser.py).

**Honesty note, read before trusting anything below** (CLAUDE.md §15: an
unverified site gets that stated plainly, not glossed over).

Written 2026-09-21 from a REAL, live browser capture (a browser-rendering
tool, not this repo's own engines yet — see TESTING.md for that
distinction, same one perplexity-scraper's own incident draws). Unlike
this family's usual first-build state, most of what follows is CONFIRMED,
not guessed:

  - `robots.txt` (`https://www.shein.com/robots.txt`) is permissive:
    `Disallow: /user/ /cart/ /geetest/ /atomic/ /abt/userinfo
    /aimtell-worker.js /coupon/getCouponListForOrder`, `Allow: /user/
    auth/login[/]`. Neither `/pdsearch/` (search) nor a product page nor a
    category page is disallowed. The `/geetest/` disallow is itself a
    real corroborating signal — see the bot-mitigation section below.
  - **Search URL, confirmed real**: `https://us.shein.com/pdsearch/
    {query}/` (URL-encode the query; SHEIN's own site adds tracking
    query params like `?ici=...&src_identifier=...` on a real click-
    through, but a plain `/pdsearch/{query}/` with none of those loads
    the same results — confirmed live).
  - **Category URL, confirmed real** (from a product page's own
    `BreadcrumbList` JSON-LD): `https://us.shein.com/{Category Name}-c-
    {numeric-id}.html`, e.g. `https://us.shein.com/Women Jeans-c-
    1934.html`.
  - **Product URL, confirmed real**: `https://us.shein.com/{slug}-p-
    {goods_id}.html`, e.g. `https://us.shein.com/dsbayvkj-p-33704388.html`
    — `{slug}` is a decorative, non-canonical part of the path (SHEIN's
    own server does not appear to validate it); `goods_id` is what
    matters and is this repo's chosen `sku` (see `output_writer.Product`'s
    docstring for why, and for the OTHER id schemes a search card and a
    product-detail page each separately expose).
  - **A search-results page's real, primary data source is NOT static
    HTML markup and NOT JSON-LD** (a search page's only `<script
    type="application/ld+json">` block is a plain `BreadcrumbList`, no
    `ItemList`/`Product` node at all — confirmed live, so
    `extract_json_ld()` correctly no-ops on a search page rather than
    mis-parsing the breadcrumb as a product). It's a JS global,
    `window.gbRawData.results.bffProductsInfo.products` — an array of
    rich, confirmed-real per-product objects (`goods_id`, `goods_sn`,
    `goods_name`, `goods_url_name`, `goods_img`/`detail_image[]`, `cat_id`
    /`cate_name`, `retailPrice`/`salePrice`/`discountPrice` — each an
    object with `amount`/`amountWithSymbol`/`usdAmount`/
    `usdAmountWithSymbol` — `retailDiscountPercent`,
    `premiumFlagNew.brandName`, `store_code`, `is_on_sale`,
    `soldOutStatus`, `stock`, `is_clearance`, `quickship`,
    `comment_rank_average`, `comment_num`, among ~65 other keys not all
    used here). `window.gbRawData.results.sum` /
    `...bffProductsInfo.result_count` is the query's total match count.
    This is embedded in the page's own SSR HTML as a plain
    `<script>window.gbRawData = {...};</script>` assignment (confirmed:
    the object is present and JSON-shaped the moment the page loads, no
    extra XHR needed for the FIRST batch) — see `extract_gb_raw_data()`.
  - **A product-DETAIL page carries a real, clean schema.org
    `ProductGroup` JSON-LD block**, confirmed live on the example URL
    above: top-level `name`/`description`/`url`/`brand.name`/
    `productGroupID`/`image[]`/`color`, plus `hasVariant`: an array of
    per-size `Product` nodes each with its own `sku`/`name`/`image`/
    `offers.{price,priceCurrency,availability,itemCondition}`/`size`.
    `extract_json_ld()`/`_json_ld_node_to_product()` are written for (and
    confirmed against) THIS page type — a single product, not a listing —
    so they are this parser's fallback path for a `--url` pointed
    directly at one product, not its primary path for `--query`/
    `--category`, which return many products per page.
  - **Real, live bot-mitigation incident, confirmed twice in the same
    session**: a cold, fresh browser context's FIRST request to
    `/pdsearch/jeans/` got redirected to `https://us.shein.com/risk/
    challenge?captcha_type=909&redirection=<original-url-encoded>&risk-
    id=<id>` instead of any search results — SHEIN's own in-house risk
    gateway, not a third-party vendor's own domain. `captcha_type=909`
    correlates with Geetest: the site's own global stylesheet (loaded on
    every page, challenged or not) defines `.geetest_wind.geetest_panel`
    rules, and `robots.txt` separately disallows `/geetest/` — three
    independent signals pointing at the same vendor. **Critically,
    this was NOT reproducible on every request in the same session**: a
    second, immediately-following request (a product-detail URL, then a
    DIFFERENT search query in a fresh tab) both rendered cleanly with no
    challenge at all. This reads as a per-context/fingerprint risk score
    rather than a blanket rule against every request — see TESTING.md for
    what this means for a real run of THIS repo's own engines, which is
    still unconfirmed (the capture above came from a different client).
  - **Re-investigated 2026-09-21 (later the same day), prompted by a
    direct question — "is it really GeeTest? could there be other
    captchas too?" — with real new findings, not just re-reading the
    same three signals**: a fresh built-in-browser session loaded
    `/pdsearch/dress/` and four other category searches back-to-back;
    none redirected to `/risk/challenge` this time (consistent with the
    "not reproducible on every request" note above — still no reliable
    way to trigger it on demand). But the page that DID load cleanly
    turned up two things the original capture missed:
    1. **Google reCAPTCHA v2 is ALSO confirmed live on shein.com** — a
       real, concrete finding, not circumstantial like the GeeTest signals
       below: `<script src="https://www.google.com/recaptcha/api.js">`
       plus a gstatic `recaptcha__ru.js` release script are both loaded on
       an ordinary search page; `window.grecaptcha` is a live object with
       the standard v2 method set (`render`/`execute`/`getResponse`/
       `reset`/`ready` — confirmed NOT `.enterprise`); and a real sitekey,
       `window.gbCommonInfo.GOOGLE_VERIFY_SITEKEY =
       "6LcoBR4UAAAAAIi5xU3U_q37C3nFaSckeMaT-P5j"`, sits in the page's own
       global config object rather than in any static markup. The
       `GOOGLE_VERIFY` naming suggests this is wired for an account/login
       anti-abuse flow (`/user/auth/login`, robots.txt-allowed, loaded
       with no visible widget either — it's presumably rendered
       programmatically after a suspicious attempt, not on page load) more
       than the generic search/product bot-wall this repo actually
       scrapes — but it is a real, confirmed second vendor on this site,
       already fully supported by `captcha_solver.py` (it always was;
       reCAPTCHA v2 predates the GeeTest work). The one real gap this
       exposed was in DETECTION, not solving: `identify_widget()`'s old
       reCAPTCHA v2 pattern only matched a static `<div class="g-recaptcha"
       data-sitekey="...">`, which never appears here — the sitekey only
       ever lives in that JS config var. Fixed the same day: a fallback in
       `captcha_solver.py` searches for reCAPTCHA's own fixed, distinctive
       sitekey shape (`6L` + 38 more characters, 40 total) anywhere on the
       page, gated on the v2 loader script actually being present. See
       `captcha_solver.py`'s own comment on `_RECAPTCHA_SITEKEY_ANYWHERE_RE`
       and its `smoke_test.py` regression check.
    2. **A previously-undocumented, apparently proprietary risk/
       fingerprinting layer, branded "Armor"**, loads on every ordinary
       page regardless of whether a challenge fires:
       `https://armor.ltwebstatic.com/she_dist/armor-libs/infp/
       infp.3.13.1.min.js` and a separate device-fingerprint SDK,
       `https://sc.ltwebstatic.com/she_dist/libs/devices/fpv2.7.js`, which
       calls `GET /devices/v3/profile/web?organization=
       FPNyuLhAtVnAeldjikus&smdata=<opaque>&callback=smCB_...` (JSONP) and
       a separate `POST /risk/verify/identity/validation/publish/sign/
       rule`. Both scripts are minified/obfuscated with no plaintext
       vendor name in them (checked directly — no "geetest", "shumei", or
       "recaptcha" string anywhere in `fpv2.7.js`'s ~188KB), so the
       fingerprinting vendor behind "Armor" is genuinely UNDETERMINED, not
       just unconfirmed — could be in-house, could be a white-labeled
       third party. The plausible read: this fingerprint/risk layer runs
       silently on every request and decides WHETHER to show
       `/risk/challenge` at all, with whatever widget appears there
       (GeeTest, reCAPTCHA, something else, or nothing at all if the score
       is high enough) as a step-up challenge behind it, not the first
       line of defense itself. This reframes but does not resolve the
       original question: the actual interactive widget shown inside a
       real `/risk/challenge` page is still uncaptured, for any vendor.
    Net effect on confidence: GeeTest is still the best-supported guess for
    what (if anything) `/risk/challenge` actually shows — unchanged from
    before, still three circumstantial signals, no live capture — but it
    is demonstrably not the ONLY captcha-shaped thing on this site, and
    this repo's own detector had a real, now-fixed blind spot for the one
    OTHER vendor (reCAPTCHA v2) that turned out to be concretely
    confirmed. See README "Known limitations" for the user-facing version
    of this.
  - **First real engine run, 2026-09-21 (Roman's own test), found a
    NEW failure shape this repo hadn't seen before — not blocked, not a
    parsing bug, but shein.com apparently serving a different page
    entirely**: a freshly-launched, cookie-less Playwright context's
    first request to the confirmed-correct
    `https://us.shein.com/pdsearch/summer%20dress/` came back exit 4
    ("empty") — `window.gbRawData` was `undefined` (the live JS read, not
    a text-parsing miss) and `--dump-html`'s capture had an EMPTY
    `<title>`, zero occurrences of `bffProductsInfo`/`pdsearch` anywhere
    in ~1.58MB of HTML, and what reads as generic client-side analytics
    boilerplate (`resource === 'ssr-landing-page'`,
    `pageFrom = isMarketing ? 'Marketing' : 'Home'`) — consistent with,
    but not proof of, shein.com rendering its own landing/marketing SSR
    variant instead of search results for this specific request. The
    SAME exact URL, requested through the built-in browser tool's
    session (which already carried shein.com cookies from unrelated
    earlier browsing in the same profile), returned a completely normal
    page seconds later: title "Search summer dress | SHEIN USA", 20
    products in `window.gbRawData`. No `/risk/challenge` redirect, no
    `>=400` status, no `captcha_type=909`, no `BOT_CHALLENGE_MARKERS` hit
    at all — this is NOT the bot-mitigation incident documented above,
    it is something else, and it correctly does NOT get treated as
    `blocked` by any current detector (misclassifying it as a captcha
    problem would be worse than an honest "empty"). The one real,
    non-speculative fix shipped for this: every engine's "zero products,
    not blocked" warning now logs `shein_parser.diagnose_unexpected_page()`
    — the page's actual `<title>` and whether either marker a real
    search page always has is present — plus the final URL, so the next
    occurrence doesn't need a `--dump-html` + manual grep session to
    diagnose. The MECHANISM (a consent/locale gate? a "new visitor"
    landing-page swap? something else tied to a bare cookie jar?) is
    UNCONFIRMED — worth testing next: does the SAME freshly-launched
    context succeed on a SECOND request (cookies now set from the
    first), or does every fresh launch hit this regardless of history?
  - **Still UNCONFIRMED**: whether scrolling a `/pdsearch/` page past its
    first SSR-embedded batch (confirmed 20 products per initial
    `gbRawData` snapshot, out of e.g. 17,059 total matches for one real
    query) re-fetches more into `window.gbRawData` in place, fires a
    separate XHR this parser would need to intercept instead, or is
    driven by clicking something — the DOM already contained thousands of
    (mostly skeleton/lazy) card elements on first load, which muddies a
    simple "did scrolling add real products" signal. Every engine's
    scroll loop below is written defensively around this uncertainty (see
    each engine's own module docstring) rather than assuming either
    answer.

Parsing strategy, in priority order:

  1. `extract_gb_raw_data()` — the confirmed-real embedded state, primary
     path for a search/category listing page. Works from either an
     already-evaluated dict (an engine's live `page.evaluate("() =>
     window.gbRawData")` — the more robust path, since it needs no
     brace-balancing over raw text) or from a raw HTML string via
     `_extract_balanced_json_assignment()` (a brace/quote-aware scanner,
     not a naive regex, since `window.gbRawData = {...}` is a JS
     assignment statement with arbitrarily nested braces inside quoted
     strings — a lazy regex would truncate on the first `}` inside a
     product description).
  2. `extract_json_ld()` — generic schema.org lookup (reused near-verbatim
     from lidl_parser.py — this part carries no site-specific knowledge
     at all). Finds a `ProductGroup`/`Product` node on a product-detail
     page; correctly finds nothing on a search-results page (confirmed).
  3. DOM fallback (`_parse_cards_from_dom`) — best-effort CSS selectors
     for a rendered search-result card, `# TODO: verify live`, for the
     case a future markup change removes `window.gbRawData` entirely.

All paths feed the same `Product` shape from `output_writer.py`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

from bs4 import BeautifulSoup

from output_writer import Product

log = logging.getLogger("shein_parser")

BASE_URL = "https://us.shein.com"
SOURCE = "shein.com"

SORT_VALUES = ("relevance", "new", "price_asc", "price_desc", "discount")
# SHEIN's own real sort parameter names/values are unconfirmed (no live
# capture of a sorted search was taken) — `--sort` is accepted and
# recorded, but NOT yet threaded into `search_url()` below as a query
# param, same honesty posture skyscanner-scraper used for an unconfirmed
# flag before its own live capture. See TESTING.md.

MIN_CARD_MATCHES = 2  # per family invariant (CLAUDE.md §5): a single unrelated
                       # link/card must not read as "results rendered" — mirrors
                       # lidl-scraper's / stockx-scraper's own MIN_CARD_MATCHES.

# REAL, live-captured incident (2026-09-21 — see module docstring):
# SHEIN's own risk gateway, not a third-party vendor's domain, so the
# useful markers are SHEIN's own path/query shape, not a vendor string.
# `captcha_solver.GENERIC_BOT_CHALLENGE_MARKERS` does NOT contain anything
# that would catch this on its own (no "geetest" string in that generic
# list, and this repo's own page content after the redirect is a normal-
# looking SHEIN shell page, not a page full of vendor-identifiable
# widget markup) — so unlike every prior family incident, these markers
# are NOT corroboration of the generic detector, they are the ONLY
# detection path for this specific incident. `detect_from_html()` still
# runs first per the shared contract; these are passed as `extra_markers`.
BOT_CHALLENGE_MARKERS: tuple = (
    "/risk/challenge",
    "captcha_type=909",
)

# Confirmed real from robots.txt: never requested even for a human-
# directed --url, same "warn, don't silently try-and-fail" role
# `is_disallowed_path()` plays in perplexity-scraper.
_DISALLOWED_PATH_PREFIXES = (
    "/user/",
    "/cart/",
    "/geetest/",
    "/atomic/",
    "/abt/userinfo",
    "/coupon/getCouponListForOrder",
)
_DISALLOWED_PATH_EXACT = ("/aimtell-worker.js",)


def is_disallowed_path(url_or_path: str) -> bool:
    path = url_or_path
    if path.startswith("http://") or path.startswith("https://"):
        from urllib.parse import urlparse
        path = urlparse(path).path
    if path in _DISALLOWED_PATH_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _DISALLOWED_PATH_PREFIXES)


_TITLE_RE = re.compile(r"<title[^>]*>([^<]*)</title>", re.I)


def diagnose_unexpected_page(html: str) -> str:
    """Best-effort one-line summary of what a page actually contains, for
    the "zero products found, but not flagged as blocked" warning every
    engine logs when both window.gbRawData and the DOM fallback come up
    empty on a /pdsearch/ or category URL's first render. Not a
    classifier, no verdict — just enough surface detail (the page's own
    <title>, whether either of the two markers a REAL search-results page
    always has are present at all) that a user doesn't have to reach for
    --dump-html and grep the captured page by hand to tell "this query
    genuinely has zero results" apart from "shein.com served something
    else entirely for this request."

    That second case is REAL, not hypothetical — seen live 2026-09-21,
    prompted by Roman's own test run: a freshly-launched, cookie-less
    Playwright context's very first request to a confirmed-correct
    `/pdsearch/summer%20dress/` URL got a page with NO `window.gbRawData`
    assignment anywhere (only an unrelated identifier of the same name
    inside a shared analytics bundle present on every page type) and an
    EMPTY `<title>` — while the identical URL, requested through a
    browser session that already carried shein.com cookies from earlier
    browsing in the same profile, returned a normal page (title "Search
    summer dress | SHEIN USA", 20 products in `window.gbRawData`)
    seconds later. The mechanism behind this is UNCONFIRMED — a
    consent/locale gate, a "new visitor" landing-page swap, or something
    else tied to a completely fresh cookie jar are all plausible, none
    verified — see shein_parser.py's module docstring, "Re-investigated
    2026-09-21" section that inspired this, for the fuller write-up. This
    function exists so the NEXT time this happens, whoever's looking at
    the log doesn't have to redo that archaeology from a raw HTML dump.
    """
    m = _TITLE_RE.search(html)
    title = m.group(1).strip() if m else ""
    has_search_markers = ("bffProductsInfo" in html) or ("pdsearch" in html.lower())
    return f"title={title!r}, search-page-markers-present={has_search_markers}, page-bytes={len(html)}"


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def search_url(*, query: Optional[str] = None, category_path: Optional[str] = None) -> str:
    """`category_path` is copied verbatim from a category URL's own path
    (e.g. `Women Jeans-c-1934.html`, confirmed real shape — see module
    docstring), the same "copy it from the site's own nav" convention
    lidl-scraper's `--category` uses. `query` takes priority when both
    are given, matching this family's `--url` > --query/--category
    precedence pattern generally (an engine resolves which one to use
    before calling this)."""
    if query:
        return f"{BASE_URL}/pdsearch/{quote(query)}/"
    if category_path:
        path = category_path if category_path.startswith("/") else f"/{category_path}"
        return f"{BASE_URL}{path}"
    raise ValueError("search_url() needs query or category_path")


def product_url(goods_url_name: Optional[str], goods_id: str) -> str:
    slug = goods_url_name or "product"
    return f"{BASE_URL}/{slug}-p-{goods_id}.html"


def make_sku(goods_id: Optional[str], url: str) -> str:
    if goods_id:
        return str(goods_id)
    # Fallback only — never expected on a real gbRawData-sourced row,
    # same "deterministic fingerprint of the URL, never random" rule
    # every sibling parser's own make_sku()/sku fallback follows.
    return "fp_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Path 1: window.gbRawData — confirmed-real embedded state (PRIMARY for a
# search/category listing page)
# --------------------------------------------------------------------------- #
def _extract_balanced_json_assignment(html: str, var_name: str) -> Optional[dict]:
    """Find `window.{var_name} = {...}` (or `var {var_name} = {...}`) in
    raw HTML and return the parsed object, using a brace/quote-aware
    scanner rather than a regex — the object is arbitrarily deep and its
    string values can themselves contain `{`/`}` (a product description,
    for instance), so a lazy `\\{.*?\\}` regex truncates on the first
    unrelated closing brace. Returns None if the assignment isn't found
    or doesn't parse as JSON (a JS object literal with unquoted keys
    would fail here too — not observed in the real capture this is based
    on, but handled as "not found" rather than raising, consistent with
    every other extract_*() helper in this family)."""
    pattern = re.compile(
        r"(?:window\.\s*" + re.escape(var_name) + r"|var\s+" + re.escape(var_name) + r")\s*=\s*"
    )
    m = pattern.search(html)
    if not m:
        return None
    start = html.find("{", m.end())
    if start == -1:
        return None
    depth = 0
    in_string: Optional[str] = None
    escaped = False
    i = start
    for i in range(start, len(html)):
        ch = html[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue
        if ch in ('"', "'"):
            in_string = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
    else:
        return None
    candidate = html[start : i + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        log.debug("gbRawData assignment found but did not parse as JSON: %s", exc)
        return None


def extract_gb_raw_data(html: str) -> Optional[dict]:
    return _extract_balanced_json_assignment(html, "gbRawData")


def _money(field_obj: Optional[dict]) -> Optional[float]:
    if not field_obj:
        return None
    amount = field_obj.get("amount")
    try:
        return float(amount) if amount is not None else None
    except (TypeError, ValueError):
        return None


def _bool01(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return None


def _gb_product_to_product(raw: dict, *, currency: Optional[str]) -> Optional[Product]:
    goods_id = raw.get("goods_id")
    if not goods_id:
        return None
    url = product_url(raw.get("goods_url_name"), str(goods_id))
    price = _money(raw.get("salePrice"))
    original_price = _money(raw.get("retailPrice"))
    if price is None:
        price = original_price  # not on sale — salePrice may equal retailPrice or be absent
    discount_pct = None
    try:
        if raw.get("retailDiscountPercent") not in (None, ""):
            discount_pct = float(raw["retailDiscountPercent"])
    except (TypeError, ValueError):
        pass
    rating = None
    try:
        if raw.get("comment_rank_average") not in (None, ""):
            rating = float(raw["comment_rank_average"])
    except (TypeError, ValueError):
        pass
    review_count = None
    try:
        if raw.get("comment_num") not in (None, ""):
            review_count = int(raw["comment_num"])
    except (TypeError, ValueError):
        pass
    in_stock = None
    if raw.get("soldOutStatus") is not None:
        in_stock = not bool(raw["soldOutStatus"])
    elif raw.get("stock") not in (None, ""):
        try:
            in_stock = int(raw["stock"]) > 0
        except (TypeError, ValueError):
            pass

    return Product(
        sku=make_sku(str(goods_id), url),
        source=SOURCE,
        category=raw.get("cate_name"),
        title=raw.get("goods_name"),
        brand=(raw.get("premiumFlagNew") or {}).get("brandName"),
        price=price,
        currency=currency,
        price_source="embedded_json",
        product_url=url,
        image_url=("https:" + raw["goods_img"]) if raw.get("goods_img", "").startswith("//") else raw.get("goods_img"),
        scraped_at=_now_iso(),
        original_price=original_price if original_price != price else None,
        discount_pct=discount_pct,
        rating=rating,
        review_count=review_count,
        store_code=raw.get("store_code"),
        in_stock=in_stock,
        is_clearance=_bool01(raw.get("is_clearance")),
        quickship=_bool01(raw.get("quickship")),
    )


@dataclass
class SearchResult:
    products: List[Product] = field(default_factory=list)
    total_available: Optional[int] = None
    source_used: str = "none"  # "gb_raw_data" | "json_ld" | "dom" | "none"


def parse_search_results(html: str, *, max_results: int, raw_data: Optional[dict] = None) -> SearchResult:
    """`raw_data`, when given, is an already-evaluated `window.gbRawData`
    (e.g. from an engine's live `page.evaluate()` — the more robust
    source, since it needs no brace-balancing over serialized HTML). When
    omitted, this falls back to extracting the same object from `html`
    via `extract_gb_raw_data()`. Either way this is the PRIMARY path —
    see module docstring for why a search page has no usable JSON-LD."""
    data = raw_data if raw_data is not None else extract_gb_raw_data(html)
    if data:
        try:
            results = data.get("results") or {}
            bpi = results.get("bffProductsInfo") or {}
            raw_products = bpi.get("products") or []
            currency = data.get("currency")
            total = results.get("sum")
            if total is None:
                total = bpi.get("result_count")
            products = []
            for raw in raw_products[:max_results]:
                p = _gb_product_to_product(raw, currency=currency)
                if p:
                    products.append(p)
            if products:
                return SearchResult(products=products, total_available=total, source_used="gb_raw_data")
        except (AttributeError, TypeError) as exc:
            log.warning("window.gbRawData present but did not match the expected shape: %s", exc)

    dom_products = _parse_cards_from_dom(html)[:max_results]
    if dom_products:
        return SearchResult(products=dom_products, total_available=None, source_used="dom")

    return SearchResult(products=[], total_available=None, source_used="none")


def total_result_count(html: str, *, raw_data: Optional[dict] = None) -> Optional[int]:
    data = raw_data if raw_data is not None else extract_gb_raw_data(html)
    if not data:
        return None
    results = data.get("results") or {}
    total = results.get("sum")
    if total is None:
        total = (results.get("bffProductsInfo") or {}).get("result_count")
    try:
        return int(total) if total is not None else None
    except (TypeError, ValueError):
        return None


def count_result_cards(html: str) -> int:
    """Used by captcha_solver.solve_when_blocked — cheap presence check,
    no readiness wait. Checks gbRawData first (cheap dict-length check,
    no full parse), then the DOM fallback selectors."""
    data = extract_gb_raw_data(html)
    if data:
        products = ((data.get("results") or {}).get("bffProductsInfo") or {}).get("products") or []
        if products:
            return len(products)
    return len(_first_dom_card_nodes(html))


# --------------------------------------------------------------------------- #
# Path 2: JSON-LD — generic schema.org lookup, reused near-verbatim
# (confirmed against a real product-DETAIL page — see module docstring;
# NOT the primary path for a listing, since a search page carries no
# Product/ItemList JSON-LD at all)
# --------------------------------------------------------------------------- #
def extract_json_ld(html: str) -> List[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        out.extend(data if isinstance(data, list) else [data])
    return out


def _find_product_group_node(nodes: List[dict]) -> Optional[dict]:
    for node in nodes:
        if node.get("@type") in ("ProductGroup", "Product"):
            return node
    return None


def _json_ld_node_to_product(node: dict, *, url: str) -> Optional[Product]:
    variants = node.get("hasVariant") or [node] if node.get("@type") == "ProductGroup" else [node]
    first_offer_variant = next((v for v in variants if v.get("offers")), variants[0] if variants else {})
    offer = first_offer_variant.get("offers") or {}
    price = None
    try:
        price = float(offer["price"]) if offer.get("price") not in (None, "") else None
    except (TypeError, ValueError):
        pass
    images = node.get("image") or []
    image_url = images[0] if isinstance(images, list) and images else (images if isinstance(images, str) else None)
    goods_id_match = re.search(r"-p-(\d+)\.html", url)
    goods_id = goods_id_match.group(1) if goods_id_match else None
    return Product(
        sku=make_sku(goods_id, url),
        source=SOURCE,
        category=None,  # not present on the ProductGroup node itself — see BreadcrumbList for category, unused here
        title=node.get("name"),
        brand=(node.get("brand") or {}).get("name"),
        price=price,
        currency=offer.get("priceCurrency"),
        price_source="json_ld",
        product_url=node.get("url") or url,
        image_url=image_url,
        scraped_at=_now_iso(),
    )


def parse_product_page(html: str, *, url: str) -> Optional[Product]:
    """Fallback path for a `--url` pointed directly at one product page —
    confirmed real against the example in the module docstring. NOT used
    by the search/category flow, which uses `parse_search_results()`."""
    nodes = extract_json_ld(html)
    node = _find_product_group_node(nodes)
    if node:
        return _json_ld_node_to_product(node, url=url)
    return None


# --------------------------------------------------------------------------- #
# Path 3: DOM fallback — best-effort, # TODO: verify live. Only exercised
# if a future markup change ever removes window.gbRawData entirely; the
# real capture this file is based on never needed this path.
# --------------------------------------------------------------------------- #
_CARD_SELECTORS = (".product-card", ".S-product-item", "[data-goods-id]")  # TODO: verify live
_TITLE_SELECTORS = (".product-card__title", "[class*='goods-name']")  # TODO: verify live
_PRICE_SELECTORS = (".product-card__price", "[class*='price']")  # TODO: verify live


def _first_dom_card_nodes(html: str):
    soup = BeautifulSoup(html, "html.parser")
    for sel in _CARD_SELECTORS:
        nodes = soup.select(sel)
        if len(nodes) >= MIN_CARD_MATCHES:
            return nodes
    return []


def _parse_cards_from_dom(html: str) -> List[Product]:
    nodes = _first_dom_card_nodes(html)
    products = []
    for node in nodes:
        title = _first_text(node, _TITLE_SELECTORS)
        if not title:
            continue
        link = node.select_one("a[href]")
        href = link["href"] if link else None
        url = href if (href and href.startswith("http")) else (f"{BASE_URL}{href}" if href else BASE_URL)
        price_text = _first_text(node, _PRICE_SELECTORS)
        price = _parse_price_text(price_text)
        products.append(Product(
            sku=make_sku(None, url), source=SOURCE, category=None, title=title, brand=None,
            price=price, currency=None, price_source="dom", product_url=url,
            image_url=None, scraped_at=_now_iso(),
        ))
    return products


def _first_text(soup_or_tag, selectors) -> Optional[str]:
    for sel in selectors:
        found = soup_or_tag.select_one(sel)
        if found and found.get_text(strip=True):
            return found.get_text(strip=True)
    return None


def _parse_price_text(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"[\d,]+\.?\d*", text.replace(",", ""))
    return float(m.group(0)) if m else None


# --------------------------------------------------------------------------- #
# Crash-safety wrapper — CLAUDE.md §6: an unexpected parse exception
# degrades to an empty result, never crashes the caller
# --------------------------------------------------------------------------- #
def safe_parse_search_results(html: str, *, max_results: int, raw_data: Optional[dict] = None) -> SearchResult:
    try:
        return parse_search_results(html, max_results=max_results, raw_data=raw_data)
    except Exception:  # noqa: BLE001 — a parse bug degrades this round, never crashes the run
        log.exception("parse_search_results raised — degrading to an empty result for this round")
        return SearchResult(products=[], total_available=None, source_used="none")


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
