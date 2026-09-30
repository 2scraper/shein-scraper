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
from typing import Optional

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
import page_flow
import shein_challenge
import shein_parser as sp
from captcha_solver import CaptchaType, build_injection_script, solve_when_blocked
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from proxy_pool import Proxy, ProxyPool, ProxyParseError, load_proxies, redact_credentials
from scraper_api_client import TwoCaptchaClient

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


_jittered_delay = page_flow.jittered_delay  # the one implementation is page_flow's


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
        log.info("Passed SHEIN's risk challenge (%s; %d paid solve(s)).", outcome.detail, outcome.solves)
        await asyncio.sleep(READINESS_WAIT_S)
    else:
        log.warning("SHEIN risk challenge not passed: %s", outcome.detail)
    return outcome.passed


class _PyppeteerSession:
    """page_flow.PageSession over one pyppeteer page. A local session owns
    its browser (one launch per attempt, on that attempt's proxy — a
    rotation is a fresh browser, CLAUDE.md §8); a --cdp-endpoint session
    borrows the run's single connection and never closes it."""

    def __init__(self, page, browser, *, owns_browser: bool, args: argparse.Namespace):
        self.page, self.browser, self.owns_browser, self.args = page, browser, owns_browser, args

    async def goto(self, url: str) -> Optional[int]:
        response = await self.page.goto(url, {"waitUntil": "domcontentloaded", "timeout": NAV_TIMEOUT_MS})
        await asyncio.sleep(READINESS_WAIT_S)
        return response.status if response is not None else None

    async def url(self) -> str:
        return self.page.url

    async def content(self) -> str:
        return await self.page.content()

    async def gb_raw_data(self) -> Optional[dict]:
        try:
            return await self.page.evaluate("() => window.gbRawData || null")
        except Exception as exc:  # noqa: BLE001 — falls back to HTML extraction
            log.debug("gbRawData read failed, will fall back to HTML extraction: %s", exc)
            return None

    async def scroll(self) -> None:
        await self.page.evaluate("() => window.scrollBy(0, Math.max(Math.floor(window.innerHeight * 0.8), 600))")

    async def pass_risk_challenge(self, client: Optional[TwoCaptchaClient]) -> bool:
        return await _maybe_pass_risk_challenge(self.page, self.args, client)

    async def solve_captcha(self, *, html: str, url: str, client: Optional[TwoCaptchaClient], count_product_links=None):
        return await _maybe_solve_captcha(html=html, url=url, client=client, policy=self.args.solve_captcha,
                                          min_score=self.args.min_score, page=self.page,
                                          count_product_links=count_product_links, args=self.args)

    async def close(self) -> None:
        try:
            await self.page.close()
        except Exception:  # noqa: BLE001 — cleanup only
            pass
        if self.owns_browser:
            try:
                await self.browser.close()
            except Exception:  # noqa: BLE001
                pass


class _PyppeteerEngine:
    """page_flow.Engine for pyppeteer."""

    name = ENGINE_NAME
    readiness_s = READINESS_WAIT_S

    def __init__(self, args: argparse.Namespace, *, remote_browser, autosolve: bool, user_agent: Optional[str]):
        self.args, self.remote_browser, self.autosolve, self.user_agent = args, remote_browser, autosolve, user_agent

    async def open(self, proxy) -> _PyppeteerSession:
        browser = self.remote_browser or await _launch(headless=self.args.headless, proxy=proxy, cdp_endpoint=None)
        page = await browser.newPage()
        if self.user_agent:
            await page.setUserAgent(self.user_agent)
        await _authenticate_if_needed(page, proxy)
        if self.autosolve:
            await _enable_scraping_browser_auto_solve(page)
        return _PyppeteerSession(page, browser, owns_browser=self.remote_browser is None, args=self.args)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


_ORPHAN_CONNECT_ERRORS = ("InvalidStatusCode", "InvalidStatus", "InvalidHandshake", "AbortHandshake")


def _quiet_orphaned_connect(loop, context) -> None:
    """A refused CDP handshake leaves pyppeteer's own connect task failing
    after connect_with_retry has already moved on (CLAUDE.md §26: "an
    orphaned-task traceback after a correct exit"). Only that shape is
    silenced; everything else reaches the default handler."""
    exc = context.get("exception")
    if exc is not None and type(exc).__name__ in _ORPHAN_CONNECT_ERRORS:
        log.debug("orphaned CDP connect task: %s", exc)
        return
    loop.default_exception_handler(context)


async def run(args: argparse.Namespace) -> int:
    asyncio.get_running_loop().set_exception_handler(_quiet_orphaned_connect)
    page_flow.init_run_state(args, shein_challenge.SolveBudget)
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
        if not args.twocaptcha_key:
            print("Error: --scraper-api requires --twocaptcha-key/TWOCAPTCHA_KEY", file=sys.stderr)
            return EXIT_BAD_USAGE
        return await page_flow.run_scraper_api(args, start_url=start_url, is_product_page=is_product_page,
                                               engine_name=ENGINE_NAME, started_at=started_at)

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

    user_agent = None
    if args.fingerprint and not refuse_if_cdp(args.cdp_endpoint):
        if client is None:
            log.warning("--fingerprint requested but no --twocaptcha-key/TWOCAPTCHA_KEY set — continuing without one.")
        else:
            profile = fetch_fingerprint(client, tags=args.fp_tags, country=args.fp_country)
            if profile:
                user_agent = user_agent_from(profile)

    remote_browser = None
    try:
        if args.cdp_endpoint:
            # ONE connection for the whole run: reconnecting per block retry
            # (as the old per-attempt launch did) hits the profile lock.
            try:
                remote_browser = await _launch(headless=args.headless, proxy=None, cdp_endpoint=args.cdp_endpoint)
            except RuntimeError as exc:
                log.error("CDP connection failed — treating as remote_api_error, not a crash: %s", exc)
                return page_flow.finish(args, merged=[], blocked=False, remote_api_error=True,
                                        engine_name=ENGINE_NAME, start_url=start_url, started_at=started_at,
                                        pages_requested=args.max_scrolls, pages_completed=1, failed_pages=None)
        engine = _PyppeteerEngine(args, remote_browser=remote_browser,
                                  autosolve=bool(args.cdp_endpoint) and args.solve_captcha != "off", user_agent=user_agent)
        return await page_flow.run_browser(engine, args, start_url=start_url, is_product_page=is_product_page,
                                           proxy_pool=proxy_pool, client=client, started_at=started_at)
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH
    finally:
        if remote_browser is not None:
            # disconnect(), never close(): close() ends the remote Browser
            # API session itself (found on perplexity-scraper, same driver).
            try:
                await remote_browser.disconnect()
            except Exception:  # noqa: BLE001 — cleanup only
                pass


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
