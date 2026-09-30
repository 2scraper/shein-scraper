#!/usr/bin/env python3
"""puppeteer_scraper.py — pyppeteer engine, parity copy of
playwright_scraper.py (same flags, same exit codes and status semantics —
see output_writer.finish_run). Not the primary engine (Playwright is); kept
for parity, and because it — like Playwright, unlike Selenium — CAN open an
authenticated `ws://login:pass@host:port` CDP session, so it is the second
engine able to use the Scraping Browser API's own `Captcha.setAutoSolve`.

Named and shaped like every sibling repo's own `puppeteer_scraper.py`
(Python + pyppeteer, not a separate Node.js file) — this repo follows
that family convention for the same reason: one shared output contract
and one set of family modules across all three engines.

pyppeteer itself is effectively unmaintained (its own README points at
Playwright) — this file exists for parity/completeness, not as a
recommendation to prefer it.

Chromium binary: sourced from `PYPPETEER_EXECUTABLE_PATH` /
`PUPPETEER_EXECUTABLE_PATH` if set (handy for reusing an existing
Playwright/system Chromium instead of pyppeteer's own bundled download),
otherwise pyppeteer's own default.
"""
from __future__ import annotations

import argparse
import random
import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from pyppeteer import connect as pyppeteer_connect
    from pyppeteer import launch as pyppeteer_launch
    from pyppeteer.errors import NetworkError, PageError, TimeoutError as PyppeteerTimeoutError
except ImportError as _IMPORT_ERROR:  # pragma: no cover — exercised by smoke_test's no-engine path
    pyppeteer_launch = None
    pyppeteer_connect = None
    NetworkError = PageError = PyppeteerTimeoutError = Exception
    _PYPPETEER_IMPORT_ERROR = _IMPORT_ERROR
else:
    _PYPPETEER_IMPORT_ERROR = None

import env_config
import scraper_api_client
import shein_challenge
import shein_parser as sp
from captcha_solver import CaptchaType, build_injection_script, detect_from_html, solve_when_blocked
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH, EXIT_REMOTE_API_ERROR, Product, finish_run, sku_key as _sku_key
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from proxy_pool import Proxy, ProxyPool, ProxyParseError, is_proxy_dead_error, load_proxies, redact_credentials
from scraper_api_client import TwoCaptchaAuthError, TwoCaptchaClient, TwoCaptchaError

ENGINE_NAME = "puppeteer"
NAV_TIMEOUT_MS = 30_000
READINESS_WAIT_S = 3.0

log = logging.getLogger("puppeteer_scraper")

_CHROMIUM_EXECUTABLE = os.environ.get("PYPPETEER_EXECUTABLE_PATH") or os.environ.get("PUPPETEER_EXECUTABLE_PATH")


def _positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return ivalue


def _budget(args) -> "shein_challenge.SolveBudget":
    """The run's shared paid-solve budget (created in run(); a fresh one
    for callers that bypass run(), e.g. tests)."""
    if getattr(args, "_solve_budget", None) is None:
        args._solve_budget = shein_challenge.SolveBudget(getattr(args, "max_solves", 8))
    return args._solve_budget


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
        description="shein.com fashion listing scraper — pyppeteer (Puppeteer) engine",
        epilog="Credentials belong in .env / SHEIN_PROXY / TWOCAPTCHA_KEY — never on this command line.",
    )
    p.add_argument("--url", default=None)
    p.add_argument("--query", default=None)
    p.add_argument("--category", default=None)
    p.add_argument("--sort", choices=sp.SORT_VALUES, default="relevance", help="Recorded in the sidecar only — NOT sent to SHEIN yet (its query parameter is unconfirmed, see shein_parser.py); diff_runs refuses runs whose sort differs")
    p.add_argument("--max-results", type=_positive_int, default=30)
    p.add_argument("--max-scrolls", type=_positive_int, default=20)
    p.add_argument("--stall-rounds", type=_positive_int, default=4)
    p.add_argument("--scroll-delay", type=float, default=1.5)
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--out", default=None)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--retry-delay", type=float, default=3.0)
    p.add_argument("--delay-jitter", type=float, default=0.3, help="Relative +/-jitter applied to --scroll-delay/--retry-delay/--rate-limit-cooldown so repeated waits are not perfectly periodic (0 disables, e.g. for reproducible tests)")
    p.add_argument("--rate-limit-cooldown", type=float, default=0.0, help="On SHEIN's own rate-limit gate (/risk/action/limit), wait this many seconds and retry ONCE on the same session before giving up, honoring the ~5 minute cooldown observed live (see TESTING.md). Off (0) by default — this can make a single invocation take minutes; consider e.g. 300 for unattended/scheduled runs.")
    p.add_argument(
        "--block-retries", type=int, default=2,
        help="On a blocked, zero-product outcome, retry this many extra times before giving up — "
             "'retry before you rotate', not a proxy/session swap (see playwright_scraper.py's own "
             "copy of this help text and README's 'Known limitations' for the sibling-repo evidence "
             "this is based on). Each retry here re-launches/re-connects via _launch(), including a "
             "fresh connect() to the same --cdp-endpoint ws:// URL for the CDP case — whether that "
             "reuses the SAME managed session or gets a new one depends on the provider's own lease "
             "semantics, unconfirmed either way for the Scraping Browser API specifically.",
    )
    p.add_argument(
        "--risk-challenge-rounds", type=int, default=5,
        help="On SHEIN's own /risk/challenge gateway, click its 'I am human' checkbox and solve up to "
             "this many rounds of its 3x3 image grid via 2Captcha GridTask (needs TWOCAPTCHA_KEY; the "
             "checkbox step alone needs no key). 0 disables. Skipped with --solve-captcha off. See "
             "shein_challenge.py.",
    )
    p.add_argument(
        "--max-solves", type=int, default=8,
        help="Cap on PAID 2Captcha solves for the whole run, across every challenge round, block "
             "retry and scroll round (0 = never pay). Recorded as solves_spent in the sidecar.",
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
    p.add_argument("--cdp-endpoint", default=None)
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


async def _launch(*, headless: bool, proxy: Optional[Proxy], cdp_endpoint: Optional[str]):
    if cdp_endpoint:
        # pyppeteer's connect() has no timeout of its own and never resolves on
        # a refused handshake — connect_with_retry bounds every attempt.
        return await scraper_api_client.connect_with_retry(
            lambda: pyppeteer_connect(browserWSEndpoint=cdp_endpoint, defaultViewport=None),
            redact=redact_credentials, log=log,
        )
    args = ["--no-sandbox", "--disable-dev-shm-usage"]
    if proxy is not None:
        args.append(proxy.pyppeteer_launch_arg())
    kwargs = dict(headless=headless, args=args)
    if _CHROMIUM_EXECUTABLE:
        kwargs["executablePath"] = _CHROMIUM_EXECUTABLE
    return await pyppeteer_launch(**kwargs)


async def _authenticate_if_needed(page, proxy: Optional[Proxy]) -> None:
    if proxy is not None:
        auth = proxy.pyppeteer_auth_dict()
        if auth:
            await page.authenticate(auth)


async def _enable_scraping_browser_auto_solve(page) -> None:
    try:
        client = await page.target.createCDPSession()
        client.on("Captcha.detected", lambda *_: log.info("[Scraping Browser API] captcha detected"))
        client.on("Captcha.solveFinished", lambda *_: log.info("[Scraping Browser API] captcha solved"))
        client.on("Captcha.solveFailed", lambda *_: log.warning("[Scraping Browser API] captcha solve failed"))
        await client.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
    except Exception as exc:  # noqa: BLE001 — optional enhancement, never fatal
        log.warning("Captcha.setAutoSolve unavailable on this CDP session (continuing without it): %s", exc)


async def _maybe_solve_captcha(
    *, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str, min_score: float = 0.3, args=None,
    page=None, count_product_links=None,
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
    budget = _budget(args) if args is not None else None
    if budget is not None and budget.remaining() == 0:
        log.warning("Captcha solving skipped: the run's solve budget is spent (--max-solves %d).", budget.limit)
        return None
    result = solve_when_blocked(
        client=client, page_url=url, html=html, count_product_links=count_product_links or sp.count_result_cards,
        extra_markers=sp.BOT_CHALLENGE_MARKERS, min_score=min_score,
    )
    if budget is not None and result.get("action") in ("solved", "warning_solver_error"):
        budget.try_spend()  # a task was created and billed, whatever came back
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
            "rate limit with nothing to solve at all (see shein_parser.py's module docstring)."
        )
    return result


class _PyppeteerChallengeDriver:
    """shein_challenge.ChallengeDriver over a pyppeteer page."""

    def __init__(self, page):
        self.page = page

    async def url(self) -> str:
        return self.page.url

    async def state(self) -> dict:
        return await self.page.evaluate(shein_challenge.STATE_JS) or {}

    async def screenshot(self, clip: dict) -> bytes:
        return await self.page.screenshot({"type": "png", "clip": clip})

    async def click(self, x: float, y: float) -> None:
        mouse = self.page.mouse
        await mouse.move(x + random.uniform(-60, 60), y + random.uniform(-60, 60), {"steps": random.randint(8, 14)})
        await mouse.move(x, y, {"steps": random.randint(10, 18)})
        await asyncio.sleep(random.uniform(0.12, 0.3))
        await mouse.down()
        await asyncio.sleep(random.uniform(0.06, 0.13))
        await mouse.up()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def _challenge_debug_dir(args: argparse.Namespace) -> Optional[str]:
    return str(Path(args.out).with_suffix("")) + "_challenge" if args.dump_html else None


async def _maybe_pass_risk_challenge(page, args: argparse.Namespace, client: Optional[TwoCaptchaClient]) -> bool:
    """True when the page has left SHEIN's /risk/challenge gateway — see
    playwright_scraper._maybe_pass_risk_challenge."""
    if not shein_challenge.on_challenge(page.url) or args.solve_captcha == "off" or args.risk_challenge_rounds <= 0:
        return False
    outcome = await shein_challenge.pass_risk_challenge(
        _PyppeteerChallengeDriver(page), client, max_rounds=args.risk_challenge_rounds, budget=_budget(args),
        debug_dir=_challenge_debug_dir(args),
    )
    if outcome.passed:
        log.info("Passed SHEIN's risk challenge (%s; %d GridTask solve(s)).", outcome.detail, outcome.solves)
        await asyncio.sleep(READINESS_WAIT_S)
    else:
        log.warning("SHEIN risk challenge not passed: %s", outcome.detail)
    return outcome.passed


async def scrape_search(
    *, args: argparse.Namespace, start_url: str,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient], autosolve: bool = False,
    user_agent: Optional[str] = None,
) -> Tuple[List[Product], bool, bool, int, bool]:
    blocked = False
    remote_api_error = False
    scroll_error = False

    proxy = proxy_pool.next() if proxy_pool else None
    log.info("Using proxy %s", proxy.masked() if proxy else "(no local proxy pool — direct connection, or a --cdp-endpoint session providing its own exit)")
    try:
        browser = await _launch(headless=args.headless, proxy=proxy, cdp_endpoint=args.cdp_endpoint)
    except RuntimeError as exc:
        log.error("CDP connection failed — treating as remote_api_error, not a crash: %s", exc)
        return [], False, True, 0, False
    page = await browser.newPage()
    if user_agent:
        await page.setUserAgent(user_agent)
    await _authenticate_if_needed(page, proxy)
    if autosolve:
        await _enable_scraping_browser_auto_solve(page)

    last_error = None
    status = None
    for attempt in range(args.retries + 1):
        try:
            response = await page.goto(start_url, {"waitUntil": "domcontentloaded", "timeout": NAV_TIMEOUT_MS})
            await asyncio.sleep(READINESS_WAIT_S)
            status = response.status if response is not None else None
            if proxy_pool is not None and proxy is not None:
                proxy_pool.report_success(proxy)
            last_error = None
            break
        except (NetworkError, PageError, PyppeteerTimeoutError, Exception) as exc:  # noqa: BLE001
            message = str(exc)
            last_error = message
            dead = is_proxy_dead_error(message)
            if proxy_pool is not None and proxy is not None and dead:
                proxy_pool.report_failure(proxy, dead=True)
                log.warning("Proxy reported dead: %s", message)
            else:
                log.warning("Navigation attempt %d/%d failed: %s", attempt + 1, args.retries + 1, message)
            if attempt < args.retries:
                await asyncio.sleep(_jittered_delay(args.retry_delay, args.delay_jitter))

    if last_error is not None:
        await browser.close()
        log.error("Search page permanently failed to load: %s", last_error)
        return [], False, True, 0, False

    # REAL, confirmed-live incidents (shein_parser.py's module docstring —
    # TWO distinct endpoints now, a captcha-shaped /risk/challenge and a
    # rate-limit /risk/action/limit): both silently redirect, no >=400
    # status involved — the current URL is the most direct signal.
    if "/risk/action/limit" in page.url:
        args._rate_limited = True
    if await _maybe_pass_risk_challenge(page, args, client):
        status = None
    if any(marker in page.url for marker in sp.RISK_GATEWAY_URL_MARKERS):
        log.warning("Redirected to SHEIN's own risk gateway (%s) — treating as blocked.", page.url)
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
        if args._rate_limited:
            break
        html = await page.content()
        try:
            raw_data = await page.evaluate("() => window.gbRawData || null")
        except Exception:  # noqa: BLE001
            raw_data = None
        cards_present = sp.count_result_cards(html) > 0
        # See playwright_scraper.py's own version of this comment (same
        # fix, same live evidence — Roman, 2026-09-22, captcha_type=909):
        # SHEIN's /risk/challenge SSR state embeds its redirect URL as
        # stale TEXT that can outlive the actual gate, so a marker match
        # past round 0 is only trusted when the CURRENT url still shows
        # the gateway too.
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
            captcha_result = await _maybe_solve_captcha(html=html, url=start_url, client=client, policy=args.solve_captcha, min_score=args.min_score, page=page, args=args)
        if captcha_result and captcha_result.get("action") in ("warning_no_key", "warning_solver_error", "detected_unidentified_widget"):
            if sp.count_result_cards(html) == 0:
                blocked = True

        result = sp.safe_parse_search_results(html, max_results=args.max_results, raw_data=raw_data)
        args._rejected_rows = max(getattr(args, "_rejected_rows", 0), result.rejected_rows)
        if result.source_used == "none" and round_num == 0 and not blocked:
            # Parity fix, added alongside Playwright's own version of this
            # warning — this engine had NO diagnostic here at all before.
            # See shein_parser.diagnose_unexpected_page's docstring — a
            # real, live 2026-09-21 case (Roman's own first engine run)
            # hit a page that wasn't blocked, wasn't a parse bug, but
            # genuinely wasn't a search-results page at all.
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
            # See playwright_scraper.py's identical loop for why this is
            # written defensively around whether scrolling actually grows
            # window.gbRawData with more real products (UNCONFIRMED).
            await page.evaluate(
                "() => window.scrollBy(0, Math.max(Math.floor(window.innerHeight * 0.8), 600))"
            )
        except Exception as exc:  # noqa: BLE001 — a scroll failure ends the loop, not the run
            log.warning("Scroll failed, stopping pagination early: %s", exc)
            scroll_error = True
            break
        await asyncio.sleep(_jittered_delay(args.scroll_delay, args.delay_jitter))

    final_html = await page.content()
    try:
        final_raw_data = await page.evaluate("() => window.gbRawData || null")
    except Exception:  # noqa: BLE001
        final_raw_data = None
    total_available = sp.total_result_count(final_html, raw_data=final_raw_data)
    args._total_results = total_available
    expected = min(total_available, args.max_results) if total_available is not None else None
    if expected is not None and len(merged) < expected:
        log.warning(
            "Fewer products collected than SHEIN reports available: reports %d, requested %d, collected %d.",
            total_available, args.max_results, len(merged),
        )
        scroll_error = True

    if args.dump_html:
        Path(_dump_path(args.out)).write_text(final_html, encoding="utf-8")

    await browser.close()
    return merged, blocked, remote_api_error, rounds, scroll_error


async def scrape_product_page(
    *, args: argparse.Namespace, start_url: str,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient], autosolve: bool = False,
    user_agent: Optional[str] = None,
) -> Tuple[List[Product], bool, bool, int, bool]:
    blocked = False
    remote_api_error = False
    proxy = proxy_pool.next() if proxy_pool else None
    try:
        browser = await _launch(headless=args.headless, proxy=proxy, cdp_endpoint=args.cdp_endpoint)
    except RuntimeError as exc:
        log.error("CDP connection failed — treating as remote_api_error, not a crash: %s", exc)
        return [], False, True, 0, False
    page = await browser.newPage()
    if user_agent:
        await page.setUserAgent(user_agent)
    await _authenticate_if_needed(page, proxy)
    if autosolve:
        # Real gap found 2026-09-22 (Roman asked for captcha auto-solve to be
        # armed on EVERY page this engine touches, not just the search entry
        # point): scrape_search() below already calls this, but this
        # product-page path never did — a direct `--url <product page>` run
        # over --cdp-endpoint silently never armed Captcha.setAutoSolve, the
        # one asymmetry playwright_scraper.py did NOT have (it arms both its
        # scrape_search AND scrape_product_page call sites). Fixed to match.
        await _enable_scraping_browser_auto_solve(page)

    last_error = None
    status = None
    for attempt in range(args.retries + 1):
        try:
            response = await page.goto(start_url, {"waitUntil": "domcontentloaded", "timeout": NAV_TIMEOUT_MS})
            await asyncio.sleep(READINESS_WAIT_S)
            status = response.status if response is not None else None
            last_error = None
            break
        except (NetworkError, PageError, PyppeteerTimeoutError, Exception) as exc:  # noqa: BLE001
            last_error = str(exc)
            log.warning("Navigation attempt %d/%d failed: %s", attempt + 1, args.retries + 1, last_error)
            if attempt < args.retries:
                await asyncio.sleep(_jittered_delay(args.retry_delay, args.delay_jitter))

    if last_error is not None:
        await browser.close()
        return [], False, True, 0, False

    if "/risk/action/limit" in page.url:
        args._rate_limited = True
    if await _maybe_pass_risk_challenge(page, args, client):
        status = None
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
        args=args)
        if captcha_result and captcha_result.get("action") == "solved":
            # No scroll/round loop here to pick the injection up naturally
            # on a later round the way scrape_search() does — a single
            # re-fetch after a beat is what actually gives this attempt a
            # chance to matter, rather than solving/injecting a token that
            # nothing ever re-reads.
            await asyncio.sleep(READINESS_WAIT_S)
            html = await page.content()
        elif captcha_result and captcha_result.get("action") in ("warning_no_key", "warning_solver_error", "detected_unidentified_widget"):
            if not sp.parse_product_page(html, url=start_url):
                blocked = True

    product = sp.parse_product_page(html, url=start_url)
    if args.dump_html:
        Path(_dump_path(args.out)).write_text(html, encoding="utf-8")
    await browser.close()
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
    browserless HTTP call (plain `requests`, not pyppeteer — nothing here
    needs `await`), no scroll loop, no live page/DOM. `cdp_url`, when
    --scraper-api-cdp set one (see run()), routes this fetch through
    2Captcha's own Scraping Browser instead of their default pool — real
    captcha auto-solve and the selected account's proxy settings apply on 2Captcha's side of
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
    if "/risk/action/limit" in html:
        args._rate_limited = True
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
    args._rejected_rows = max(getattr(args, "_rejected_rows", 0), parsed.rejected_rows)
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
    args._rejected_rows = 0
    args._total_results = None
    args._solve_budget = shein_challenge.SolveBudget(args.max_solves)
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
        # A whole separate, browserless code path — no pyppeteer is needed
        # at all here (see CLAUDE.md §6), so the pyppeteer_launch-is-None
        # check below is skipped entirely.
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
        # A counter, not range(): the one --rate-limit-cooldown retry must not
        # spend a --block-retries attempt (audit 2026-09-30: with --block-retries 0
        # the promised retry never ran, because `continue` ended the loop).
        block_attempt = 0
        while True:
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
                    await asyncio.sleep(_wait_s)
                    continue
                log.warning("SHEIN rate limit reached; stopping this run without block retries. Try again after at least five minutes.")
                break
            if remote_api_error or not (blocked and not merged):
                break
            if block_attempt >= args.block_retries:
                break
            block_attempt += 1
            log.warning(
                "Blocked with zero products (Scraper API attempt %d/%d) — retrying the same "
                "fetch before giving up.",
                block_attempt, args.block_retries + 1,
            )
            await asyncio.sleep(_jittered_delay(args.retry_delay, args.delay_jitter))
        price_confirmed_pct = (sum(1 for p in merged if p.price is not None) / len(merged)) if merged else None
        return finish_run(
            products=merged, out_path=args.out, fmt=args.format, engine=ENGINE_NAME, url=start_url,
            pages_requested=1, pages_completed=0 if remote_api_error else 1, failed_pages=None,
            blocked=blocked, remote_api_error=remote_api_error, allow_empty=args.allow_empty,
            started_at=started_at, rejected_rows=getattr(args, "_rejected_rows", 0), max_results=args.max_results, extra_meta={"solves_spent": _budget(args).spent, "sort": getattr(args, "sort", None)},
            rate_limited=bool(getattr(args, "_rate_limited", False)), total_results=getattr(args, "_total_results", None), price_confirmed_pct=price_confirmed_pct,
        )

    if pyppeteer_launch is None:
        print(f"Error: pyppeteer is not installed ({_PYPPETEER_IMPORT_ERROR}). "
              f"pip install -r requirements-puppeteer.txt", file=sys.stderr)
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
    autosolve = bool(args.cdp_endpoint) and args.solve_captcha != "off"

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
        # A counter, not range(): the one --rate-limit-cooldown retry must not
        # spend a --block-retries attempt (audit 2026-09-30: with --block-retries 0
        # the promised retry never ran, because `continue` ended the loop).
        block_attempt = 0
        while True:
            merged, blocked, remote_api_error, rounds, scroll_error = await scrape_fn(
                args=args, start_url=start_url, proxy_pool=proxy_pool, client=client, autosolve=autosolve,
                user_agent=user_agent,
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
            if block_attempt >= args.block_retries:
                break
            block_attempt += 1
            log.warning(
                "Blocked with zero products (attempt %d/%d) — retrying before giving up.",
                block_attempt, args.block_retries + 1,
            )
            await asyncio.sleep(_jittered_delay(args.retry_delay, args.delay_jitter))
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
        started_at=started_at, rejected_rows=getattr(args, "_rejected_rows", 0), max_results=args.max_results, extra_meta={"solves_spent": _budget(args).spent, "sort": getattr(args, "sort", None)},
            rate_limited=bool(getattr(args, "_rate_limited", False)), total_results=getattr(args, "_total_results", None), price_confirmed_pct=price_confirmed_pct,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_arg_parser().parse_args()
    args = env_config.apply_env(args)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
