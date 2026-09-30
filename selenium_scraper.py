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
import asyncio
import base64
import random
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.actions.action_builder import ActionBuilder
except ImportError as _IMPORT_ERROR:  # pragma: no cover — exercised by smoke_test's no-engine path
    webdriver = None
    WebDriverException = Exception
    Options = None
    Service = None
    ActionBuilder = None
    _SELENIUM_IMPORT_ERROR = _IMPORT_ERROR
else:
    _SELENIUM_IMPORT_ERROR = None

import env_config
import page_flow
import shein_challenge
import shein_parser as sp
from captcha_solver import CaptchaType, build_injection_script, solve_when_blocked
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH
from proxy_pool import Proxy, ProxyPool, ProxyParseError, load_proxies
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from scraper_api_client import TwoCaptchaClient

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


def _budget(args) -> "shein_challenge.SolveBudget":
    """The run's shared paid-solve budget (created in run(); a fresh one
    for callers that bypass run(), e.g. tests)."""
    if getattr(args, "_solve_budget", None) is None:
        args._solve_budget = shein_challenge.SolveBudget(getattr(args, "max_solves", 8))
    return args._solve_budget


_jittered_delay = page_flow.jittered_delay  # the one implementation is page_flow's


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="shein.com fashion listing scraper — Selenium engine",
        epilog="Credentials belong in .env / SHEIN_PROXY / TWOCAPTCHA_KEY — never on this command line.",
    )
    p.add_argument("--url", default=None)
    p.add_argument("--query", default=None)
    p.add_argument("--category", default=None)
    p.add_argument("--sort", choices=sp.SORT_VALUES, default="relevance", help='Recorded in the results (.meta.json) only; not sent to SHEIN yet, its URL parameter is unconfirmed. diff_runs refuses runs with different sorts')
    p.add_argument("--max-results", type=_positive_int, default=30)
    p.add_argument("--max-scrolls", type=_positive_int, default=20)
    p.add_argument("--stall-rounds", type=_positive_int, default=4)
    p.add_argument("--scroll-delay", type=float, default=1.5)
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--out", default=None)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--retry-delay", type=float, default=3.0)
    p.add_argument("--delay-jitter", type=float, default=0.3, help="Relative +/-jitter applied to --scroll-delay/--retry-delay/--rate-limit-cooldown so repeated waits are not perfectly periodic (0 disables, e.g. for reproducible tests)")
    p.add_argument("--rate-limit-cooldown", type=float, default=0.0, help="After SHEIN's rate limit (/risk/action/limit), wait this many seconds and retry once on the same session (0 = off; e.g. 300 for scheduled runs)")
    p.add_argument(
        "--block-retries", type=int, default=2,
        help='Retries on the SAME browser session after a blocked page with no products, before giving up (a different proxy/profile is your call between runs)',
    )
    p.add_argument(
        "--risk-challenge-rounds", type=int, default=5,
        help="Puzzle rounds per SHEIN /risk/challenge: the 'I am human' checkbox is free; the image grid and icon puzzle are solved via 2Captcha (needs TWOCAPTCHA_KEY). 0 = don't solve",
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
    p.add_argument("--fingerprint", action="store_true", help='Apply a 2Captcha Fingerprint API user agent to a local browser (ignored with --cdp-endpoint, which brings its own)')
    p.add_argument("--fp-tags", default=None, help="Fingerprint API filter, e.g. 'Windows'")
    p.add_argument("--fp-country", default=None, help="Fingerprint API filter, e.g. 'us'")
    p.add_argument("--cdp-endpoint", default=None,
                    help='Connect to a 2Captcha Scraping Browser API profile (ws://...) instead of launching a local browser (or set SHEIN_CDP_ENDPOINT)')
    p.add_argument(
        "--scraper-api", action="store_true",
        help="Fetch through 2Captcha's Scraper API instead of a browser: one page per run, no scrolling, no local captcha solving (needs TWOCAPTCHA_KEY). --proxy/--cdp-endpoint/--fingerprint are ignored",
    )
    p.add_argument("--scraper-api-timeout", type=int, default=60, help="Seconds 2Captcha itself waits for the target page to finish loading (1-120, their limit)")
    p.add_argument("--scraper-api-url", default=None, help="Override the Scraper API base URL (testing only)")
    p.add_argument(
        "--scraper-api-cdp", action="store_true",
        help='With --scraper-api: route the fetch through a Scraping Browser profile (its captcha auto-solve applies). Falls back once to the plain pool if that session fails',
    )
    p.add_argument("--scraper-api-country", default=None, help='With --scraper-api-cdp: use an existing Browser API account configured for this country, e.g. us')
    p.add_argument("--scraper-api-account-id", type=_positive_int, default=None, help="Existing 2Captcha Browser API account ID for --scraper-api-cdp; required when multiple accounts match the requested country")
    p.add_argument("--scraper-api-profile-id", default=None, help='With --scraper-api-cdp: reuse this Scraping Browser profile across runs (recommended; a fresh profile is challenged more)')
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
    *, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str, min_score: float = 0.3, args=None,
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


class _SeleniumChallengeDriver:
    """shein_challenge.ChallengeDriver over a (sync) Selenium driver —
    each call is a plain blocking WebDriver call wrapped in a coroutine,
    run under asyncio.run() by _maybe_pass_risk_challenge. Screenshots use
    Chrome's own CDP Page.captureScreenshot for its `clip` support (every
    driver this engine builds is Chrome/Chromium)."""

    def __init__(self, driver):
        self.driver = driver

    async def url(self) -> str:
        return self.driver.current_url

    async def state(self) -> dict:
        return self.driver.execute_script(f"return ({shein_challenge.STATE_JS})();") or {}

    async def screenshot(self, clip: dict) -> bytes:
        shot = self.driver.execute_cdp_cmd(
            "Page.captureScreenshot", {"format": "png", "clip": {**clip, "scale": 1}},
        )
        return base64.b64decode(shot["data"])

    async def click(self, x: float, y: float) -> None:
        builder = ActionBuilder(self.driver)
        pointer = builder.pointer_action
        pointer.move_to_location(max(0, int(x + random.uniform(-60, 60))), max(0, int(y + random.uniform(-60, 60))))
        pointer.pause(random.uniform(0.15, 0.3))
        pointer.move_to_location(int(x), int(y))
        pointer.pause(random.uniform(0.12, 0.3))
        pointer.pointer_down()
        pointer.pause(random.uniform(0.06, 0.13))
        pointer.pointer_up()
        builder.perform()

    async def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def _challenge_debug_dir(args: argparse.Namespace) -> Optional[str]:
    return str(Path(args.out).with_suffix("")) + "_challenge" if args.dump_html else None


async def _pass_risk_challenge_async(driver, args: argparse.Namespace, client: Optional[TwoCaptchaClient]) -> bool:
    """True when the page has left SHEIN's /risk/challenge gateway — see
    playwright_scraper._maybe_pass_risk_challenge. Async so the shared
    page_flow loop can await it (the sync wrapper below is for callers
    outside a running loop)."""
    try:
        current_url = driver.current_url
    except WebDriverException:
        return False
    if not shein_challenge.on_challenge(current_url) or args.solve_captcha == "off" or args.risk_challenge_rounds <= 0:
        return False
    outcome = await shein_challenge.pass_risk_challenge(
        _SeleniumChallengeDriver(driver), client, max_rounds=args.risk_challenge_rounds, budget=_budget(args),
        debug_dir=_challenge_debug_dir(args),
    )
    if outcome.passed:
        log.info("Passed SHEIN's risk challenge (%s; %d paid solve(s)).", outcome.detail, outcome.solves)
        time.sleep(READINESS_WAIT_S)
    else:
        log.warning("SHEIN risk challenge not passed: %s", outcome.detail)
    return outcome.passed


def _maybe_pass_risk_challenge(driver, args: argparse.Namespace, client: Optional[TwoCaptchaClient]) -> bool:
    return asyncio.run(_pass_risk_challenge_async(driver, args, client))


class _SeleniumSession:
    """page_flow.PageSession over one Selenium driver (sync WebDriver calls
    inside coroutines; the shared loop runs under asyncio.run in run())."""

    def __init__(self, driver, args: argparse.Namespace):
        self.driver, self.args = driver, args

    async def goto(self, url: str) -> Optional[int]:
        self.driver.set_page_load_timeout(NAV_TIMEOUT_S)
        self.driver.get(url)
        time.sleep(READINESS_WAIT_S)
        try:
            reported = self.driver.execute_script(_STATUS_JS)
            return int(reported) if reported else None
        except WebDriverException:
            return None

    async def url(self) -> str:
        try:
            return self.driver.current_url
        except WebDriverException:
            return ""

    async def content(self) -> str:
        return self.driver.page_source

    async def gb_raw_data(self) -> Optional[dict]:
        try:
            return self.driver.execute_script(_GB_RAW_DATA_JS)
        except WebDriverException as exc:
            log.debug("gbRawData read failed, will fall back to HTML extraction: %s", exc)
            return None

    async def scroll(self) -> None:
        self.driver.execute_script("window.scrollBy(0, Math.max(Math.floor(window.innerHeight * 0.8), 600));")

    async def pass_risk_challenge(self, client: Optional[TwoCaptchaClient]) -> bool:
        return await _pass_risk_challenge_async(self.driver, self.args, client)

    async def solve_captcha(self, *, html: str, url: str, client: Optional[TwoCaptchaClient], count_product_links=None):
        return _maybe_solve_captcha(html=html, url=url, client=client, policy=self.args.solve_captcha,
                                    min_score=self.args.min_score, driver=self.driver,
                                    count_product_links=count_product_links, args=self.args)

    async def close(self) -> None:
        try:
            self.driver.quit()
        except Exception:  # noqa: BLE001 — cleanup only
            pass


class _SeleniumEngine:
    """page_flow.Engine for Selenium: a fresh driver per attempt (Selenium
    has no separate context object; a rotation is a fresh browser)."""

    name = ENGINE_NAME
    readiness_s = READINESS_WAIT_S

    def __init__(self, args: argparse.Namespace, *, user_agent: Optional[str]):
        self.args, self.user_agent = args, user_agent

    async def open(self, proxy) -> _SeleniumSession:
        driver = _build_driver(headless=self.args.headless, proxy=proxy, cdp_endpoint=self.args.cdp_endpoint,
                               user_agent=self.user_agent)
        return _SeleniumSession(driver, self.args)

    async def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def run(args: argparse.Namespace) -> int:
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
        return asyncio.run(page_flow.run_scraper_api(args, start_url=start_url, is_product_page=is_product_page,
                                                     engine_name=ENGINE_NAME, started_at=started_at))

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
    if args.fingerprint and not refuse_if_cdp(args.cdp_endpoint):
        if client is None:
            log.warning("--fingerprint requested but no --twocaptcha-key/TWOCAPTCHA_KEY set — continuing without one.")
        else:
            profile = fetch_fingerprint(client, tags=args.fp_tags, country=args.fp_country)
            if profile:
                user_agent = user_agent_from(profile)

    try:
        return asyncio.run(page_flow.run_browser(
            _SeleniumEngine(args, user_agent=user_agent), args, start_url=start_url,
            is_product_page=is_product_page, proxy_pool=proxy_pool, client=client, started_at=started_at,
        ))
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH


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
