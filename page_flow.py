#!/usr/bin/env python3
"""page_flow.py — the ONE fetch loop all three engines run (CLAUDE.md §26).

Until 2026-09-30 each engine carried its own copy of this loop, and the
copies had already drifted: Playwright never recognised a dead proxy on
navigation (its `is_proxy_dead_error` import was unused), Selenium and
pyppeteer credited a proxy as healthy before the HTTP status was even
read, and pyppeteer reconnected to the Scraping Browser on every block
retry. Here the loop exists once:

  - `fetch_search` / `fetch_product_page` — one attempt at a listing or a
    product page: navigate (bounded retries, dead-proxy detection), pass
    SHEIN's own /risk/challenge, recognise /risk/action/limit, solve a
    generic widget, scroll-merge gbRawData rounds, dump, close;
  - `run_attempts` — "retry before you rotate" (`--block-retries`) plus the
    one `--rate-limit-cooldown` retry, which never spends a block retry;
  - `scrape_via_scraper_api` / `run_scraper_api` — the browserless mode,
    which never depended on an engine and is now written once;
  - `finish` — the single `finish_run` call with every sidecar field.

Each engine passes an `Engine` (open a page session, sleep) whose sessions
provide NAMED operations — `goto`, `url`, `content`, `gb_raw_data`,
`scroll`, `pass_risk_challenge`, `solve_captcha`, `close`. No JavaScript
crosses this boundary (CLAUDE.md §1): each engine spells its own few
lines in its own driver's dialect.
"""
from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path
from typing import Any, List, Optional, Protocol

import shein_parser as sp
from captcha_solver import detect_from_html
from output_writer import EXIT_REMOTE_API_ERROR, Product, finish_run, sku_key
from proxy_pool import ProxyPool, is_proxy_dead_error, redact_credentials
from scraper_api_client import TwoCaptchaAuthError, TwoCaptchaClient, TwoCaptchaError

log = logging.getLogger("page_flow")

Attempt = tuple  # (products, blocked, remote_api_error, rounds, scroll_error)


def jittered_delay(base_seconds: float, jitter: float) -> float:
    """`base_seconds` times a random factor in [1-jitter, 1+jitter], never
    negative, so a run's own waits are not perfectly periodic."""
    if base_seconds <= 0 or jitter <= 0:
        return max(0.0, base_seconds)
    return max(0.0, base_seconds * random.uniform(1.0 - jitter, 1.0 + jitter))


class PageSession(Protocol):
    async def goto(self, url: str) -> Optional[int]: ...  # HTTP status or None; raises on failure
    async def url(self) -> str: ...
    async def content(self) -> str: ...
    async def gb_raw_data(self) -> Optional[dict]: ...
    async def scroll(self) -> None: ...  # raises on failure
    async def pass_risk_challenge(self, client: Optional[TwoCaptchaClient]) -> bool: ...
    async def solve_captcha(self, *, html: str, url: str, client: Optional[TwoCaptchaClient],
                            count_product_links=None) -> Optional[dict]: ...
    async def close(self) -> None: ...


class Engine(Protocol):
    name: str
    readiness_s: float

    async def open(self, proxy) -> PageSession: ...  # RuntimeError = the browser/CDP itself failed
    async def sleep(self, seconds: float) -> None: ...


def _dump_path(out_path: str) -> str:
    return str(Path(out_path).with_suffix("")) + "_debug.html"


def _on_gateway(url: str) -> bool:
    return any(marker in (url or "") for marker in sp.RISK_GATEWAY_URL_MARKERS)


async def _open_and_navigate(engine: Engine, args, start_url: str, proxy_pool: Optional[ProxyPool], client):
    """(session, status, proxy) after a successful navigation, or
    (None, None, proxy) when the browser could not be opened or the page
    never loaded — both a remote failure, never a crash."""
    proxy = proxy_pool.next() if proxy_pool else None
    log.info("Using proxy %s", proxy.masked() if proxy else "(no local proxy pool — direct connection, or a --cdp-endpoint session providing its own exit)")
    try:
        session = await engine.open(proxy)
    except RuntimeError as exc:
        log.error("Browser connection failed — treating as remote_api_error, not a crash: %s", exc)
        return None, None, proxy
    last_error = None
    status = None
    for attempt in range(args.retries + 1):
        try:
            status = await session.goto(start_url)
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001 — every remote call must be bounded and reported
            last_error = redact_credentials(str(exc)).splitlines()[0] if str(exc) else type(exc).__name__
            if proxy_pool is not None and proxy is not None and is_proxy_dead_error(last_error):
                # A dead proxy is not a timeout (CLAUDE.md §8): report it,
                # so the pool rotates instead of retrying the same exit.
                proxy_pool.report_failure(proxy, dead=True)
                log.warning("Proxy reported dead: %s", last_error)
            else:
                log.warning("Navigation attempt %d/%d failed: %s", attempt + 1, args.retries + 1, last_error)
            if attempt < args.retries:
                await engine.sleep(jittered_delay(args.retry_delay, args.delay_jitter))
    if last_error is not None:
        await session.close()
        log.error("Page permanently failed to load: %s", last_error)
        return None, None, proxy
    return session, status, proxy


async def _gateway_checks(session: PageSession, args, client, status, proxy_pool, proxy, *, search: bool) -> tuple:
    """(blocked, status) after SHEIN's own gateways had their say: the
    rate limit is flagged, /risk/challenge gets its automated pass, and a
    page still on either gateway (or an HTTP >= 400) is blocked."""
    blocked = False
    if "/risk/action/limit" in await session.url():
        args._rate_limited = True
    if await session.pass_risk_challenge(client):
        status = None  # the status was the challenge page's, not the page we now hold
    current = await session.url()
    if _on_gateway(current):
        log.warning("Redirected to SHEIN's own risk gateway (%s) — treating as blocked.", current)
        blocked = True
        if search and proxy_pool is not None and proxy is not None:
            proxy_pool.report_failure(proxy, dead=False)
    if status is not None and status >= 400:
        log.warning("Page returned HTTP %d — treating as blocked, not empty.", status)
        blocked = True
        if proxy_pool is not None and proxy is not None and status in (403, 429):
            proxy_pool.report_failure(proxy, dead=True)
    elif status is not None and not blocked and proxy_pool is not None and proxy is not None:
        proxy_pool.report_success(proxy)
    return blocked, status


async def fetch_search(engine: Engine, args, start_url: str, proxy_pool: Optional[ProxyPool], client) -> Attempt:
    """One attempt at a search/category listing: gbRawData merged across
    bounded scroll rounds (see shein_parser.py for why a scroll may or may
    not grow it — the stall cap keeps the loop bounded either way)."""
    session, status, proxy = await _open_and_navigate(engine, args, start_url, proxy_pool, client)
    if session is None:
        return [], False, True, 0, False
    try:
        blocked, status = await _gateway_checks(session, args, client, status, proxy_pool, proxy, search=True)
        scroll_error = False
        seen: set = set()
        merged: List[Product] = []
        stall = 0
        previous_count = -1
        rounds = 0
        for round_num in range(args.max_scrolls + 1):
            rounds = round_num
            if args._rate_limited:
                break
            html = await session.content()
            raw_data = await session.gb_raw_data()
            cards_present = sp.count_result_cards(html) > 0
            # The challenge page's SSR state embeds its own redirect URL as
            # text, and that text can outlive the gateway (live 2026-09-22):
            # past round 0 a marker only counts while the URL still shows it.
            captcha_detected = detect_from_html(html, sp.BOT_CHALLENGE_MARKERS)
            if captcha_detected and round_num > 0 and not _on_gateway(await session.url()):
                log.warning(
                    "A bot-mitigation marker matched stale page text, but the page has moved on to "
                    "%s (%s) — not re-flagging this round as a captcha block.",
                    await session.url(), sp.diagnose_unexpected_page(html),
                )
                captcha_detected = False
            if captcha_detected and not cards_present:
                blocked = True
            if captcha_detected:
                captcha_result = await session.solve_captcha(html=html, url=start_url, client=client)
                if captcha_result and captcha_result.get("action") in (
                    "warning_no_key", "warning_solver_error", "detected_unidentified_widget",
                ) and sp.count_result_cards(html) == 0:
                    blocked = True

            result = sp.safe_parse_search_results(html, max_results=args.max_results, raw_data=raw_data)
            args._rejected_rows = max(getattr(args, "_rejected_rows", 0), result.rejected_rows)
            if result.source_used == "none" and round_num == 0 and not blocked:
                log.warning(
                    "No products recognised on the first render (%s, final URL: %s) — either this "
                    "search genuinely has no results, the gbRawData path needs updating for the current "
                    "markup, or shein.com served a DIFFERENT page than search results (a real case — "
                    "see shein_parser.py). Re-run with --dump-html to inspect the captured page.",
                    sp.diagnose_unexpected_page(html), await session.url(),
                )

            round_skus = {sku_key(p) for p in result.products}
            new_skus = round_skus - seen
            if new_skus:
                merged.extend(p for p in result.products if sku_key(p) in new_skus)
                seen |= new_skus
                stall = 0
            elif previous_count == len(round_skus):
                stall += 1
            else:
                stall = 0
            previous_count = len(round_skus)
            if result.products:
                blocked = False

            if len(merged) >= args.max_results:
                merged = merged[: args.max_results]
                break
            if round_num >= args.max_scrolls or stall >= args.stall_rounds:
                break
            try:
                await session.scroll()
            except Exception as exc:  # noqa: BLE001 — a scroll failure ends pagination, not the run
                log.warning("Scroll failed, stopping pagination early: %s", exc)
                scroll_error = True
                break
            await engine.sleep(jittered_delay(args.scroll_delay, args.delay_jitter))

        final_html = await session.content()
        total = sp.total_result_count(final_html, raw_data=await session.gb_raw_data())
        args._total_results = total
        expected = min(total, args.max_results) if total is not None else None
        if expected is not None and len(merged) < expected:
            log.warning(
                "Fewer products collected than SHEIN reports available: reports %d, requested %d, collected %d.",
                total, args.max_results, len(merged),
            )
            scroll_error = True
        if args.dump_html:
            Path(_dump_path(args.out)).write_text(final_html, encoding="utf-8")
        return merged, blocked, False, rounds, scroll_error
    finally:
        await session.close()


async def fetch_product_page(engine: Engine, args, start_url: str, proxy_pool: Optional[ProxyPool], client) -> Attempt:
    """One attempt at a `--url` product page (JSON-LD, see shein_parser)."""
    session, status, proxy = await _open_and_navigate(engine, args, start_url, proxy_pool, client)
    if session is None:
        return [], False, True, 0, False
    try:
        blocked, status = await _gateway_checks(session, args, client, status, proxy_pool, proxy, search=False)
        html = await session.content()

        def parsed(h: str) -> int:
            return 1 if sp.parse_product_page(h, url=start_url) else 0

        if detect_from_html(html, sp.BOT_CHALLENGE_MARKERS):
            # Product-page-shaped presence count, NOT the search-card count
            # (which is 0 on every product page and would pay for a solve
            # on any harmless site-wide marker).
            captcha_result = await session.solve_captcha(html=html, url=start_url, client=client, count_product_links=parsed)
            if captcha_result and captcha_result.get("action") == "solved":
                await engine.sleep(engine.readiness_s)
                html = await session.content()
            elif captcha_result and captcha_result.get("action") in (
                "warning_no_key", "warning_solver_error", "detected_unidentified_widget",
            ) and not parsed(html):
                blocked = True
        product = sp.parse_product_page(html, url=start_url)
        if args.dump_html:
            Path(_dump_path(args.out)).write_text(html, encoding="utf-8")
        products = [product] if product else []
        if not products and not blocked:
            log.warning("Product page rendered but no ProductGroup/Product JSON-LD was found — see shein_parser.py.")
        return products, blocked, False, 0, False
    finally:
        await session.close()


async def run_attempts(attempt, args, *, sleep, label: str) -> Attempt:
    """--block-retries ("retry before you rotate", on the SAME session) and
    the ONE --rate-limit-cooldown retry, which never spends a block retry
    (audit 2026-09-30: `continue` on the last range() iteration ended the
    loop, so the promised retry never ran)."""
    block_attempt = 0
    while True:
        result = await attempt()
        merged, blocked, remote_api_error, _rounds, _scroll_error = result
        if args._rate_limited:
            if args.rate_limit_cooldown > 0 and not args._cooldown_used:
                args._cooldown_used = True
                args._rate_limited = False
                wait_s = jittered_delay(args.rate_limit_cooldown, args.delay_jitter)
                log.warning(
                    "SHEIN rate limit reached — waiting %.0fs (opt-in --rate-limit-cooldown, "
                    "honoring the ~5 minute cooldown observed live) before ONE retry on the "
                    "same session, instead of giving up immediately.",
                    wait_s,
                )
                await sleep(wait_s)
                continue
            log.warning("SHEIN rate limit reached; stopping this run without block retries. Try again after at least five minutes.")
            return result
        if remote_api_error or not (blocked and not merged):
            return result
        if block_attempt >= args.block_retries:
            return result
        block_attempt += 1
        log.warning(
            "Blocked with zero products (%s attempt %d/%d) — retrying the same fetch before giving up.",
            label, block_attempt, args.block_retries + 1,
        )
        await sleep(jittered_delay(args.retry_delay, args.delay_jitter))


def scrape_via_scraper_api(*, args, start_url: str, is_product_page: bool, client: TwoCaptchaClient,
                           cdp_url: Optional[str] = None) -> Attempt:
    """--scraper-api: one browserless HTTP call, no live page, no scroll.
    With --scraper-api-cdp, `cdp_url` routes it through 2Captcha's own
    Scraping Browser (their auto-solve applies on their side). There is no
    DOM here for a solved token to go into, so --solve-captcha is a no-op
    in this mode — --block-retries is the only mitigation."""
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
    # The body IS the final response — no later navigation can have moved
    # past the gateway, so a marker is trusted outright here.
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
            "No products recognised in the Scraper API response (%s) — either this search genuinely "
            "has no results, or the fetch landed on a page/locale this parser doesn't recognise. "
            "Re-run with --dump-html to inspect what actually came back.",
            sp.diagnose_unexpected_page(html),
        )
    return parsed.products, blocked, False, 0, False


async def run_scraper_api(args, *, start_url: str, is_product_page: bool, engine_name: str, started_at: float) -> int:
    """The whole --scraper-api mode, identical for every engine (it never
    touches one)."""
    if (args.proxy or args.proxy_file or args.cdp_endpoint or args.fingerprint):
        log.warning(
            "--scraper-api ignores --proxy/--proxy-file/--cdp-endpoint/--fingerprint — this mode "
            "brings its own exit IP/device via 2Captcha's own infrastructure, see --scraper-api's help text."
        )
    if (args.scraper_api_country or args.scraper_api_profile_id or args.scraper_api_account_id) and not args.scraper_api_cdp:
        log.warning(
            "--scraper-api-country/--scraper-api-account-id/--scraper-api-profile-id are ignored without "
            "--scraper-api-cdp — there is no Scraping Browser session for them to apply to."
        )
    client = TwoCaptchaClient(args.twocaptcha_key, api_base=args.captcha_api, scraper_api_base=args.scraper_api_url)
    cdp_url = None
    if args.scraper_api_cdp and not args.scraper_api_profile_id:
        log.warning(
            "--scraper-api-cdp without --scraper-api-profile-id — each run gets a fresh profile from "
            "2Captcha's default pool instead of a warmed, reused identity. Pass --scraper-api-profile-id "
            "to reuse one across runs (see scraping_browser_connection_url's docstring and README/TESTING.md)."
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
    state = {"cdp_url": cdp_url, "fallback_used": False}

    async def attempt() -> Attempt:
        result = scrape_via_scraper_api(args=args, start_url=start_url, is_product_page=is_product_page,
                                        client=client, cdp_url=state["cdp_url"])
        if result[2] and state["cdp_url"] is not None and not state["fallback_used"]:
            # The Scraping Browser session itself failed (an HTTP-level
            # error, not a blocked page): ONE automatic fallback to the plain
            # pool for the rest of the run, logged loudly because it drops
            # the country/profile pinning and 2Captcha's auto-solve.
            log.warning(
                "--scraper-api-cdp's Scraping Browser session failed — falling back to --scraper-api's "
                "plain default pool for the rest of this run instead of giving up outright. This run no "
                "longer has --scraper-api-cdp's country/profile selection or 2Captcha's own captcha auto-solve."
            )
            state["fallback_used"] = True
            state["cdp_url"] = None
            result = scrape_via_scraper_api(args=args, start_url=start_url, is_product_page=is_product_page,
                                            client=client, cdp_url=None)
        return result

    merged, blocked, remote_api_error, _rounds, _scroll_error = await run_attempts(
        attempt, args, sleep=asyncio.sleep, label="Scraper API",
    )
    return finish(args, merged=merged, blocked=blocked, remote_api_error=remote_api_error,
                  engine_name=engine_name, start_url=start_url, started_at=started_at,
                  pages_requested=1, pages_completed=0 if remote_api_error else 1, failed_pages=None)


def finish(args, *, merged: List[Product], blocked: bool, remote_api_error: bool, engine_name: str,
           start_url: str, started_at: float, pages_requested: int, pages_completed: int,
           failed_pages: Optional[List[int]]) -> int:
    """The one finish_run call, with every sidecar field the family and
    diff_runs rely on."""
    budget = getattr(args, "_solve_budget", None)
    price_confirmed_pct = (sum(1 for p in merged if p.price is not None) / len(merged)) if merged else None
    return finish_run(
        products=merged, out_path=args.out, fmt=args.format, engine=engine_name, url=start_url,
        pages_requested=pages_requested, pages_completed=pages_completed, failed_pages=failed_pages,
        blocked=blocked, remote_api_error=remote_api_error, allow_empty=args.allow_empty,
        started_at=started_at, rejected_rows=getattr(args, "_rejected_rows", 0), max_results=args.max_results,
        extra_meta={"solves_spent": budget.spent if budget is not None else 0, "sort": getattr(args, "sort", None)},
        rate_limited=bool(getattr(args, "_rate_limited", False)), total_results=getattr(args, "_total_results", None),
        price_confirmed_pct=price_confirmed_pct,
    )


async def run_browser(engine: Engine, args, *, start_url: str, is_product_page: bool,
                      proxy_pool: Optional[ProxyPool], client, started_at: float) -> int:
    """The browser modes (local launch or --cdp-endpoint), after the engine
    has its browser: block retries around one fetch function, then finish."""
    fetch = fetch_product_page if is_product_page else fetch_search

    async def attempt() -> Attempt:
        return await fetch(engine, args, start_url, proxy_pool, client)

    merged, blocked, remote_api_error, rounds, scroll_error = await run_attempts(
        attempt, args, sleep=engine.sleep, label="browser-session",
    )
    completed_rounds = rounds + 1
    return finish(args, merged=merged, blocked=blocked, remote_api_error=remote_api_error,
                  engine_name=engine.name, start_url=start_url, started_at=started_at,
                  pages_requested=args.max_scrolls, pages_completed=completed_rounds,
                  failed_pages=[completed_rounds + 1] if scroll_error else None)


def init_run_state(args, budget_factory) -> None:
    """Per-run flags every engine and this module read."""
    args._rate_limited = False
    args._cooldown_used = False
    args._rejected_rows = 0
    args._total_results = None
    args._solve_budget = budget_factory(args.max_solves)


__all__: List[Any] = [
    "Engine", "PageSession", "fetch_search", "fetch_product_page", "run_attempts", "run_browser",
    "run_scraper_api", "scrape_via_scraper_api", "finish", "init_run_state", "jittered_delay",
]
