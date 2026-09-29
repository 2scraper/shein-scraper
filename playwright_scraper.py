#!/usr/bin/env python3
"""playwright_scraper.py — Playwright engine for the shein-scraper family
member. Playwright is the primary engine (see selenium_scraper.py /
puppeteer_scraper.py for parity copies — all three must agree on exit
codes, run status and whether a run crashes or spends money — CLAUDE.md
§4).

**Local-first, same principle as every other family member** — this
launches an ordinary local headless Chromium by default and does NOT
require a 2Captcha Scraping Browser (`--cdp-endpoint`) session to run.
Unlike this repo's earlier siblings' first builds, this one is NOT
starting from zero live data: a browser-rendering tool (not this engine
itself — see TESTING.md) already confirmed shein.com's real search-page
data source and a real, live bot-mitigation incident. **What that capture
does NOT confirm is what THIS engine — a real headless Playwright launch,
a different client — actually gets.** `--proxy`/`--cdp-endpoint`/
`--fingerprint` remain opt-in power options, framed the same as every
sibling repo, not proven necessary defaults yet.

Example:
    python3 playwright_scraper.py --query "summer dress" --format json --out results.json
    python3 playwright_scraper.py --category "Women Jeans-c-1934.html" --max-results 40
    python3 playwright_scraper.py --url "https://us.shein.com/dsbayvkj-p-33704388.html"

This engine reads `window.gbRawData` LIVE via `page.evaluate()` on every
round — the confirmed-real, richest source (see shein_parser.py's module
docstring) — falling back to extracting the same object from the raw
HTML when evaluation fails for any reason. Whether scrolling actually
grows this object with more real products past its first SSR batch, or
needs some other trigger, is UNCONFIRMED (see shein_parser.py) — this
loop re-checks the object each round rather than assuming either answer,
and stops on the same stall-based heuristic as lidl-scraper's own loop.
"""
from __future__ import annotations

import argparse
import random
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional

try:
    from playwright.async_api import Browser, BrowserContext, Page, async_playwright
except ImportError as _IMPORT_ERROR:  # pragma: no cover — exercised by smoke_test's no-engine path
    Browser = BrowserContext = Page = None
    async_playwright = None
    _PLAYWRIGHT_IMPORT_ERROR = _IMPORT_ERROR
else:
    _PLAYWRIGHT_IMPORT_ERROR = None

import env_config
import shein_parser as sp
from captcha_solver import CaptchaType, build_injection_script, detect_from_html, solve_when_blocked
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH, EXIT_REMOTE_API_ERROR, Product, finish_run, sku_key as _sku_key
from proxy_pool import Proxy, ProxyPool, ProxyParseError, is_proxy_dead_error, load_proxies, redact_credentials
from scraper_api_client import TwoCaptchaAuthError, TwoCaptchaClient, TwoCaptchaError

ENGINE_NAME = "playwright"

# --- the handful of engine constants that vary per site (CLAUDE.md §5) ---
NAV_TIMEOUT_MS = 30_000
READINESS_WAIT_MS = 3_000  # window.gbRawData is confirmed present in the initial
                            # SSR payload (no extra XHR needed for the first
                            # batch — see shein_parser.py) so this is mostly
                            # slack for the risk-gateway redirect (if any) to
                            # settle, not for client-side hydration.
MIN_CARD_MATCHES = sp.MIN_CARD_MATCHES

log = logging.getLogger("playwright_scraper")


def _positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return ivalue


def _nonnegative_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {value!r})")
    return ivalue


def _nonnegative_float(value: str) -> float:
    fvalue = float(value)
    if fvalue < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {value!r})")
    return fvalue


def _jittered_delay(base_seconds: float, jitter: float) -> float:
    """Multiply base_seconds by a random factor in [1-jitter, 1+jitter] so
    repeated waits (scroll pauses, retry backoff, a rate-limit cooldown)
    aren't perfectly periodic — an easy signal for a site's own rate/
    bot-detection heuristics to key off of. jitter<=0 disables this (e.g.
    for reproducible tests); never returns a negative delay."""
    if base_seconds <= 0 or jitter <= 0:
        return base_seconds
    return max(0.0, base_seconds * random.uniform(1 - jitter, 1 + jitter))

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="shein.com fashion listing scraper — Playwright engine",
        epilog="Credentials belong in .env / SHEIN_PROXY / TWOCAPTCHA_KEY — never on this command line.",
    )
    p.add_argument("--url", default=None, help="Full shein.com search/category/product URL (or set SHEIN_URL) — overrides --query/--category")
    p.add_argument("--query", default=None, help="Search term, e.g. 'summer dress'")
    p.add_argument("--category", default=None, help="A category path copied from shein.com navigation, e.g. 'Women Jeans-c-1934.html'")
    p.add_argument("--sort", choices=sp.SORT_VALUES, default="relevance", help="Recorded on the run only — NOT yet wired into the URL, see shein_parser.py")
    p.add_argument("--max-results", type=_positive_int, default=30, help="Cap on number of products scraped")
    p.add_argument("--max-scrolls", type=_positive_int, default=20, help="Hard cap on scroll rounds, independent of --stall-rounds")
    p.add_argument("--stall-rounds", type=_positive_int, default=4, help="Stop after this many consecutive scrolls add no new product")
    p.add_argument("--scroll-delay", type=_nonnegative_float, default=1.5, help="Delay between scroll rounds, seconds")
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--out", default=None, help="Output path (default: shein_results.<format>)")
    p.add_argument("--retries", type=_nonnegative_int, default=2, help="Retries on initial navigation failure")
    p.add_argument("--retry-delay", type=_nonnegative_float, default=3.0)
    p.add_argument("--delay-jitter", type=_nonnegative_float, default=0.3, help="Relative +/-jitter applied to --scroll-delay/--retry-delay/--rate-limit-cooldown so repeated waits are not perfectly periodic (0 disables, e.g. for reproducible tests)")
    p.add_argument("--rate-limit-cooldown", type=_nonnegative_float, default=0.0, help="On SHEIN's own rate-limit gate (/risk/action/limit), wait this many seconds and retry ONCE on the same session before giving up, honoring the ~5 minute cooldown observed live (see TESTING.md). Off (0) by default — this can make a single invocation take minutes; consider e.g. 300 for unattended/scheduled runs.")
    p.add_argument(
        "--block-retries", type=_nonnegative_int, default=2,
        help="On a blocked, zero-product outcome, retry on the SAME browser/CDP session (same exit "
             "IP, same device identity) this many extra times before giving up — 'retry before you "
             "rotate', not a proxy/session swap. Sibling family member etsy-scraper measured this "
             "directly against its own DataDome-protected site: one profile was refused twice "
             "(t=bv) then cleared on the third attempt onward — see its README. A fresh --proxy/"
             "--cdp-endpoint identity is a separate, manual decision the caller makes between runs, "
             "not something this flag does automatically (see README's 'Known limitations').",
    )
    p.add_argument("--proxy", default=None, help="A single proxy, e.g. http://login:pass@host:port (or set SHEIN_PROXY)")
    p.add_argument("--proxy-file", default=None, help="One proxy per line, same formats as --proxy")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=3)
    p.add_argument("--twocaptcha-key", default=None, help="(or set TWOCAPTCHA_KEY)")
    p.add_argument("--captcha-api", default=None, help="Override the 2Captcha API base URL (testing only)")
    p.add_argument("--solve-captcha", choices=["off", "when-blocked", "always"], default="when-blocked")
    p.add_argument("--min-score", type=float, default=0.3, help="Minimum acceptable reCAPTCHA v3 score (2Captcha's minScore task field)")
    p.add_argument("--cdp-endpoint", default=None, help="Connect to a remote CDP session (e.g. the 2Captcha Scraping Browser API) instead of launching locally (or set SHEIN_CDP_ENDPOINT) — opt-in, not required for a normal run")
    p.add_argument("--fingerprint", action="store_true", help="Fetch and apply a 2Captcha Fingerprint API profile (ignored with --cdp-endpoint — see fingerprint_client.refuse_if_cdp)")
    p.add_argument("--fp-tags", default=None, help="Fingerprint API filter, e.g. 'Windows,Chrome'")
    p.add_argument("--fp-country", default=None, help="Fingerprint API filter, e.g. 'us'")
    p.add_argument(
        "--scraper-api", action="store_true",
        help="Fetch via 2Captcha's Scraper API (scraper.2captcha.com) instead of launching any local "
             "or --cdp-endpoint browser — a single browserless HTTP call, run entirely on 2Captcha's "
             "own infrastructure. Requires --twocaptcha-key/TWOCAPTCHA_KEY. A GENUINELY DIFFERENT "
             "product from --cdp-endpoint's Scraping Browser API — see scraper_api_client.py's module "
             "docstring. Confirmed live 2026-09-22: real shein.com pages come back, but this is NOT a "
             "confirmed bypass — the same /risk/challenge interstitial this repo already knows about "
             "shows up here too, intermittently (see --block-retries). By itself this mode has no "
             "captcha solving (a solved token has nothing to inject into — no live page/DOM here) and "
             "no documented way to pin the exit country/locale, so a clean (non-blocked) response can "
             "still land on a non-US shein.com locale this repo's parser doesn't recognise. See "
             "--scraper-api-cdp for the fix to both. --max-scrolls/--stall-rounds/--scroll-delay/"
             "--proxy/--cdp-endpoint/--fingerprint are all IGNORED in this mode (a single static fetch "
             "has no scroll loop, and brings its own exit IP/device) — set together, they log a "
             "warning rather than silently doing nothing.",
    )
    p.add_argument("--scraper-api-timeout", type=_positive_int, default=60, help="Seconds 2Captcha itself waits for the target page to finish loading (1-120, their limit)")
    p.add_argument("--scraper-api-url", default=None, help="Override the Scraper API base URL (testing only)")
    p.add_argument(
        "--scraper-api-cdp", action="store_true",
        help="Route --scraper-api's fetch through a 2Captcha Scraping Browser CDP session (their "
             "'cdpurl' field on the Scraper API task) instead of their own default browser pool — "
             "chaining two 2Captcha products together, not pointing this at a caller-supplied "
             "--cdp-endpoint (that flag stays ignored in --scraper-api mode, see its help text: an "
             "arbitrary CDP session isn't known to support this field the way 2Captcha's own does). "
             "This selects an existing country-configured Browser API account and may enable "
             "its captcha auto-solve — see scraper_api_client.TwoCaptchaClient.scraping_browser_connection_url and "
             "scrape_url's own docstrings for exactly what 2Captcha documents. Requires "
             "--scraper-api. WIRED BUT NOT YET LIVE-TESTED: the underlying 'cdpurl' field is "
             "documented by 2Captcha but this codebase had never exercised it before this flag "
             "existed — confirm it live before relying on it (TESTING.md). If the Scraping "
             "Browser session itself fails (a Scraper API HTTP error, not a normal blocked-with-"
             "zero-products outcome), this run automatically falls back to --scraper-api's plain "
             "default pool once, logged loudly, rather than giving up outright — losing country/"
             "profile selection and 2Captcha's own captcha auto-solve for the rest of that run.",
    )
    p.add_argument("--scraper-api-country", default=None, help="Require an existing Browser API account configured for this country, e.g. us; does not change its proxy country (ignored without --scraper-api-cdp)")
    p.add_argument("--scraper-api-account-id", type=_positive_int, default=None, help="Existing 2Captcha Browser API account ID for --scraper-api-cdp; required when multiple accounts match the requested country")
    p.add_argument("--scraper-api-profile-id", default=None, help="Reuse a specific Scraping Browser profile id across runs for --scraper-api-cdp, instead of the default pool (ignored without --scraper-api-cdp; see scraping_browser_connection_url's docstring on why reuse is preferred)")
    p.add_argument("--allow-empty", action="store_true", help="Write output even if zero products were found")
    p.add_argument("--dump-html", action="store_true", help="Save the final accumulated page HTML next to --out, on success too")
    p.add_argument("--headless", dest="headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    return p


def _default_out(fmt: str) -> str:
    return f"shein_results.{fmt}"


def _resolve_start_url(args: argparse.Namespace):
    if args.url:
        if sp.is_disallowed_path(args.url):
            return None, True  # (url, is_product_page) — signal handled by caller
        return args.url, args.url.count("-p-") > 0 and args.url.endswith(".html") and "pdsearch" not in args.url
    if args.query or args.category:
        return sp.search_url(query=args.query, category_path=args.category), False
    return None, False


def _dump_path(out_path: str) -> str:
    stem = Path(out_path).with_suffix("")
    return f"{stem}_debug.html"


async def _new_context(
    browser: Browser, proxy: Optional[Proxy], user_agent: Optional[str], *,
    reuse_default: bool = False,
) -> BrowserContext:
    if reuse_default:
        # A Browser API profile keeps its cookies in the existing CDP
        # context. browser.new_context() creates an empty, isolated jar and
        # discards a manually completed SHEIN risk challenge.
        if browser.contexts:
            return browser.contexts[0]
        # Confirmed live 2026-09-29 (Roman's own machine, --block-retries):
        # after two blocked attempts on the same Browser API profile,
        # browser.contexts came back EMPTY on the third attempt -- the
        # provider's own infrastructure appears to recycle the underlying
        # session during a long block, out from under this still-connected
        # CDP handle. Raising here used to crash the whole run (CLAUDE.md
        # §6: a block/retry must degrade to a failed unit, never a crash
        # that discards already-collected data). Fall back to a fresh,
        # empty context instead -- this attempt loses whatever cookies the
        # persistent context carried (a previously solved challenge no
        # longer applies), but the run itself survives to report a normal
        # blocked/zero-product outcome rather than an unhandled traceback.
        log.warning(
            "CDP browser has no persistent default context (the provider "
            "appears to have recycled this profile's session) -- falling "
            "back to a fresh context for this attempt; any previously "
            "solved captcha's cookies are gone."
        )
    kwargs = {}
    if proxy is not None:
        kwargs["proxy"] = proxy.playwright_proxy_dict()
    if user_agent:
        kwargs["user_agent"] = user_agent
    return await browser.new_context(**kwargs)


async def _close_scrape_page(page: Page, context: BrowserContext, *, reuse_default: bool) -> None:
    await page.close()
    if not reuse_default:
        await context.close()


async def _enable_scraping_browser_auto_solve(context: BrowserContext, page: Page) -> None:
    """Only meaningful over --cdp-endpoint — see the identical helper in
    every sibling repo's playwright_scraper.py."""
    try:
        session = await context.new_cdp_session(page)
        session.on("Captcha.detected", lambda *_: log.info("[Scraping Browser API] captcha detected"))
        session.on("Captcha.solveFinished", lambda *_: log.info("[Scraping Browser API] captcha solved"))
        session.on("Captcha.solveFailed", lambda *_: log.warning("[Scraping Browser API] captcha solve failed"))
        await session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
    except Exception as exc:  # noqa: BLE001 — optional enhancement, never fatal
        log.warning("Captcha.setAutoSolve unavailable on this CDP session (continuing without it): %s", exc)


async def _maybe_solve_captcha(
    *, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str, min_score: float = 0.3,
    page: Optional[Page] = None, count_product_links=None,
) -> Optional[dict]:
    """`count_product_links` defaults to `sp.count_result_cards` — correct
    for scrape_search()'s own call site, WRONG for a product-detail page
    (which has zero search-result cards by definition, so the default
    would read every product-page load as "0 products present" and
    attempt a solve on ANY bot-challenge marker match, including markers
    that are present site-wide and harmless — see README "Known
    limitations", the reCAPTCHA-loaded-everywhere finding). Fixed
    2026-09-22: scrape_product_page() below now passes its own callable
    (did sp.parse_product_page() already find real data?) instead of
    silently inheriting the search-page-shaped default."""
    if policy == "off" or client is None:
        return None
    result = solve_when_blocked(
        client=client, page_url=url, html=html, count_product_links=count_product_links or sp.count_result_cards,
        extra_markers=sp.BOT_CHALLENGE_MARKERS, min_score=min_score,
    )
    action = result.get("action")
    if action == "no_captcha_detected":
        pass
    elif action == "skipped_products_present":
        log.info("Captcha-like marker present but results already rendered — not solving.")
    elif action == "warning_no_key":
        log.warning("Captcha solving skipped: %s", result.get("detail"))
    elif action == "warning_solver_error":
        log.warning("Captcha solve failed: %s", result.get("detail"))
    elif action == "solved":
        log.info("Captcha solved via 2Captcha (%s).", result.get("captcha_type"))
        # Actually write the solution back into the page — see
        # captcha_solver.build_injection_script's docstring for the
        # honesty caveat: this uses each widget's own STANDARD, publicly
        # documented convention, never anything confirmed against a real
        # shein.com capture. reCAPTCHA v3 has no such convention and
        # returns None (nothing to inject; the token is still in `result`
        # for a caller with site-specific knowledge to use).
        if page is not None:
            script = build_injection_script(CaptchaType(result["captcha_type"]), result["token"])
            if script is None:
                log.info(
                    "No generic injection point for %s — token was solved but not written into "
                    "the page (this is expected for reCAPTCHA v3; see captcha_solver.py).",
                    result.get("captcha_type"),
                )
            else:
                try:
                    injected = await page.evaluate(script)
                    log.info(
                        "Injected solved %s into the page (found a target element/callback: %s) "
                        "— unconfirmed whether shein.com's real widget actually reads this "
                        "standard-convention field/callback.",
                        result.get("captcha_type"), bool(injected),
                    )
                except Exception as exc:  # noqa: BLE001 — a failed injection degrades, never crashes the run
                    log.warning("Captcha solved but injecting it into the page failed: %s", exc)
    elif action == "detected_unidentified_widget":
        log.warning(
            "A bot-mitigation marker was detected but no known widget/sitekey could be extracted "
            "— SHEIN's own risk-gateway family (/risk/challenge, /risk/action/limit) has two "
            "confirmed real incidents with no known 2Captcha automated solve path: the "
            "captcha-shaped /risk/challenge, and /risk/action/limit, which looks like a plain "
            "rate limit with nothing to solve at all (see shein_parser.py's module docstring); "
            "this is expected for either case, not necessarily a bug."
        )
    return result


async def _connect_over_cdp(pw, cdp_endpoint: str):
    """See every sibling repo's playwright_scraper.py for why this wraps
    the connection error rather than letting it propagate: connect_over_cdp
    repeats a failed endpoint's login:password in its own message and
    "Call log" several times over."""
    try:
        return await pw.chromium.connect_over_cdp(cdp_endpoint)
    except Exception as exc:
        raise RuntimeError(f"CDP connection failed: {redact_credentials(str(exc))}") from None


async def _read_gb_raw_data(page: Page) -> Optional[dict]:
    """Live JS read, the confirmed-real, most robust source (see
    shein_parser.py) — never lets a JS-side error crash the run."""
    try:
        return await page.evaluate("() => window.gbRawData || null")
    except Exception as exc:  # noqa: BLE001 — falls back to HTML-text extraction, never fatal
        log.debug("page.evaluate('window.gbRawData') failed, will fall back to HTML extraction: %s", exc)
        return None


async def scrape_search(
    *, args: argparse.Namespace, start_url: str, browser: Browser,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    autosolve: bool, user_agent: Optional[str],
) -> tuple:
    """Returns (products, blocked, remote_api_error, scroll_rounds_done, scroll_error)
    — same contract as every sibling engine's own scrape_search()/
    scrape_urls()."""
    blocked = False
    remote_api_error = False
    scroll_error = False

    proxy = proxy_pool.next() if proxy_pool else None
    log.info("Using proxy %s", proxy.masked() if proxy else "(no local proxy pool — direct connection, or a --cdp-endpoint session providing its own exit)")
    reuse_default = bool(args.cdp_endpoint)
    context = await _new_context(browser, proxy, user_agent, reuse_default=reuse_default)
    page = await context.new_page()
    if autosolve:
        await _enable_scraping_browser_auto_solve(context, page)

    last_error = None
    status = None
    for attempt in range(args.retries + 1):
        try:
            response = await page.goto(start_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(READINESS_WAIT_MS)
            status = response.status if response is not None else None
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001 — every remote call must be bounded and reported
            last_error = str(exc)
            log.warning("Navigation attempt %d/%d failed: %s", attempt + 1, args.retries + 1, last_error)
            if attempt < args.retries:
                await asyncio.sleep(_jittered_delay(args.retry_delay, args.delay_jitter))

    if last_error is not None:
        await _close_scrape_page(page, context, reuse_default=reuse_default)
        log.error("Search page permanently failed to load: %s", last_error)
        return [], False, True, 0, False

    # REAL, confirmed-live incidents (see shein_parser.py's module
    # docstring — TWO distinct endpoints now, a captcha-shaped
    # `/risk/challenge` and a rate-limit `/risk/action/limit`, the
    # latter confirmed via this exact engine, not a browser-rendering
    # tool): both silently redirect a request they don't like, rather
    # than returning a >=400 status — the final page.url is the single
    # most reliable signal for either, cheaper and more direct than
    # scanning HTML content (though sp.BOT_CHALLENGE_MARKERS below also
    # matches, since the redirect target is embedded as text in the
    # page's own SSR JSON state).
    if "/risk/action/limit" in page.url:
        args._rate_limited = True
    if any(marker in page.url for marker in sp.RISK_GATEWAY_URL_MARKERS):
        log.warning("Redirected to SHEIN's own risk gateway (%s) — treating as blocked.", page.url)
        blocked = True
        if proxy_pool is not None and proxy is not None:
            proxy_pool.report_failure(proxy, dead=False)

    if status is not None and status >= 400:
        log.warning("Search page returned HTTP %d — treating as blocked, not empty.", status)
        blocked = True
        if proxy_pool is not None and proxy is not None and status in (403, 429):
            proxy_pool.report_failure(proxy, dead=True)
    elif status is not None and proxy_pool is not None and proxy is not None:
        proxy_pool.report_success(proxy)

    seen_skus: set = set()
    merged: List[Product] = []
    stall = 0
    previous_product_count = -1
    rounds = 0

    for round_num in range(args.max_scrolls + 1):
        rounds = round_num
        if args._rate_limited:
            break
        html = await page.content()
        raw_data = await _read_gb_raw_data(page)
        cards_present = sp.count_result_cards(html) > 0
        # SHEIN's /risk/challenge SSR state embeds its OWN redirect URL as
        # text (see the comment above the initial page.url check above) —
        # but a REAL, live capture (Roman, 2026-09-22, captcha_type=909)
        # showed that text can persist on a page the browser has already
        # moved PAST the gateway to: a plain homepage, zero captcha
        # widgets, zero challenge iframes, `window.gbRawData` present but
        # not search data — see shein_parser.py's module docstring. A bare
        # substring match on `html` alone can't tell "still gated" apart
        # from "was gated a few seconds ago, then redirected somewhere
        # unrelated". `page.url` can, since it reflects the CURRENT
        # navigation rather than stale embedded state, so a marker match
        # past round 0 (round 0 is corroborated by the page.url check that
        # already ran right after the initial goto) is only trusted when
        # `page.url` still shows the gateway too.
        captcha_detected = detect_from_html(html, sp.BOT_CHALLENGE_MARKERS)
        if captcha_detected and round_num > 0 and not any(marker in page.url for marker in sp.RISK_GATEWAY_URL_MARKERS):
            log.warning(
                "A bot-mitigation marker matched stale page text, but the page has moved on to "
                "%s (%s) — not re-flagging this round as a captcha block.",
                page.url, sp.diagnose_unexpected_page(html),
            )
            captcha_detected = False
        if captcha_detected and not cards_present:
            blocked = True
        captcha_result = None
        if captcha_detected:
            captcha_result = await _maybe_solve_captcha(html=html, url=start_url, client=client, policy=args.solve_captcha, min_score=args.min_score, page=page)
        if captcha_result and captcha_result.get("action") in ("warning_no_key", "warning_solver_error", "detected_unidentified_widget"):
            if sp.count_result_cards(html) == 0:
                blocked = True

        result = sp.safe_parse_search_results(html, max_results=args.max_results, raw_data=raw_data)
        if result.source_used == "none" and round_num == 0 and not blocked:
            # See shein_parser.diagnose_unexpected_page's own docstring — a
            # real, live 2026-09-21 case (Roman's own first engine run) hit
            # exactly this with a page that wasn't blocked, wasn't a parse
            # bug, but genuinely wasn't a search-results page at all.
            log.warning(
                "No products recognised on the first render (%s, final URL: %s) — either "
                "this search genuinely has no results, shein_parser.py's window.gbRawData "
                "path needs updating for the current shein.com markup, or shein.com served "
                "a DIFFERENT page than search results for this request (a real, not just "
                "hypothetical, case — see shein_parser.py's module docstring, "
                "'First real engine run' section). Re-run with --dump-html to inspect the "
                "captured page.",
                sp.diagnose_unexpected_page(html), page.url,
            )

        round_skus = {_sku_key(p) for p in result.products}
        new_skus = round_skus - seen_skus
        current_product_count = len(round_skus)
        if new_skus:
            for p in result.products:
                if _sku_key(p) in new_skus:
                    merged.append(p)
            seen_skus |= new_skus
            stall = 0
        else:
            if previous_product_count == current_product_count:
                stall += 1
            else:
                stall = 0
        previous_product_count = current_product_count

        if result.products:
            blocked = False

        if len(merged) >= args.max_results:
            merged = merged[: args.max_results]
            break
        if round_num >= args.max_scrolls:
            break
        if stall >= args.stall_rounds:
            break

        try:
            # Whether this actually grows window.gbRawData with more real
            # products, or just renders more skeleton/lazy cards already
            # present in the initial DOM, is UNCONFIRMED — see
            # shein_parser.py's module docstring. The stall-based
            # `--stall-rounds` cap above is what keeps this loop bounded
            # either way, rather than assuming scrolling always helps.
            await page.evaluate(
                "() => window.scrollBy(0, Math.max(Math.floor(window.innerHeight * 0.8), 600))"
            )
        except Exception as exc:  # noqa: BLE001 — a scroll failure ends the loop, not the run
            log.warning("Scroll failed, stopping pagination early: %s", exc)
            scroll_error = True
            break
        await asyncio.sleep(_jittered_delay(args.scroll_delay, args.delay_jitter))

    final_html = await page.content()
    final_raw_data = await _read_gb_raw_data(page)
    total_available = sp.total_result_count(final_html, raw_data=final_raw_data)
    expected = min(total_available, args.max_results) if total_available is not None else None
    if expected is not None and len(merged) < expected:
        log.warning(
            "Fewer products collected than SHEIN reports available: reports %d, requested %d, collected %d.",
            total_available, args.max_results, len(merged),
        )
        scroll_error = True

    if args.dump_html:
        Path(_dump_path(args.out)).write_text(final_html, encoding="utf-8")

    await _close_scrape_page(page, context, reuse_default=reuse_default)
    return merged, blocked, remote_api_error, rounds, scroll_error


async def scrape_product_page(
    *, args: argparse.Namespace, start_url: str, browser: Browser,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    autosolve: bool, user_agent: Optional[str],
) -> tuple:
    """A --url pointed directly at one product page — confirmed real via
    shein_parser.parse_product_page()'s JSON-LD path. Returns the same
    5-tuple shape as scrape_search() for a uniform caller."""
    blocked = False
    remote_api_error = False

    proxy = proxy_pool.next() if proxy_pool else None
    reuse_default = bool(args.cdp_endpoint)
    context = await _new_context(browser, proxy, user_agent, reuse_default=reuse_default)
    page = await context.new_page()
    if autosolve:
        await _enable_scraping_browser_auto_solve(context, page)

    last_error = None
    status = None
    for attempt in range(args.retries + 1):
        try:
            response = await page.goto(start_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(READINESS_WAIT_MS)
            status = response.status if response is not None else None
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            log.warning("Navigation attempt %d/%d failed: %s", attempt + 1, args.retries + 1, last_error)
            if attempt < args.retries:
                await asyncio.sleep(_jittered_delay(args.retry_delay, args.delay_jitter))

    if last_error is not None:
        await _close_scrape_page(page, context, reuse_default=reuse_default)
        return [], False, True, 0, False

    if "/risk/action/limit" in page.url:
        args._rate_limited = True
    if any(marker in page.url for marker in sp.RISK_GATEWAY_URL_MARKERS):
        blocked = True
    if status is not None and status >= 400:
        blocked = True

    html = await page.content()
    # Fixed 2026-09-22 (documented in README "Known limitations" as a real,
    # confirmed, not-yet-fixed gap): this path never attempted captcha
    # solving at all — only scrape_search()'s round loop called
    # _maybe_solve_captcha. `count_product_links` here is product-page-
    # shaped (did the page already parse?), not search-page-shaped — see
    # _maybe_solve_captcha's own docstring for why passing the default
    # would be wrong here.
    captcha_result = None
    if detect_from_html(html, sp.BOT_CHALLENGE_MARKERS):
        captcha_result = await _maybe_solve_captcha(
            html=html, url=start_url, client=client, policy=args.solve_captcha, min_score=args.min_score,
            page=page, count_product_links=lambda h: 1 if sp.parse_product_page(h, url=start_url) else 0,
        )
        if captcha_result and captcha_result.get("action") == "solved":
            # No scroll/round loop here to pick the injection up naturally
            # on a later round the way scrape_search() does — a single
            # re-fetch after a beat is what actually gives this attempt a
            # chance to matter, rather than solving/injecting a token that
            # nothing ever re-reads.
            await page.wait_for_timeout(READINESS_WAIT_MS)
            html = await page.content()
        elif captcha_result and captcha_result.get("action") in ("warning_no_key", "warning_solver_error", "detected_unidentified_widget"):
            if not sp.parse_product_page(html, url=start_url):
                blocked = True

    product = sp.parse_product_page(html, url=start_url)
    if args.dump_html:
        Path(_dump_path(args.out)).write_text(html, encoding="utf-8")
    await _close_scrape_page(page, context, reuse_default=reuse_default)
    products = [product] if product else []
    if not products and not blocked:
        log.warning("Product page rendered but no ProductGroup/Product JSON-LD was found — see shein_parser.py.")
    return products, blocked, remote_api_error, 0, False


def _scrape_via_scraper_api(
    *, args: argparse.Namespace, start_url: str, is_product_page: bool, client: TwoCaptchaClient,
    cdp_url: Optional[str] = None,
) -> tuple:
    """--scraper-api's own fetch path: one browserless HTTP call to
    2Captcha's Scraper API, no local/CDP browser, no scroll loop (a static
    HTML snapshot can't scroll itself — see --scraper-api's help text).
    Returns the same 5-tuple shape as scrape_search()/scrape_product_page()
    so run() below can treat all three the same way; `rounds` is always 0
    here since there is exactly one fetch, never a round loop.

    `cdp_url`, when --scraper-api-cdp set one (see run()), routes this
    fetch through 2Captcha's own Scraping Browser instead of their
    default pool — real captcha auto-solve and the selected account's proxy settings apply on
    2Captcha's side of that session, not in this function; there is
    nothing this function itself needs to do differently to benefit from
    it beyond passing it through."""
    try:
        result = client.scrape_url(start_url, timeout=args.scraper_api_timeout, cdp_url=cdp_url)
    except TwoCaptchaAuthError as exc:
        log.error("Scraper API: %s", exc)
        return [], False, True, 0, False
    except TwoCaptchaError as exc:
        log.error("Scraper API request failed — treating as remote_api_error, not a crash: %s", exc)
        return [], False, True, 0, False

    html = result.body
    if "/risk/action/limit" in html:
        args._rate_limited = True
    blocked = False
    if result.target_status is not None and result.target_status >= 400:
        log.warning("Scraper API: target page returned HTTP %d — treating as blocked.", result.target_status)
        blocked = True
    # No live page.url to corroborate a marker match against here (see
    # scrape_search()'s own comment on why that check exists on the
    # browser path) — this fetch's body IS the final response, there is
    # nothing further the target could have navigated on to in the
    # meantime, so a marker match is trusted outright.
    if detect_from_html(html, sp.BOT_CHALLENGE_MARKERS):
        blocked = True

    if args.dump_html:
        Path(_dump_path(args.out)).write_text(html, encoding="utf-8")

    # --solve-captcha itself is still a documented no-op here: THIS
    # function never calls captcha_solver/TwoCaptchaClient.solve_and_wait
    # — there is no live page/DOM in this mode for a solved token to be
    # injected into (see --scraper-api's help text). Captcha handling in
    # this mode comes entirely from --scraper-api-cdp instead (2Captcha's
    # own Scraping Browser solves on their side of that session, before
    # the HTML ever reaches this function) — --block-retries (retry this
    # same call) remains the only mitigation for a still-blocked response
    # either way.
    if is_product_page:
        product = sp.parse_product_page(html, url=start_url)
        products = [product] if product else []
        if not products and not blocked:
            log.warning("Product page fetched via Scraper API but no ProductGroup/Product JSON-LD was found.")
        return products, blocked, False, 0, False

    parsed = sp.safe_parse_search_results(html, max_results=args.max_results)
    if parsed.source_used == "none" and not blocked:
        log.warning(
            "No products recognised in the Scraper API response (%s) — either this search "
            "genuinely has no results, or the fetch landed on a page/locale this repo's parser "
            "doesn't recognise (--scraper-api's help text has the known locale caveat). Re-run "
            "with --dump-html to inspect what actually came back.",
            sp.diagnose_unexpected_page(html),
        )
    return parsed.products, blocked, False, 0, False


async def run(args: argparse.Namespace) -> int:
    args._rate_limited = False
    args._cooldown_used = False
    started_at = time.time()
    start_url, is_product_page = _resolve_start_url(args)
    if not start_url:
        if args.url and sp.is_disallowed_path(args.url):
            print(f"Error: --url {args.url!r} matches a robots.txt-disallowed path — refusing.", file=sys.stderr)
        else:
            print("Error: provide --url, --query, or --category", file=sys.stderr)
        return EXIT_BAD_USAGE
    if args.format not in ("json", "csv"):
        print(f"Error: unsupported --format {args.format!r}", file=sys.stderr)
        return EXIT_BAD_USAGE
    args.out = args.out or _default_out(args.format)

    if args.scraper_api_cdp and not args.scraper_api:
        print("Error: --scraper-api-cdp requires --scraper-api", file=sys.stderr)
        return EXIT_BAD_USAGE

    if args.scraper_api:
        # A whole separate, browserless code path — no Playwright import
        # is needed at all here (this mode runs even in an environment
        # with no engine driver installed — see CLAUDE.md §6), so the
        # async_playwright-is-None check below is skipped entirely.
        if not args.twocaptcha_key:
            print("Error: --scraper-api requires --twocaptcha-key/TWOCAPTCHA_KEY", file=sys.stderr)
            return EXIT_BAD_USAGE
        if args.proxy or args.proxy_file or args.cdp_endpoint or args.fingerprint:
            log.warning(
                "--scraper-api ignores --proxy/--proxy-file/--cdp-endpoint/--fingerprint — this mode "
                "brings its own exit IP/device via 2Captcha's own infrastructure, see --scraper-api's "
                "help text."
            )
        if (args.scraper_api_country or args.scraper_api_profile_id or args.scraper_api_account_id) and not args.scraper_api_cdp:
            log.warning(
                "--scraper-api-country/--scraper-api-account-id/--scraper-api-profile-id are ignored without --scraper-api-cdp "
                "— there is no Scraping Browser session for them to apply to."
            )
        client = TwoCaptchaClient(args.twocaptcha_key, api_base=args.captcha_api, scraper_api_base=args.scraper_api_url)
        cdp_url = None
        if args.scraper_api_cdp and not args.scraper_api_profile_id:
            log.warning(
                "--scraper-api-cdp without --scraper-api-profile-id — each run gets a fresh "
                "profile from 2Captcha's default pool instead of a warmed, reused identity. "
                "Pass --scraper-api-profile-id to reuse one across runs (see "
                "scraping_browser_connection_url's docstring and README/TESTING.md)."
            )
        if args.scraper_api_cdp:
            # Constructed once, reused across every --block-retries attempt
            # below — same reuse-a-profile principle scraping_browser_
            # connection_url's own docstring recommends, not a fresh
            # session minted per attempt.
            try:
                cdp_url = client.scraping_browser_connection_url(
                    country=args.scraper_api_country, profile_id=args.scraper_api_profile_id,
                    account_id=args.scraper_api_account_id,
                )
            except TwoCaptchaError as exc:
                log.error("Scraping Browser connection setup failed: %s", exc)
                return EXIT_REMOTE_API_ERROR
        blocked = remote_api_error = False
        merged: List[Product] = []
        cdp_fallback_used = False
        for block_attempt in range(args.block_retries + 1):
            merged, blocked, remote_api_error, rounds, scroll_error = _scrape_via_scraper_api(
                args=args, start_url=start_url, is_product_page=is_product_page, client=client,
                cdp_url=cdp_url,
            )
            if remote_api_error and cdp_url is not None and not cdp_fallback_used:
                # The Scraping Browser session itself is what failed here (an
                # HTTP-level error from client.scrape_url — bad/expired
                # cdpurl, or a generic Scraper API problem that would fail
                # identically without it), not a normal blocked-with-zero-
                # products outcome, so this isn't what --block-retries is
                # for. One automatic fallback attempt on 2Captcha's plain
                # default pool instead of giving up outright — this drops
                # --scraper-api-cdp's country/profile pinning and 2Captcha's
                # own captcha auto-solve for the rest of this run, so it's
                # logged loudly, not silent. cdp_url is cleared for good
                # (not just this attempt) so a remaining --block-retries
                # attempt doesn't keep hitting the same broken session.
                log.warning(
                    "--scraper-api-cdp's Scraping Browser session failed — falling back to "
                    "--scraper-api's plain default pool for the rest of this run instead of "
                    "giving up outright. This run no longer has --scraper-api-cdp's country/"
                    "profile selection or 2Captcha's own captcha auto-solve."
                )
                cdp_fallback_used = True
                cdp_url = None
                merged, blocked, remote_api_error, rounds, scroll_error = _scrape_via_scraper_api(
                    args=args, start_url=start_url, is_product_page=is_product_page, client=client,
                    cdp_url=cdp_url,
                )
            if args._rate_limited:
                if args.rate_limit_cooldown > 0 and not args._cooldown_used:
                    args._cooldown_used = True
                    args._rate_limited = False
                    _wait_s = _jittered_delay(args.rate_limit_cooldown, args.delay_jitter)
                    log.warning(
                        "SHEIN rate limit reached — waiting %.0fs (opt-in --rate-limit-cooldown, "
                        "honoring the ~5 minute cooldown observed live) before ONE retry on the "
                        "same session, instead of giving up immediately.",
                        _wait_s,
                    )
                    time.sleep(_wait_s)
                    continue
                log.warning("SHEIN rate limit reached; stopping this run without block retries. Try again after at least five minutes.")
                break
            if remote_api_error or not (blocked and not merged):
                break
            if block_attempt < args.block_retries:
                log.warning(
                    "Blocked with zero products (Scraper API attempt %d/%d) — retrying the same "
                    "fetch before giving up.",
                    block_attempt + 1, args.block_retries + 1,
                )
                time.sleep(_jittered_delay(args.retry_delay, args.delay_jitter))
        price_confirmed_pct = (sum(1 for p in merged if p.price is not None) / len(merged)) if merged else None
        return finish_run(
            products=merged, out_path=args.out, fmt=args.format, engine=ENGINE_NAME, url=start_url,
            pages_requested=1, pages_completed=0 if remote_api_error else 1, failed_pages=None,
            blocked=blocked, remote_api_error=remote_api_error, allow_empty=args.allow_empty,
            started_at=started_at, price_confirmed_pct=price_confirmed_pct,
        )

    if async_playwright is None:
        print(f"Error: playwright is not installed ({_PLAYWRIGHT_IMPORT_ERROR}). "
              f"pip install -r requirements-playwright.txt && playwright install chromium", file=sys.stderr)
        return EXIT_CRASH

    try:
        proxies = load_proxies(args.proxy, args.proxy_file)
    except ProxyParseError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE
    proxy_pool = ProxyPool(proxies, shuffle=args.proxy_shuffle, block_retries=args.proxy_block_retries) if proxies else None

    client = None
    if args.twocaptcha_key and (args.solve_captcha != "off" or args.fingerprint):
        client = TwoCaptchaClient(args.twocaptcha_key, api_base=args.captcha_api)

    user_agent = None
    cdp_refused_fingerprint = refuse_if_cdp(args.cdp_endpoint)
    if args.fingerprint and not cdp_refused_fingerprint:
        if client is None:
            log.warning("--fingerprint requested but no --twocaptcha-key/TWOCAPTCHA_KEY set — continuing without one.")
        else:
            profile = fetch_fingerprint(client, tags=args.fp_tags, country=args.fp_country)
            if profile:
                user_agent = user_agent_from(profile)

    cdp_connect_failed = False
    try:
        async with async_playwright() as pw:
            if args.cdp_endpoint:
                if args.proxy or args.proxy_file:
                    log.warning("Ignoring --proxy: a --cdp-endpoint session already carries its own exit IP.")
                    proxy_pool = None
                try:
                    browser = await _connect_over_cdp(pw, args.cdp_endpoint)
                except RuntimeError as exc:
                    log.error("CDP connection failed — treating as remote_api_error, not a crash: %s", exc)
                    cdp_connect_failed = True
                    browser = None
            else:
                browser = await pw.chromium.launch(headless=args.headless)

            if cdp_connect_failed:
                merged, blocked, remote_api_error, rounds, scroll_error = [], False, True, 0, False
            else:
                autosolve = bool(args.cdp_endpoint) and args.solve_captcha != "off"
                scrape_fn = scrape_product_page if is_product_page else scrape_search
                # "Retry before you rotate" (etsy-scraper's own measured finding
                # against a comparably hard bot-mitigation system — see the
                # --block-retries help text): a blocked, zero-product outcome
                # gets retried on the SAME browser connection — same exit IP,
                # same CDP-provided device identity if --cdp-endpoint is in
                # play — before this run gives up. Local launches get a fresh
                # context per attempt. CDP attempts reuse the provider's
                # default context so an already-cleared risk challenge and
                # its cookies survive across pages and runs.
                for block_attempt in range(args.block_retries + 1):
                    merged, blocked, remote_api_error, rounds, scroll_error = await scrape_fn(
                        args=args, start_url=start_url, browser=browser, proxy_pool=proxy_pool,
                        client=client, autosolve=autosolve, user_agent=user_agent,
                    )
                    if args._rate_limited:
                        if args.rate_limit_cooldown > 0 and not args._cooldown_used:
                            args._cooldown_used = True
                            args._rate_limited = False
                            _wait_s = _jittered_delay(args.rate_limit_cooldown, args.delay_jitter)
                            log.warning(
                                "SHEIN rate limit reached — waiting %.0fs (opt-in --rate-limit-cooldown, "
                                "honoring the ~5 minute cooldown observed live) before ONE retry on the "
                                "same session, instead of giving up immediately.",
                                _wait_s,
                            )
                            await asyncio.sleep(_wait_s)
                            continue
                        log.warning("SHEIN rate limit reached; stopping this run without block retries. Try again after at least five minutes.")
                        break
                    if not (blocked and not merged):
                        break
                    if block_attempt < args.block_retries:
                        log.warning(
                            "Blocked with zero products (attempt %d/%d on this same browser session) "
                            "— retrying on the SAME exit/identity rather than giving up immediately.",
                            block_attempt + 1, args.block_retries + 1,
                        )
                        await asyncio.sleep(_jittered_delay(args.retry_delay, args.delay_jitter))
                await browser.close()
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH

    price_confirmed_pct = (sum(1 for p in merged if p.price is not None) / len(merged)) if merged else None
    completed_rounds = rounds + 1
    failed_pages = [completed_rounds + 1] if scroll_error else None

    return finish_run(
        products=merged,
        out_path=args.out,
        fmt=args.format,
        engine=ENGINE_NAME,
        url=start_url,
        pages_requested=args.max_scrolls,
        pages_completed=completed_rounds,
        failed_pages=failed_pages,
        blocked=blocked,
        remote_api_error=remote_api_error,
        allow_empty=args.allow_empty,
        started_at=started_at,
        price_confirmed_pct=price_confirmed_pct,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = build_arg_parser()
    args = parser.parse_args()
    args = env_config.apply_env(args)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
