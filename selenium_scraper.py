#!/usr/bin/env python3
"""selenium_scraper.py — Selenium engine, parity copy of
playwright_scraper.py (same flags, same exit codes, same status semantics
— see output_writer.finish_run). Selenium is not the primary engine
(Playwright is); it exists for parity, not because it is preferred.

Same hard engine limits as the rest of the family (CLAUDE.md §6):

  - Selenium CANNOT use an AUTHENTICATED remote CDP endpoint.
    chromedriver's `debuggerAddress` takes a bare `host:port`; Playwright's
    `connect_over_cdp` and pyppeteer's `connect` take a full
    `ws://user:pass@host:port` and authenticate on the WebSocket upgrade.
    A `--cdp-endpoint` carrying credentials (the Scraping Browser API
    shape) is refused outright here with EXIT_BAD_USAGE — no half-working
    attempt — with a pointer to playwright_scraper.py / puppeteer_scraper.py.
  - Selenium's `--proxy-server` CANNOT authenticate at all. A `--proxy`
    with a login/password has its credentials stripped before being handed
    to Chrome, and this engine WARNS rather than silently dropping them.

Chrome binary: normally auto-detected by Selenium/Selenium Manager from a
regular Chrome/Chromium install. Set `CHROME_BIN` or `SELENIUM_CHROME_BIN`
to point at a specific binary instead; `SELENIUM_CHROMEDRIVER_PATH` /
`CHROMEDRIVER_PATH` pin a specific chromedriver (Selenium Manager's
default network auto-resolve fails outright in an offline/network-
restricted environment — see the constant below).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
except ImportError as _IMPORT_ERROR:  # pragma: no cover — exercised by smoke_test's no-engine path
    webdriver = None
    WebDriverException = Exception
    Options = None
    Service = None
    _SELENIUM_IMPORT_ERROR = _IMPORT_ERROR
else:
    _SELENIUM_IMPORT_ERROR = None

import env_config
import shein_parser as sp
from captcha_solver import CaptchaType, build_injection_script, detect_from_html, solve_when_blocked
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH, Product, finish_run, sku_key as _sku_key
from proxy_pool import Proxy, ProxyPool, ProxyParseError, is_proxy_dead_error, load_proxies
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from scraper_api_client import TwoCaptchaAuthError, TwoCaptchaClient, TwoCaptchaError

ENGINE_NAME = "selenium"
NAV_TIMEOUT_S = 30
READINESS_WAIT_S = 3.0
_CHROME_BINARY = os.environ.get("SELENIUM_CHROME_BIN") or os.environ.get("CHROME_BIN")
_CHROMEDRIVER_PATH = os.environ.get("SELENIUM_CHROMEDRIVER_PATH") or os.environ.get("CHROMEDRIVER_PATH")

# Selenium Manager phones home to plausible.io with usage stats by default
# (confirmed live on earlier family members) — opted out the same way
# here, before this module's own SECURITY.md promise is tested.
os.environ.setdefault("SE_AVOID_STATS", "true")

log = logging.getLogger("selenium_scraper")


def _positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return ivalue


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="shein.com fashion listing scraper — Selenium engine",
        epilog="Credentials belong in .env / SHEIN_PROXY / TWOCAPTCHA_KEY — never on this command line.",
    )
    p.add_argument("--url", default=None)
    p.add_argument("--query", default=None)
    p.add_argument("--category", default=None)
    p.add_argument("--sort", choices=sp.SORT_VALUES, default="relevance", help="Recorded on the run only — NOT yet wired into the URL, see shein_parser.py")
    p.add_argument("--max-results", type=_positive_int, default=30)
    p.add_argument("--max-scrolls", type=_positive_int, default=20)
    p.add_argument("--stall-rounds", type=_positive_int, default=4)
    p.add_argument("--scroll-delay", type=float, default=1.5)
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--out", default=None)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--retry-delay", type=float, default=3.0)
    p.add_argument(
        "--block-retries", type=int, default=2,
        help="On a blocked, zero-product outcome, retry this many extra times before giving up — "
             "'retry before you rotate', not a proxy swap (see playwright_scraper.py's own copy of "
             "this help text and README's 'Known limitations' for the sibling-repo evidence this is "
             "based on). Selenium/chromedriver cannot authenticate a remote --cdp-endpoint at all "
             "(see the error above), so unlike Playwright/Puppeteer this only re-runs against the "
             "same local browser + --proxy exit, not a managed session identity.",
    )
    p.add_argument("--proxy", default=None)
    p.add_argument("--proxy-file", default=None)
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=3)
    p.add_argument("--twocaptcha-key", default=None)
    p.add_argument("--captcha-api", default=None, help="Override the 2Captcha API base URL (testing only)")
    p.add_argument("--solve-captcha", choices=["off", "when-blocked", "always"], default="when-blocked")
    p.add_argument("--min-score", type=float, default=0.3, help="Minimum acceptable reCAPTCHA v3 score (2Captcha's minScore task field)")
    p.add_argument("--fingerprint", action="store_true", help="Fetch and apply a 2Captcha Fingerprint API profile's user agent (ignored with --cdp-endpoint — see fingerprint_client.refuse_if_cdp)")
    p.add_argument("--fp-tags", default=None, help="Fingerprint API filter, e.g. 'Windows'")
    p.add_argument("--fp-country", default=None, help="Fingerprint API filter, e.g. 'us'")
    p.add_argument("--cdp-endpoint", default=None,
                    help="NOTE: refused if it carries credentials — Selenium cannot authenticate a remote CDP session")
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
    p.add_argument("--scraper-api-timeout", type=int, default=60, help="Seconds 2Captcha itself waits for the target page to finish loading (1-120, their limit)")
    p.add_argument("--scraper-api-url", default=None, help="Override the Scraper API base URL (testing only)")
    p.add_argument(
        "--scraper-api-cdp", action="store_true",
        help="Route --scraper-api's fetch through a 2Captcha Scraping Browser CDP session (their "
             "'cdpurl' field on the Scraper API task) instead of their own default browser pool — "
             "chaining two 2Captcha products together, not pointing this at a caller-supplied "
             "--cdp-endpoint (that flag stays ignored in --scraper-api mode, see its help text: an "
             "arbitrary CDP session isn't known to support this field the way 2Captcha's own does). "
             "This is what actually gets --scraper-api real captcha auto-solve and exit-country "
             "pinning — see scraper_api_client.TwoCaptchaClient.scraping_browser_connection_url and "
             "scrape_url's own docstrings for exactly what 2Captcha documents. Requires "
             "--scraper-api. WIRED BUT NOT YET LIVE-TESTED: the underlying 'cdpurl' field is "
             "documented by 2Captcha but this codebase had never exercised it before this flag "
             "existed — confirm it live before relying on it (TESTING.md). If the Scraping "
             "Browser session itself fails (a Scraper API HTTP error, not a normal blocked-with-"
             "zero-products outcome), this run automatically falls back to --scraper-api's plain "
             "default pool once, logged loudly, rather than giving up outright — losing country/"
             "profile pinning and 2Captcha's own captcha auto-solve for the rest of that run.",
    )
    p.add_argument("--scraper-api-country", default=None, help="Exit country for --scraper-api-cdp's Scraping Browser session, e.g. 'us' (ignored without --scraper-api-cdp)")
    p.add_argument("--scraper-api-profile-id", default=None, help="Reuse a specific Scraping Browser profile id across runs for --scraper-api-cdp, instead of the default pool (ignored without --scraper-api-cdp; see scraping_browser_connection_url's docstring on why reuse is preferred)")
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--dump-html", action="store_true")
    p.add_argument("--headless", dest="headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    return p


def _default_out(fmt: str) -> str:
    return f"shein_results.{fmt}"


def _resolve_start_url(args: argparse.Namespace):
    if args.url:
        if sp.is_disallowed_path(args.url):
            return None, True
        return args.url, args.url.count("-p-") > 0 and args.url.endswith(".html") and "pdsearch" not in args.url
    if args.query or args.category:
        return sp.search_url(query=args.query, category_path=args.category), False
    return None, False


def _dump_path(out_path: str) -> str:
    stem = Path(out_path).with_suffix("")
    return f"{stem}_debug.html"


def _cdp_endpoint_has_credentials(cdp_endpoint: str) -> bool:
    parts = urlparse(cdp_endpoint)
    return bool(parts.username or parts.password)


def _build_driver(*, headless: bool, proxy: Optional[Proxy], cdp_endpoint: Optional[str], user_agent: Optional[str] = None):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    if _CHROME_BINARY:
        options.binary_location = _CHROME_BINARY
    if user_agent:
        options.add_argument(f"--user-agent={user_agent}")

    if cdp_endpoint:
        options.debugger_address = urlparse(cdp_endpoint).netloc.split("@")[-1]
        return webdriver.Chrome(options=options)

    if proxy is not None:
        if proxy.has_auth:
            log.warning(
                "Selenium's --proxy-server cannot authenticate — using %s:%s "
                "with credentials STRIPPED, not silently dropped.",
                proxy.host, proxy.port,
            )
        options.add_argument(f"--proxy-server={proxy.server_only()}")

    if Service and _CHROMEDRIVER_PATH:
        service = Service(executable_path=_CHROMEDRIVER_PATH)
    elif Service:
        service = Service()  # Selenium Manager: resolves/downloads over the network
    else:
        service = None
    return webdriver.Chrome(service=service, options=options) if service else webdriver.Chrome(options=options)


# See sibling repos' selenium_scraper.py for why this reads the
# Navigation Timing API instead of a driver-native Response object.
_STATUS_JS = (
    "try { return performance.getEntriesByType('navigation')[0].responseStatus || 0; } "
    "catch (e) { return 0; }"
)
_GB_RAW_DATA_JS = "try { return window.gbRawData || null; } catch (e) { return null; }"


def _maybe_solve_captcha(
    *, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str, min_score: float = 0.3,
    driver=None, count_product_links=None,
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
    if action == "warning_no_key":
        log.warning("Captcha solving skipped: %s", result.get("detail"))
    elif action == "warning_solver_error":
        log.warning("Captcha solve failed: %s", result.get("detail"))
    elif action == "solved":
        log.info("Captcha solved via 2Captcha (%s).", result.get("captcha_type"))
        # See captcha_solver.build_injection_script's docstring for the
        # honesty caveat: this uses each widget's own STANDARD, publicly
        # documented convention, never anything confirmed against a real
        # shein.com capture.
        if driver is not None:
            script = build_injection_script(CaptchaType(result["captcha_type"]), result["token"])
            if script is None:
                log.info(
                    "No generic injection point for %s — token was solved but not written into "
                    "the page (this is expected for reCAPTCHA v3; see captcha_solver.py).",
                    result.get("captcha_type"),
                )
            else:
                try:
                    injected = driver.execute_script(f"return {script}")
                    log.info(
                        "Injected solved %s into the page (found a target element/callback: %s) "
                        "— unconfirmed whether shein.com's real widget actually reads this "
                        "standard-convention field/callback.",
                        result.get("captcha_type"), bool(injected),
                    )
                except WebDriverException as exc:
                    log.warning("Captcha solved but injecting it into the page failed: %s", exc)
    elif action == "detected_unidentified_widget":
        log.warning(
            "A bot-mitigation marker was detected but no known widget/sitekey could be extracted "
            "— SHEIN's own risk-gateway family (/risk/challenge, /risk/action/limit) has two "
            "confirmed real incidents with no known 2Captcha automated solve path: the "
            "captcha-shaped /risk/challenge, and /risk/action/limit, which looks like a plain "
            "rate limit with nothing to solve at all (see shein_parser.py's module docstring)."
        )
    return result


def scrape_search(
    *, args: argparse.Namespace, start_url: str,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    user_agent: Optional[str] = None,
) -> Tuple[List[Product], bool, bool, int, bool]:
    blocked = False
    remote_api_error = False
    scroll_error = False

    proxy = proxy_pool.next() if proxy_pool else None
    log.info("Using proxy %s", proxy.masked() if proxy else "(no local proxy pool — direct connection, or a --cdp-endpoint session providing its own exit)")
    driver = _build_driver(headless=args.headless, proxy=proxy, cdp_endpoint=args.cdp_endpoint, user_agent=user_agent)

    last_error = None
    status = None
    for attempt in range(args.retries + 1):
        try:
            driver.set_page_load_timeout(NAV_TIMEOUT_S)
            driver.get(start_url)
            time.sleep(READINESS_WAIT_S)
            try:
                reported = driver.execute_script(_STATUS_JS)
                status = int(reported) if reported else None
            except WebDriverException:
                pass
            if proxy_pool is not None and proxy is not None:
                proxy_pool.report_success(proxy)
            last_error = None
            break
        except WebDriverException as exc:
            message = str(exc)
            last_error = message
            dead = is_proxy_dead_error(message)
            if proxy_pool is not None and proxy is not None and dead:
                proxy_pool.report_failure(proxy, dead=True)
                log.warning("Proxy reported dead: %s", message)
            else:
                log.warning("Navigation attempt %d/%d failed: %s", attempt + 1, args.retries + 1, message)
            if attempt < args.retries:
                time.sleep(args.retry_delay)

    if last_error is not None:
        driver.quit()
        log.error("Search page permanently failed to load: %s", last_error)
        return [], False, True, 0, False

    # REAL, confirmed-live incidents (shein_parser.py's module docstring —
    # TWO distinct endpoints now, a captcha-shaped /risk/challenge and a
    # rate-limit /risk/action/limit): both silently redirect, no >=400
    # status involved — the current URL is the most direct signal.
    try:
        current_url = driver.current_url
    except WebDriverException:
        current_url = start_url
    if any(marker in current_url for marker in sp.RISK_GATEWAY_URL_MARKERS):
        log.warning("Redirected to SHEIN's own risk gateway (%s) — treating as blocked.", current_url)
        blocked = True

    if status is not None and status >= 400:
        log.warning("Search page returned HTTP %d — treating as blocked, not empty.", status)
        blocked = True

    seen_skus: set = set()
    merged: List[Product] = []
    stall = 0
    previous_product_count = -1
    rounds = 0

    for round_num in range(args.max_scrolls + 1):
        rounds = round_num
        html = driver.page_source
        try:
            raw_data = driver.execute_script(_GB_RAW_DATA_JS)
        except WebDriverException:
            raw_data = None
        cards_present = sp.count_result_cards(html) > 0
        # See playwright_scraper.py's own version of this comment (same
        # fix, same live evidence — Roman, 2026-09-22, captcha_type=909):
        # SHEIN's /risk/challenge SSR state embeds its redirect URL as
        # stale TEXT that can outlive the actual gate, so a marker match
        # past round 0 is only trusted when the CURRENT url still shows
        # the gateway too.
        captcha_detected = detect_from_html(html, sp.BOT_CHALLENGE_MARKERS)
        if captcha_detected and round_num > 0:
            try:
                round_url = driver.current_url
            except WebDriverException:
                round_url = start_url
            if not any(marker in round_url for marker in sp.RISK_GATEWAY_URL_MARKERS):
                log.warning(
                    "A bot-mitigation marker matched stale page text, but the page has moved on "
                    "to %s (%s) — not re-flagging this round as a captcha block.",
                    round_url, sp.diagnose_unexpected_page(html),
                )
                captcha_detected = False
        if captcha_detected and not cards_present:
            blocked = True
        captcha_result = None
        if captcha_detected:
            captcha_result = _maybe_solve_captcha(html=html, url=start_url, client=client, policy=args.solve_captcha, min_score=args.min_score, driver=driver)
        if captcha_result and captcha_result.get("action") in ("warning_no_key", "warning_solver_error", "detected_unidentified_widget"):
            if sp.count_result_cards(html) == 0:
                blocked = True

        result = sp.safe_parse_search_results(html, max_results=args.max_results, raw_data=raw_data)
        if result.source_used == "none" and round_num == 0 and not blocked:
            # Parity fix, added alongside Playwright's own version of this
            # warning — this engine had NO diagnostic here at all before.
            # See shein_parser.diagnose_unexpected_page's docstring — a
            # real, live 2026-09-21 case (Roman's own first engine run)
            # hit a page that wasn't blocked, wasn't a parse bug, but
            # genuinely wasn't a search-results page at all.
            try:
                current_url = driver.current_url
            except WebDriverException:
                current_url = start_url
            log.warning(
                "No products recognised on the first render (%s, final URL: %s) — either "
                "this search genuinely has no results, shein_parser.py's window.gbRawData "
                "path needs updating for the current shein.com markup, or shein.com served "
                "a DIFFERENT page than search results for this request (a real, not just "
                "hypothetical, case — see shein_parser.py's module docstring, "
                "'First real engine run' section). Re-run with --dump-html to inspect the "
                "captured page.",
                sp.diagnose_unexpected_page(html), current_url,
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
            # See playwright_scraper.py's identical loop for why this is
            # written defensively around whether scrolling actually grows
            # window.gbRawData with more real products (UNCONFIRMED).
            driver.execute_script(
                "window.scrollBy(0, Math.max(Math.floor(window.innerHeight * 0.8), 600));"
            )
        except WebDriverException as exc:
            log.warning("Scroll failed, stopping pagination early: %s", exc)
            scroll_error = True
            break
        time.sleep(args.scroll_delay)

    final_html = driver.page_source
    try:
        final_raw_data = driver.execute_script(_GB_RAW_DATA_JS)
    except WebDriverException:
        final_raw_data = None
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

    driver.quit()
    return merged, blocked, remote_api_error, rounds, scroll_error


def scrape_product_page(
    *, args: argparse.Namespace, start_url: str,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
    user_agent: Optional[str] = None,
) -> Tuple[List[Product], bool, bool, int, bool]:
    blocked = False
    remote_api_error = False
    proxy = proxy_pool.next() if proxy_pool else None
    driver = _build_driver(headless=args.headless, proxy=proxy, cdp_endpoint=args.cdp_endpoint, user_agent=user_agent)

    last_error = None
    status = None
    for attempt in range(args.retries + 1):
        try:
            driver.set_page_load_timeout(NAV_TIMEOUT_S)
            driver.get(start_url)
            time.sleep(READINESS_WAIT_S)
            try:
                reported = driver.execute_script(_STATUS_JS)
                status = int(reported) if reported else None
            except WebDriverException:
                pass
            last_error = None
            break
        except WebDriverException as exc:
            last_error = str(exc)
            log.warning("Navigation attempt %d/%d failed: %s", attempt + 1, args.retries + 1, last_error)
            if attempt < args.retries:
                time.sleep(args.retry_delay)

    if last_error is not None:
        driver.quit()
        return [], False, True, 0, False

    try:
        current_url = driver.current_url
    except WebDriverException:
        current_url = start_url
    if any(marker in current_url for marker in sp.RISK_GATEWAY_URL_MARKERS):
        blocked = True
    if status is not None and status >= 400:
        blocked = True

    html = driver.page_source
    # Fixed 2026-09-22 (documented in README "Known limitations" as a real,
    # confirmed, not-yet-fixed gap): this path never attempted captcha
    # solving at all — only scrape_search()'s round loop called
    # _maybe_solve_captcha. `count_product_links` here is product-page-
    # shaped (did the page already parse?), not search-page-shaped — see
    # _maybe_solve_captcha's own docstring for why passing the default
    # would be wrong here. (No --cdp-endpoint autosolve equivalent here —
    # Selenium refuses that flag outright, CLAUDE.md §6 — but the classic
    # captcha-API + local-injection path this uses on every other engine's
    # local-browser mode applies here exactly the same way.)
    captcha_result = None
    if detect_from_html(html, sp.BOT_CHALLENGE_MARKERS):
        captcha_result = _maybe_solve_captcha(
            html=html, url=start_url, client=client, policy=args.solve_captcha, min_score=args.min_score,
            driver=driver, count_product_links=lambda h: 1 if sp.parse_product_page(h, url=start_url) else 0,
        )
        if captcha_result and captcha_result.get("action") == "solved":
            # No scroll/round loop here to pick the injection up naturally
            # on a later round the way scrape_search() does — a single
            # re-fetch after a beat is what actually gives this attempt a
            # chance to matter, rather than solving/injecting a token that
            # nothing ever re-reads.
            time.sleep(READINESS_WAIT_S)
            html = driver.page_source
        elif captcha_result and captcha_result.get("action") in ("warning_no_key", "warning_solver_error", "detected_unidentified_widget"):
            if not sp.parse_product_page(html, url=start_url):
                blocked = True

    product = sp.parse_product_page(html, url=start_url)
    if args.dump_html:
        Path(_dump_path(args.out)).write_text(html, encoding="utf-8")
    driver.quit()
    products = [product] if product else []
    if not products and not blocked:
        log.warning("Product page rendered but no ProductGroup/Product JSON-LD was found — see shein_parser.py.")
    return products, blocked, remote_api_error, 0, False


def _scrape_via_scraper_api(
    *, args: argparse.Namespace, start_url: str, is_product_page: bool, client: TwoCaptchaClient,
    cdp_url: Optional[str] = None,
) -> Tuple[List[Product], bool, bool, int, bool]:
    """--scraper-api's own fetch path — see playwright_scraper.py's copy of
    this function for the full rationale (identical logic, duplicated per
    engine per this family's own convention — CLAUDE.md §4). One
    browserless HTTP call, no scroll loop, no live page/DOM. `cdp_url`,
    when --scraper-api-cdp set one (see run()), routes this fetch through
    2Captcha's own Scraping Browser instead of their default pool — real
    captcha auto-solve and country pinning happen on 2Captcha's side of
    that session, nothing here needs to change beyond passing it through."""
    try:
        result = client.scrape_url(start_url, timeout=args.scraper_api_timeout, cdp_url=cdp_url)
    except TwoCaptchaAuthError as exc:
        log.error("Scraper API: %s", exc)
        return [], False, True, 0, False
    except TwoCaptchaError as exc:
        log.error("Scraper API request failed — treating as remote_api_error, not a crash: %s", exc)
        return [], False, True, 0, False

    html = result.body
    blocked = False
    if result.target_status is not None and result.target_status >= 400:
        log.warning("Scraper API: target page returned HTTP %d — treating as blocked.", result.target_status)
        blocked = True
    if detect_from_html(html, sp.BOT_CHALLENGE_MARKERS):
        blocked = True

    if args.dump_html:
        Path(_dump_path(args.out)).write_text(html, encoding="utf-8")

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


def run(args: argparse.Namespace) -> int:
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
        # A whole separate, browserless code path — no Selenium/chromedriver
        # is needed at all here (see CLAUDE.md §6), so none of the checks
        # below (webdriver import, --cdp-endpoint credential shape) apply.
        if not args.twocaptcha_key:
            print("Error: --scraper-api requires --twocaptcha-key/TWOCAPTCHA_KEY", file=sys.stderr)
            return EXIT_BAD_USAGE
        if args.proxy or args.proxy_file or args.cdp_endpoint or args.fingerprint:
            log.warning(
                "--scraper-api ignores --proxy/--proxy-file/--cdp-endpoint/--fingerprint — this mode "
                "brings its own exit IP/device via 2Captcha's own infrastructure, see --scraper-api's "
                "help text."
            )
        if (args.scraper_api_country or args.scraper_api_profile_id) and not args.scraper_api_cdp:
            log.warning(
                "--scraper-api-country/--scraper-api-profile-id are ignored without --scraper-api-cdp "
                "— there is no Scraping Browser session for them to apply to."
            )
        client = TwoCaptchaClient(args.twocaptcha_key, api_base=args.captcha_api, scraper_api_base=args.scraper_api_url)
        cdp_url = None
        if args.scraper_api_cdp:
            cdp_url = client.scraping_browser_connection_url(
                country=args.scraper_api_country, profile_id=args.scraper_api_profile_id,
            )
        blocked = remote_api_error = False
        merged: List[Product] = []
        cdp_fallback_used = False
        for block_attempt in range(args.block_retries + 1):
            merged, blocked, remote_api_error, rounds, scroll_error = _scrape_via_scraper_api(
                args=args, start_url=start_url, is_product_page=is_product_page, client=client,
                cdp_url=cdp_url,
            )
            if remote_api_error and cdp_url is not None and not cdp_fallback_used:
                # See playwright_scraper.py's identical fallback for the
                # full rationale (duplicated per engine per CLAUDE.md §4).
                log.warning(
                    "--scraper-api-cdp's Scraping Browser session failed — falling back to "
                    "--scraper-api's plain default pool for the rest of this run instead of "
                    "giving up outright. This run no longer has --scraper-api-cdp's country/"
                    "profile pinning or 2Captcha's own captcha auto-solve."
                )
                cdp_fallback_used = True
                cdp_url = None
                merged, blocked, remote_api_error, rounds, scroll_error = _scrape_via_scraper_api(
                    args=args, start_url=start_url, is_product_page=is_product_page, client=client,
                    cdp_url=cdp_url,
                )
            if remote_api_error or not (blocked and not merged):
                break
            if block_attempt < args.block_retries:
                log.warning(
                    "Blocked with zero products (Scraper API attempt %d/%d) — retrying the same "
                    "fetch before giving up.",
                    block_attempt + 1, args.block_retries + 1,
                )
                time.sleep(args.retry_delay)
        price_confirmed_pct = (sum(1 for p in merged if p.price is not None) / len(merged)) if merged else None
        return finish_run(
            products=merged, out_path=args.out, fmt=args.format, engine=ENGINE_NAME, url=start_url,
            pages_requested=1, pages_completed=0 if remote_api_error else 1, failed_pages=None,
            blocked=blocked, remote_api_error=remote_api_error, allow_empty=args.allow_empty,
            started_at=started_at, price_confirmed_pct=price_confirmed_pct,
        )

    if args.cdp_endpoint and _cdp_endpoint_has_credentials(args.cdp_endpoint):
        print(
            "Error: --cdp-endpoint carries credentials — Selenium/chromedriver's "
            "debuggerAddress takes a bare host:port and cannot authenticate a "
            "remote session. Use playwright_scraper.py or puppeteer_scraper.py "
            "for the Scraping Browser API.", file=sys.stderr,
        )
        return EXIT_BAD_USAGE
    if webdriver is None:
        print(f"Error: selenium is not installed ({_SELENIUM_IMPORT_ERROR}). "
              f"pip install -r requirements-selenium.txt", file=sys.stderr)
        return EXIT_CRASH

    try:
        proxies = load_proxies(args.proxy, args.proxy_file)
    except ProxyParseError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE
    proxy_pool = ProxyPool(proxies, shuffle=args.proxy_shuffle, block_retries=args.proxy_block_retries) if proxies else None
    if args.cdp_endpoint and proxy_pool is not None:
        log.warning("Ignoring --proxy: a --cdp-endpoint session already carries its own exit IP.")
        proxy_pool = None

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

    try:
        scrape_fn = scrape_product_page if is_product_page else scrape_search
        # "Retry before you rotate" — see playwright_scraper.py's own copy of
        # this comment and --block-retries' help text for the sibling-repo
        # evidence this is based on.
        for block_attempt in range(args.block_retries + 1):
            merged, blocked, remote_api_error, rounds, scroll_error = scrape_fn(
                args=args, start_url=start_url, proxy_pool=proxy_pool, client=client, user_agent=user_agent,
            )
            if not (blocked and not merged):
                break
            if block_attempt < args.block_retries:
                log.warning(
                    "Blocked with zero products (attempt %d/%d) — retrying before giving up.",
                    block_attempt + 1, args.block_retries + 1,
                )
                time.sleep(args.retry_delay)
        price_confirmed_pct = (sum(1 for p in merged if p.price is not None) / len(merged)) if merged else None
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH

    completed_rounds = rounds + 1
    failed_pages = [completed_rounds + 1] if scroll_error else None

    return finish_run(
        products=merged, out_path=args.out, fmt=args.format, engine=ENGINE_NAME, url=start_url,
        pages_requested=args.max_scrolls, pages_completed=completed_rounds, failed_pages=failed_pages,
        blocked=blocked, remote_api_error=remote_api_error, allow_empty=args.allow_empty,
        started_at=started_at, price_confirmed_pct=price_confirmed_pct,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_arg_parser().parse_args()
    args = env_config.apply_env(args)
    try:
        return run(args)
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
