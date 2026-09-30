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
from typing import Optional

try:
    from playwright.async_api import Browser, BrowserContext, Page, async_playwright
except ImportError as _IMPORT_ERROR:  # pragma: no cover — exercised by smoke_test's no-engine path
    Browser = BrowserContext = Page = None
    async_playwright = None
    _PLAYWRIGHT_IMPORT_ERROR = _IMPORT_ERROR
else:
    _PLAYWRIGHT_IMPORT_ERROR = None

import env_config
import scraper_api_client
import page_flow
import shein_challenge
import shein_parser as sp
from captcha_solver import CaptchaType, build_injection_script, solve_when_blocked
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH
from proxy_pool import Proxy, ProxyPool, ProxyParseError, load_proxies, redact_credentials
from scraper_api_client import TwoCaptchaClient

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


def _budget(args) -> "shein_challenge.SolveBudget":
    """The run's shared paid-solve budget (created in run(); a fresh one
    for callers that bypass run(), e.g. tests)."""
    if getattr(args, "_solve_budget", None) is None:
        args._solve_budget = shein_challenge.SolveBudget(getattr(args, "max_solves", 8))
    return args._solve_budget


_jittered_delay = page_flow.jittered_delay  # kept for callers and tests; the one implementation is page_flow's


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="shein.com fashion listing scraper — Playwright engine",
        epilog="Credentials belong in .env / SHEIN_PROXY / TWOCAPTCHA_KEY — never on this command line.",
    )
    p.add_argument("--url", default=None, help="Full shein.com search/category/product URL (or set SHEIN_URL) — overrides --query/--category")
    p.add_argument("--query", default=None, help="Search term, e.g. 'summer dress'")
    p.add_argument("--category", default=None, help="A category path copied from shein.com navigation, e.g. 'Women Jeans-c-1934.html'")
    p.add_argument("--sort", choices=sp.SORT_VALUES, default="relevance", help="Recorded in the sidecar only — NOT sent to SHEIN yet (its query parameter is unconfirmed, see shein_parser.py); diff_runs refuses runs whose sort differs")
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
    p.add_argument(
        "--risk-challenge-rounds", type=_nonnegative_int, default=5,
        help="On SHEIN's own /risk/challenge gateway, click its 'I am human' checkbox and solve up to "
             "this many rounds of its 3x3 image grid via 2Captcha GridTask (needs TWOCAPTCHA_KEY; the "
             "checkbox step alone needs no key). 0 disables. Skipped with --solve-captcha off. See "
             "shein_challenge.py.",
    )
    p.add_argument(
        "--max-solves", type=_nonnegative_int, default=8,
        help="Cap on PAID 2Captcha solves for the whole run, across every challenge round, block "
             "retry and scroll round (0 = never pay). Recorded as solves_spent in the sidecar.",
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


def _challenge_debug_dir(args: argparse.Namespace) -> Optional[str]:
    return str(Path(args.out).with_suffix("")) + "_challenge" if args.dump_html else None


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
    *, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str, min_score: float = 0.3, args=None,
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


class _PlaywrightChallengeDriver:
    """shein_challenge.ChallengeDriver over a Playwright page."""

    def __init__(self, page: Page):
        self.page = page
        page.on("response", self._on_response)

    def _on_response(self, response) -> None:
        # SHEIN's verdict on each submission ("code" 0 = accepted, 9001 =
        # "System error" even for answers verified correct by eye) — the
        # one signal that separates a wrong answer from a risk rejection.
        if "/risk/verify/identity/validation/check" in response.url:
            async def report():
                try:
                    body = await response.json()
                    log.info("SHEIN validation/check: code=%s msg=%s type=%s", body.get("code"), body.get("msg"),
                             (body.get("info") or {}).get("validate_type"))
                except Exception:  # noqa: BLE001 — diagnostics only
                    pass
            asyncio.ensure_future(report())

    async def url(self) -> str:
        return self.page.url

    async def state(self) -> dict:
        return await self.page.evaluate(shein_challenge.STATE_JS) or {}

    async def screenshot(self, clip: dict) -> bytes:
        return await self.page.screenshot(clip=clip)

    async def click(self, x: float, y: float) -> None:
        mouse = self.page.mouse
        await mouse.move(x + random.uniform(-60, 60), y + random.uniform(-60, 60), steps=random.randint(8, 14))
        await mouse.move(x, y, steps=random.randint(10, 18))
        await self.page.wait_for_timeout(random.randint(120, 300))
        await mouse.down()
        await self.page.wait_for_timeout(random.randint(60, 130))
        await mouse.up()

    async def sleep(self, seconds: float) -> None:
        await self.page.wait_for_timeout(int(seconds * 1000))


async def _maybe_pass_risk_challenge(page: Page, args: argparse.Namespace, client: Optional[TwoCaptchaClient]) -> bool:
    """True when the page has left SHEIN's /risk/challenge gateway (it
    redirects back to the original URL on success)."""
    if not shein_challenge.on_challenge(page.url) or args.solve_captcha == "off" or args.risk_challenge_rounds <= 0:
        return False
    outcome = await shein_challenge.pass_risk_challenge(
        _PlaywrightChallengeDriver(page), client, max_rounds=args.risk_challenge_rounds, budget=_budget(args),
        debug_dir=_challenge_debug_dir(args),
    )
    if outcome.passed:
        log.info("Passed SHEIN's risk challenge (%s; %d paid solve(s)).", outcome.detail, outcome.solves)
        await page.wait_for_timeout(READINESS_WAIT_MS)
    else:
        log.warning("SHEIN risk challenge not passed: %s", outcome.detail)
    return outcome.passed


async def _connect_over_cdp(pw, cdp_endpoint: str):
    """See every sibling repo's playwright_scraper.py for why this wraps
    the connection error rather than letting it propagate: connect_over_cdp
    repeats a failed endpoint's login:password in its own message and
    "Call log" several times over."""
    return await scraper_api_client.connect_with_retry(
        lambda: pw.chromium.connect_over_cdp(cdp_endpoint), redact=redact_credentials, log=log,
    )


class _PlaywrightSession:
    """page_flow.PageSession over one Playwright page (CLAUDE.md §26: the
    loop is shared, only these named operations are Playwright's)."""

    def __init__(self, page: Page, context: BrowserContext, *, reuse_default: bool, args: argparse.Namespace):
        self.page, self.context, self.reuse_default, self.args = page, context, reuse_default, args

    async def goto(self, url: str) -> Optional[int]:
        response = await self.page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await self.page.wait_for_timeout(READINESS_WAIT_MS)
        return response.status if response is not None else None

    async def url(self) -> str:
        return self.page.url

    async def content(self) -> str:
        return await self.page.content()

    async def gb_raw_data(self) -> Optional[dict]:
        """Live JS read — the confirmed-real, most robust source (see
        shein_parser.py); a JS-side error falls back to HTML extraction."""
        try:
            return await self.page.evaluate("() => window.gbRawData || null")
        except Exception as exc:  # noqa: BLE001
            log.debug("page.evaluate('window.gbRawData') failed, will fall back to HTML extraction: %s", exc)
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
        await _close_scrape_page(self.page, self.context, reuse_default=self.reuse_default)


class _PlaywrightEngine:
    """page_flow.Engine: opens a page per attempt on the run's browser.
    Over --cdp-endpoint the profile's default context is reused, so an
    already-cleared challenge and its cookies survive; a local launch gets
    a fresh context per attempt."""

    name = ENGINE_NAME
    readiness_s = READINESS_WAIT_MS / 1000

    def __init__(self, browser: Browser, args: argparse.Namespace, *, autosolve: bool, user_agent: Optional[str]):
        self.browser, self.args, self.autosolve, self.user_agent = browser, args, autosolve, user_agent

    async def open(self, proxy) -> _PlaywrightSession:
        reuse_default = bool(self.args.cdp_endpoint)
        context = await _new_context(self.browser, proxy, self.user_agent, reuse_default=reuse_default)
        page = await context.new_page()
        if self.autosolve:
            await _enable_scraping_browser_auto_solve(context, page)
        return _PlaywrightSession(page, context, reuse_default=reuse_default, args=self.args)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


async def run(args: argparse.Namespace) -> int:
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
        # Browserless: runs even with no engine driver installed (CLAUDE.md §6).
        if not args.twocaptcha_key:
            print("Error: --scraper-api requires --twocaptcha-key/TWOCAPTCHA_KEY", file=sys.stderr)
            return EXIT_BAD_USAGE
        return await page_flow.run_scraper_api(args, start_url=start_url, is_product_page=is_product_page,
                                               engine_name=ENGINE_NAME, started_at=started_at)

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
    if args.fingerprint and not refuse_if_cdp(args.cdp_endpoint):
        if client is None:
            log.warning("--fingerprint requested but no --twocaptcha-key/TWOCAPTCHA_KEY set — continuing without one.")
        else:
            profile = fetch_fingerprint(client, tags=args.fp_tags, country=args.fp_country)
            if profile:
                user_agent = user_agent_from(profile)

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
                    return page_flow.finish(args, merged=[], blocked=False, remote_api_error=True,
                                            engine_name=ENGINE_NAME, start_url=start_url, started_at=started_at,
                                            pages_requested=args.max_scrolls, pages_completed=1, failed_pages=None)
            else:
                browser = await pw.chromium.launch(headless=args.headless)
            engine = _PlaywrightEngine(browser, args, autosolve=bool(args.cdp_endpoint) and args.solve_captcha != "off",
                                       user_agent=user_agent)
            try:
                return await page_flow.run_browser(engine, args, start_url=start_url, is_product_page=is_product_page,
                                                   proxy_pool=proxy_pool, client=client, started_at=started_at)
            finally:
                await browser.close()
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH


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
