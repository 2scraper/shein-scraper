#!/usr/bin/env python3
"""smoke_test.py — one file of plain functions with inline/synthetic-
fixture checks. No pytest, no conftest. `tests/test_smoke.py` wraps this
as a single pytest entry point so `pytest` also works, without a second
copy of the checks.

**Honesty note, read before trusting a green run** (same caveat as every
other family member's smoke_test.py). Most HTML fixtures below are
SYNTHETIC — hand-written to exercise the parsing code paths, but built to
match the REAL confirmed field names/shapes from a real live capture (see
shein_parser.py's module docstring and TESTING.md), not invented ones —
this is a stronger starting position than most sibling repos had on their
first build. `tests/fixtures/shein_risk_challenge_real.html` is a
scrubbed, REAL capture of the live bot-mitigation incident (not
synthetic); `tests/fixtures/shein_search_live_dress_20260921.json` is a
second, later REAL capture — a genuine `window.gbRawData` snapshot from a
clean (non-challenged) live search — used to prove `parse_search_results()`
round-trips through `output_writer.finish_run()` as a clean `complete` run
against real data, not just synthetic fixtures. A green run here proves
the architecture (exit codes, dedupe, precedence, credential redaction,
CLI validation, engines importing cleanly) AND that the confirmed data
shapes parse correctly against both synthetic AND real captured data — it
does NOT yet prove this repo's own engine scripts (browser launch, scroll
loop, retries) get the same treatment against the live site end-to-end;
both real captures so far came from the built-in browser-rendering tool,
not `playwright_scraper.py` itself (see TESTING.md — network egress to
shein.com and to the Chromium download CDN is currently blocked from both
this repo's cloud build environment and the linked device's own shell, so
that last step is still open).

Run directly: `python3 smoke_test.py`
"""
from __future__ import annotations

import asyncio
import inspect as _inspect
import json
import tempfile
from pathlib import Path

import captcha_solver
import diff_runs
import env_config
import output_writer
import proxy_pool
import puppeteer_scraper
import scraper_api_client
import selenium_scraper
import shein_challenge
import shein_parser as sp

try:
    import playwright_scraper
except Exception as exc:  # pragma: no cover — this import itself must never fail
    raise AssertionError(f"playwright_scraper must import cleanly even without playwright installed: {exc}") from exc

ROOT = Path(__file__).parent

RESULTS = []  # (name, ok, detail)


def check(name):
    """Runs the decorated function IMMEDIATELY (at module-load time) and
    records the outcome — same pattern as every other family member's
    smoke_test.py; every check function is named `_` because only RESULTS
    is ever read, nothing looks a check up by name."""
    def decorator(fn):
        try:
            fn()
            RESULTS.append((name, True, ""))
        except AssertionError as exc:
            RESULTS.append((name, False, str(exc)))
        except Exception as exc:  # a check that crashes is still a failure, not an uncaught traceback
            RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        except SystemExit as exc:  # a CLI helper exiting inside a check would otherwise end the whole suite silently
            RESULTS.append((name, False, f"SystemExit: {exc}"))
        return fn
    return decorator


def asyncio_run_maybe(mod, args):
    """playwright_scraper.run()/puppeteer_scraper.run() are coroutines;
    selenium_scraper.run() is plain sync."""
    result = mod.run(args)
    if _inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


# --------------------------------------------------------------------------- #
# Engine import/CLI hygiene (CLAUDE.md §6)
# --------------------------------------------------------------------------- #
@check("engines import cleanly regardless of installed drivers")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        assert hasattr(mod, "build_arg_parser")
        assert hasattr(mod, "run")


@check("each engine imports its driver at MODULE level, guarded by try/except ImportError")
def _():
    for path in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "except ImportError as _IMPORT_ERROR" in src, f"{path}: missing guarded driver import"


@check("no forbidden overclaiming wording in any shipped .py/.md/.yml file")
def _():
    banned = (
        "cloud browser", "antidetect browser", "2scraper antidetect browser",
        "gate.2prx.com", "--antidetect", "antidetect_local_api",
    )
    exempt_names = {"smoke_test.py", "CLAUDE.md"}
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in (".py", ".md", ".html", ".toml", ".cfg", ".yml", ".yaml"):
            continue
        if path.name in exempt_names or path.name.startswith("2scraper"):
            continue
        if ".git" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for phrase in banned:
            assert phrase not in text, f"{path.relative_to(ROOT)}: contains banned phrase {phrase!r}"


@check("all three engines expose the identical --flag set (CLAUDE.md §4)")
def _():
    def flag_set(mod):
        return {opt for a in mod.build_arg_parser()._actions for opt in a.option_strings if opt.startswith("--")}

    pw, se, pu = flag_set(playwright_scraper), flag_set(selenium_scraper), flag_set(puppeteer_scraper)
    all_engines = pw | se | pu
    for name, flags in (("playwright_scraper", pw), ("selenium_scraper", se), ("puppeteer_scraper", pu)):
        missing = all_engines - flags
        assert not missing, f"{name} is missing {sorted(missing)} that (an)other engine(s) define — flag sets have drifted apart"


@check("all three engines share the same default output filename stem")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        assert mod._default_out("json") == "shein_results.json"


@check("all engines read window.gbRawData live via page evaluation, not just static HTML")
def _():
    evaluate_calls = {
        "playwright_scraper.py": "window.gbRawData",
        "selenium_scraper.py": "_GB_RAW_DATA_JS",
        "puppeteer_scraper.py": "window.gbRawData",
    }
    for path, needle in evaluate_calls.items():
        src = (ROOT / path).read_text(encoding="utf-8")
        assert needle in src, f"{path}: missing a live window.gbRawData read"


@check("captcha markers only classify a page as blocked when product cards are absent")
def _():
    for path in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "captcha_detected and not cards_present" in src, (
            f"{path}: a marker can still turn a healthy product page into EXIT_BLOCKED"
        )


@check("all engines check the confirmed-real /risk/challenge redirect, not just an HTTP status")
def _():
    for path in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "/risk/challenge" in src, f"{path}: missing the confirmed-real bot-mitigation redirect check"


@check("engines never request a robots.txt-disallowed path — _resolve_start_url filters it out")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        url, _ = mod._resolve_start_url(mod.build_arg_parser().parse_args(["--url", "https://us.shein.com/cart/"]))
        assert url is None, f"{mod.__name__}: a disallowed path must never be attempted"


# --------------------------------------------------------------------------- #
# output_writer — exit codes / precedence / dedupe (CLAUDE.md §9)
# --------------------------------------------------------------------------- #
@check("exit codes and STATUS_BY_EXIT match the family contract exactly")
def _():
    expected = {0: "complete", 1: "crashed", 2: "bad_usage", 3: "blocked", 4: "empty", 5: "remote_api_error", 6: "partial"}
    assert output_writer.STATUS_BY_EXIT == expected


def _mk_product(sku, price=16.29, **kw):
    defaults = dict(
        sku=sku, source="shein.com", category="Women Mini Dresses", title="Solid Color Sleeveless Mini Dress",
        brand="Aloruh", price=price, currency="USD", price_source="embedded_json",
        product_url=f"https://us.shein.com/dress-p-{sku}.html", image_url=None, scraped_at="2026-09-21T00:00:00Z",
    )
    defaults.update(kw)
    return output_writer.Product(**defaults)


@check("finish_run: rows gathered by a run that did not finish are PARTIAL (6) with the cause in stop_reason — never 5/3 with a file (CLAUDE.md §25; audit 2026-09-30 got exit 5 AND a written file). Rewrites the old pinned 'exit 5 with products' position deliberately.")
def _():
    cases = (
        (dict(blocked=True, remote_api_error=True), "remote_api_error"),
        (dict(blocked=False, remote_api_error=True), "remote_api_error"),
        (dict(blocked=True, remote_api_error=False), "blocked"),
        (dict(blocked=False, remote_api_error=False, failed_pages=[3]), "failed_pages"),
        (dict(blocked=False, remote_api_error=False, rejected_rows=2), "rejected_rows"),
    )
    for kw, reason in cases:
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "out.json")
            kw = {"failed_pages": None, **kw}
            code = output_writer.finish_run(
                products=[_mk_product("1")], out_path=out, fmt="json", engine="test", url="u",
                pages_requested=3, pages_completed=2, allow_empty=False, started_at=0.0, **kw,
            )
            assert code == output_writer.EXIT_PARTIAL, (kw, code)
            assert Path(out).exists(), "already-collected products must still be written out"
            meta = json.loads(Path(f"{out}.meta.json").read_text())
            assert meta["status"] == "partial" and meta["stop_reason"] == reason, (kw, meta)
            if reason == "rejected_rows":
                assert meta["rejected_rows"] == 2
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "z.json")
        code = output_writer.finish_run(
            products=[], out_path=out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=0,
            failed_pages=None, blocked=False, remote_api_error=True, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_REMOTE_API_ERROR and not Path(out).exists(), "5 promises no file"


@check("finish_run precedence: blocked+zero-products respects --allow-empty for WHETHER to write, never for the STATUS")
def _():
    with tempfile.TemporaryDirectory() as td:
        out_a = str(Path(td) / "a.json")
        code = output_writer.finish_run(
            products=[], out_path=out_a, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=[2],
            blocked=True, remote_api_error=False, allow_empty=True, started_at=0.0,
        )
        assert code == output_writer.EXIT_BLOCKED
        assert Path(out_a).exists(), "--allow-empty means a zero-product outcome DOES get written"
        meta = json.loads(Path(f"{out_a}.meta.json").read_text())
        assert meta["status"] == "blocked", "--allow-empty must never launder this into 'complete'"

        out_b = str(Path(td) / "b.json")
        code = output_writer.finish_run(
            products=[], out_path=out_b, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=[2],
            blocked=True, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_BLOCKED
        assert not Path(out_b).exists(), "without --allow-empty, a zero-product outcome writes nothing"


@check("finish_run: zero products without --allow-empty writes neither file nor sidecar")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_ZERO_PRODUCTS
        assert not Path(out).exists()
        assert not Path(f"{out}.meta.json").exists()


@check("finish_run: partial (failed pages, some products) writes output and reports EXIT_PARTIAL")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[_mk_product("1")], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=2, pages_completed=1, failed_pages=[2],
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_PARTIAL
        assert Path(out).exists()
        meta = json.loads(Path(f"{out}.meta.json").read_text())
        assert meta["status"] == "partial"


@check("finish_run: a clean run with products writes output and reports EXIT_OK/complete")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[_mk_product("1"), _mk_product("2")], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_OK
        data = json.loads(Path(out).read_text())
        assert len(data) == 2


@check("Product field order: family-common fields first, shein-specific fields after")
def _():
    expected_head = [
        "sku", "source", "category", "title", "brand", "price", "currency",
        "price_source", "product_url", "image_url", "scraped_at",
    ]
    assert output_writer.PRODUCT_FIELD_NAMES[: len(expected_head)] == expected_head
    tail = output_writer.PRODUCT_FIELD_NAMES[len(expected_head):]
    for name in ("original_price", "discount_pct", "rating", "review_count", "store_code", "in_stock", "is_clearance", "quickship"):
        assert name in tail, f"missing shein-specific field {name!r}"


@check("merge_pages dedupes by sku, last-write-wins, in PAGE order not arrival order")
def _():
    page1 = [_mk_product("1", price=10.00), _mk_product("2", price=20.00)]
    page2 = [_mk_product("1", price=15.00), _mk_product("3", price=30.00)]  # "1" price changed
    merged = output_writer.merge_pages([page1, page2])
    skus = [p.sku for p in merged]
    assert skus == ["1", "2", "3"], f"expected page-order with new items appended, got {skus}"
    p1 = next(p for p in merged if p.sku == "1")
    assert p1.price == 15.00, "later page's value must win for a repeated sku"


@check("write_csv writes a header even for zero rows")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.csv")
        output_writer.write_csv([], out)
        text = Path(out).read_text()
        assert text.strip() != ""
        assert "sku" in text.splitlines()[0]


# --------------------------------------------------------------------------- #
# proxy_pool — parsing, redaction, dead-marking (family-shared, unmodified)
# --------------------------------------------------------------------------- #
@check("proxy_pool rejects a malformed proxy string with ProxyParseError")
def _():
    try:
        proxy_pool.load_proxies("not a proxy!!", None)
        raise AssertionError("expected ProxyParseError")
    except proxy_pool.ProxyParseError:
        pass


@check("proxy_pool parses a credentialed proxy and masks it in logs")
def _():
    proxies = proxy_pool.load_proxies("http://user:secretpass@host.example:8080", None)
    assert len(proxies) == 1
    p = proxies[0]
    assert p.has_auth
    masked = p.masked()
    assert "secretpass" not in masked
    assert "host.example" in masked


@check("proxy_pool.redact_credentials strips login:password out of an arbitrary string")
def _():
    raw = "connect failed: ws://myuser:mysecret@cb.2captcha.com:9222 (5 attempts)"
    redacted = proxy_pool.redact_credentials(raw)
    assert "mysecret" not in redacted
    assert "myuser" not in redacted


# --------------------------------------------------------------------------- #
# captcha_solver — generic detection (family-shared, unmodified) + the
# real, shein-specific incident
# --------------------------------------------------------------------------- #
@check("captcha_solver.detect_from_html finds generic bot-challenge markers")
def _():
    assert captcha_solver.detect_from_html("<html>please complete the g-recaptcha below</html>")
    assert not captcha_solver.detect_from_html("<html><body>ordinary page, no widgets</body></html>")


@check("BOT_CHALLENGE_MARKERS matches the real, captured SHEIN risk/challenge incident (2026-09-21), not a guess")
def _():
    # Updated once a real shein.com block page existed to check against
    # (see shein_parser.py's module docstring for the full incident).
    # Unlike every prior family incident, the GENERIC detector does NOT
    # catch this one on its own (SHEIN's own gateway carries no vendor
    # string the generic list knows about) — these markers are the ONLY
    # detection path, not corroboration, so this check matters more here
    # than in a sibling repo's equivalent.
    real_block_fixture = (ROOT / "tests" / "fixtures" / "shein_risk_challenge_real.html").read_text(encoding="utf-8")
    assert sp.BOT_CHALLENGE_MARKERS, "the real incident below should have left at least one marker"
    # BOT_CHALLENGE_MARKERS now covers TWO distinct real incidents (see
    # shein_parser.py's module docstring) — this fixture is only the
    # /risk/challenge one, so only ITS markers are asserted against it;
    # the /risk/action/limit marker gets its own check below.
    risk_challenge_markers = ("/risk/challenge", "captcha_type=909")
    for marker in risk_challenge_markers:
        assert marker in sp.BOT_CHALLENGE_MARKERS, f"{marker!r} unexpectedly dropped from BOT_CHALLENGE_MARKERS"
        assert marker.lower() in real_block_fixture.lower(), f"{marker!r} does not match the actual captured incident"
    assert captcha_solver.detect_from_html(real_block_fixture, sp.BOT_CHALLENGE_MARKERS), (
        "detect_from_html must flag the real captured risk/challenge page as a block, with extra_markers passed"
    )
    assert not captcha_solver.detect_from_html(real_block_fixture), (
        "confirms the GENERIC markers alone do NOT catch this incident — extra_markers is load-bearing here, "
        "not just corroboration, unlike every prior family incident"
    )


@check("BOT_CHALLENGE_MARKERS/RISK_GATEWAY_URL_MARKERS also cover /risk/action/limit — the rate-limit incident found on Roman's own first live engine run (added 2026-09-21)")
def _():
    # Real URL from a live playwright_scraper.py run, not a browser-
    # rendering tool this time: https://us.shein.com/risk/action/limit
    # ?risk-id=E4913845744991097345 — see shein_parser.py's module
    # docstring, "RESOLVED minutes later" section, for the full incident.
    # No real scrubbed HTML capture is committed here (unlike the
    # /risk/challenge fixture) — the actual page carries third-party
    # tracker/analytics noise not worth preserving just to prove a path
    # marker matches; the marker itself is the load-bearing thing.
    assert "/risk/action/limit" in sp.BOT_CHALLENGE_MARKERS
    assert "/risk/action/limit" in sp.RISK_GATEWAY_URL_MARKERS
    assert "/risk/challenge" in sp.RISK_GATEWAY_URL_MARKERS

    real_rate_limit_url = "https://us.shein.com/risk/action/limit?risk-id=E4913845744991097345"
    assert any(marker in real_rate_limit_url for marker in sp.RISK_GATEWAY_URL_MARKERS), (
        "an engine's page.url/current_url check must flag the real rate-limit redirect as blocked"
    )
    # risk-id= alone must NOT be a marker (see the module-docstring note on
    # why) — it's shared across both incidents, so using it standalone
    # would be a false-positive risk on any URL from either gateway with
    # a DIFFERENT, as-yet-unseen path.
    assert not any(m == "risk-id=" for m in sp.BOT_CHALLENGE_MARKERS)

    synthetic_rate_limit_html = (
        "<!DOCTYPE html><html><head><title></title></head>"
        "<body>window.location = 'https://us.shein.com/risk/action/limit?risk-id=abc123'</body></html>"
    )
    assert captcha_solver.detect_from_html(synthetic_rate_limit_html, sp.BOT_CHALLENGE_MARKERS)
    assert not captcha_solver.detect_from_html(synthetic_rate_limit_html), (
        "confirms the GENERIC markers alone do not catch this one either, same as /risk/challenge"
    )
    assert captcha_solver.identify_widget(synthetic_rate_limit_html) is None, (
        "there is nothing to solve on a rate-limit page — identify_widget finding nothing is correct, "
        "not a gap, and solve_when_blocked's 'detected_unidentified_widget' branch is what an engine "
        "sees for it"
    )


@check("all three engines' page.url/current_url block-check uses RISK_GATEWAY_URL_MARKERS, not a hardcoded single incident (parity gap this would silently reopen if one engine reverted)")
def _():
    for path in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "RISK_GATEWAY_URL_MARKERS" in src, f"{path} lost the shared risk-gateway URL check"
        assert 'if "/risk/challenge" in' not in src, (
            f"{path} has a stale hardcoded /risk/challenge-only URL check that bypasses "
            f"RISK_GATEWAY_URL_MARKERS — /risk/action/limit would silently stop being caught"
        )


@check("all three engines' round loop corroborates a post-round-0 BOT_CHALLENGE_MARKERS match against that round's own current URL before trusting it (added 2026-09-22 — a real live run's stale 'originalUrl' SSR text kept matching BOT_CHALLENGE_MARKERS on rounds AFTER the browser had already moved off the risk gateway onto an unrelated page, misreporting captcha-solve failures 5 rounds running; see shein_parser.py's module docstring)")
def _():
    for path in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "not re-flagging this round as a captcha block" in src, (
            f"{path} lost the stale-marker corroboration fix — a captcha marker match on ANY "
            f"round would be trusted again even after the browser has moved off the risk gateway"
        )
        assert "round_num > 0" in src, (
            f"{path}'s corroboration must be scoped to rounds AFTER 0 — round 0 stays "
            f"unconditionally trusted, since it's corroborated by the page.url check that "
            f"already runs right after the initial navigation, before content can go stale"
        )
        # The solve attempt itself must now be gated on the (corroborated)
        # captcha_detected flag, not called unconditionally every round —
        # otherwise solve_when_blocked's OWN internal marker check (same
        # markers, same possibly-stale html) reproduces the exact same
        # false positive one layer down, making the corroboration above a
        # no-op for the actual bug that was observed live.
        assert "captcha_result = None" in src, (
            f"{path}'s captcha-solve call must default to skipped (None) and only run when "
            f"captcha_detected is still true after url corroboration"
        )


@check("all three engines implement '--block-retries' — retry a blocked/zero-product outcome on the SAME browser/session before giving up, rather than declaring failure on the first hit (added 2026-09-22, directly prompted by Roman asking why this repo can't clear SHEIN's defenses the way sibling family members clear theirs — etsy-scraper's own README documents measuring exactly this: one DataDome profile refused (t=bv) twice, then cleared from the third attempt on — 'retry before you rotate, use a handful rather than minting one per run')")
def _():
    for path in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "--block-retries" in src, f"{path} is missing the --block-retries flag — engine parity gap"
        assert "block_attempt" in src, (
            f"{path} is missing the retry loop itself (not just the flag) around its scrape_fn call"
        )
        assert "retry before you rotate" in src.lower(), (
            f"{path}'s --block-retries help text lost the sibling-repo justification — this is not "
            f"an arbitrary knob, it's a documented, measured pattern from etsy-scraper's own README"
        )


@check("captcha_solver.identify_widget extracts a Turnstile sitekey")
def _():
    html = '<div class="cf-turnstile" data-sitekey="0x4AAA_example"></div>'
    signal = captcha_solver.identify_widget(html)
    assert signal is not None
    assert signal.captcha_type == captcha_solver.CaptchaType.CLOUDFLARE_TURNSTILE
    assert signal.sitekey == "0x4AAA_example"


@check("captcha_solver.identify_widget finds a reCAPTCHA v2 sitekey that lives in a JS config var, not static markup (added 2026-09-21, live on shein.com — see README 'Known limitations')")
def _():
    # Real shape confirmed live via the built-in browser tool, 2026-09-21,
    # in direct response to Roman asking whether other captcha vendors
    # might be in play besides the suspected GeeTest one: shein.com loads
    # https://www.google.com/recaptcha/api.js and has a live
    # window.grecaptcha v2 object (render/execute/getResponse/reset/
    # ready — confirmed NOT .enterprise) on every ordinary page, but the
    # real sitekey (window.gbCommonInfo.GOOGLE_VERIFY_SITEKEY =
    # "6LcoBR4UAAAAAIi5xU3U_q37C3nFaSckeMaT-P5j") lives only in a JS
    # config object, never in a static `<div class="g-recaptcha"
    # data-sitekey="...">` — the ONLY shape _SITEKEY_PATTERNS[RECAPTCHA_V2]
    # could see before this fix, meaning the old code would have silently
    # never even DETECTED this real, live, confirmed captcha vendor.
    html = (
        '<script src="https://www.google.com/recaptcha/api.js" async></script>'
        '<script>window.gbCommonInfo = {"GOOGLE_VERIFY_SITEKEY":'
        '"6LcoBR4UAAAAAIi5xU3U_q37C3nFaSckeMaT-P5j","OTHER_KEY":1};</script>'
    )
    signal = captcha_solver.identify_widget(html)
    assert signal is not None, "must detect the sitekey even though it's not in static markup"
    assert signal.captcha_type == captcha_solver.CaptchaType.RECAPTCHA_V2
    assert signal.sitekey == "6LcoBR4UAAAAAIi5xU3U_q37C3nFaSckeMaT-P5j"

    # The v3 loader (sitekey in the `render=` query param) must still win
    # when it's the one actually present — this fallback must not steal
    # v3's own, more specific signal.
    html_v3 = '<script src="https://www.google.com/recaptcha/api.js?render=6LcoBR4UAAAAAIi5xU3U_q37C3nFaSckeMaT-P5j"></script>'
    signal_v3 = captcha_solver.identify_widget(html_v3)
    assert signal_v3.captcha_type == captcha_solver.CaptchaType.RECAPTCHA_V3

    # No false positive: a sitekey-shaped string with no recaptcha loader
    # anywhere on the page must not fire — this is a fallback GATED on the
    # loader being present, not an unconditional sitekey-shaped-string
    # scan of the whole page.
    html_no_loader = "<p>unrelated id 6LcoBR4UAAAAAIi5xU3U_q37C3nFaSckeMaT-P5j in some other context</p>"
    assert captcha_solver.identify_widget(html_no_loader) is None


@check("captcha_solver GeeTest support (added 2026-09-21, see module docstring): v4 and v3 both identify and build the correct 2Captcha task")
def _():
    # These two HTML shapes are UNCONFIRMED best-effort (GeeTest's own
    # public integration docs), not a real shein.com capture — see
    # captcha_solver.py's module-level comment above _GEETEST_V4_ID_PATTERNS.
    # This test only proves the plumbing (extraction -> task payload)
    # works for the documented shape, not that shein.com's real widget
    # matches it.
    v4_html = '<div class="geetest_captcha_button" data-captcha-id="0123456789abcdef0123456789abcdef"></div>'
    v4_signal = captcha_solver.identify_widget(v4_html)
    assert v4_signal is not None
    assert v4_signal.captcha_type == captcha_solver.CaptchaType.GEETEST_V4
    assert v4_signal.captcha_id == "0123456789abcdef0123456789abcdef"
    v4_task = captcha_solver._task_payload(v4_signal, "https://us.shein.com/pdsearch/dress/", proxyless=True)
    assert v4_task == {
        "type": "GeeTestV4TaskProxyless",
        "websiteURL": "https://us.shein.com/pdsearch/dress/",
        "captchaId": "0123456789abcdef0123456789abcdef",
    }

    v3_html = "<script>initGeetest({gt: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', challenge: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'});</script>"
    v3_signal = captcha_solver.identify_widget(v3_html)
    assert v3_signal is not None
    assert v3_signal.captcha_type == captcha_solver.CaptchaType.GEETEST_V3
    assert v3_signal.gt == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert v3_signal.challenge == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    v3_task = captcha_solver._task_payload(v3_signal, "https://us.shein.com/pdsearch/dress/", proxyless=True)
    assert v3_task == {
        "type": "GeeTestTaskProxyless",
        "websiteURL": "https://us.shein.com/pdsearch/dress/",
        "gt": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "challenge": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    }

    # A page with neither shape (e.g. this repo's own real
    # shein_risk_challenge_real.html capture, which never got as far as
    # rendering an actual widget) correctly finds nothing to solve.
    assert captcha_solver.identify_widget("<html><body>plain page</body></html>") is None


@check("scraper_api_client.solve_and_wait falls back to a JSON-encoded solution for a multi-field result (GeeTest), instead of crashing")
def _():
    # GeeTest's solution has no single token/gRecaptchaResponse field
    # (see scraper_api_client.solve_and_wait's docstring) — this proves
    # the fallback path returns something usable rather than raising
    # TwoCaptchaError the way it would have before this change.
    client = scraper_api_client.TwoCaptchaClient(api_key="fake-key-for-test")

    class _FakeCreated:
        task_id = 1

    def _fake_create_task(task):
        return _FakeCreated()

    def _fake_get_task_result(created):
        return {
            "status": "ready",
            "solution": {
                "lot_number": "20260921aaaa",
                "pass_token": "bbbb",
                "gen_time": "1789990000",
                "captcha_output": "cccc",
            },
        }

    client.create_task = _fake_create_task
    client.get_task_result = _fake_get_task_result
    result = client.solve_and_wait({"type": "GeeTestV4TaskProxyless"}, poll_interval=0)
    parsed = json.loads(result)
    assert parsed["lot_number"] == "20260921aaaa"
    assert parsed["pass_token"] == "bbbb"


@check("captcha_solver.build_injection_script (added 2026-09-21, closing the injection gap Roman flagged): correct JS per widget type, None where there's no generic injection point")
def _():
    # Same honesty caveat as everywhere else in this file: these are each
    # widget's own STANDARD, publicly-documented client-integration
    # convention, never anything confirmed against a real shein.com (or
    # any family-site) widget capture — see build_injection_script's own
    # docstring. This check only proves the JS TEXT is built correctly
    # for a given (captcha_type, solution) pair, not that any real widget
    # actually reads the field/callback it targets.
    turnstile = captcha_solver.build_injection_script(captcha_solver.CaptchaType.CLOUDFLARE_TURNSTILE, "tok-abc")
    assert turnstile is not None
    assert "cf-turnstile-response" in turnstile
    assert json.dumps("tok-abc") in turnstile

    recaptcha_v2 = captcha_solver.build_injection_script(captcha_solver.CaptchaType.RECAPTCHA_V2, "tok-def")
    assert recaptcha_v2 is not None
    assert "g-recaptcha-response" in recaptcha_v2
    assert json.dumps("tok-def") in recaptcha_v2

    hcaptcha = captcha_solver.build_injection_script(captcha_solver.CaptchaType.HCAPTCHA, "tok-ghi")
    assert hcaptcha is not None
    assert "h-captcha-response" in hcaptcha

    # reCAPTCHA v3 is invisible and its token is typically consumed
    # straight into the SITE'S OWN JS (often an XHR), never read back off
    # a DOM element — guessing site-specific consumption code would
    # violate this shared module's own no-site-knowledge charter, so this
    # deliberately returns None rather than a script that does nothing.
    assert captcha_solver.build_injection_script(captcha_solver.CaptchaType.RECAPTCHA_V3, "tok-jkl") is None

    # GeeTest's solution is several fields together (see
    # scraper_api_client.solve_and_wait's docstring) — the injection
    # script must JSON.parse the JSON-encoded solution string and try
    # both prefixed/unprefixed field-name conventions.
    v3_solution = json.dumps({"challenge": "chal1", "validate": "val1", "seccode": "sec1"})
    geetest_v3 = captcha_solver.build_injection_script(captcha_solver.CaptchaType.GEETEST_V3, v3_solution)
    assert geetest_v3 is not None
    assert "geetest_challenge" in geetest_v3 and "geetest_validate" in geetest_v3 and "geetest_seccode" in geetest_v3
    assert "JSON.parse" in geetest_v3

    v4_solution = json.dumps({"captcha_id": "cid1", "lot_number": "lot1", "pass_token": "pt1", "gen_time": "gt1", "captcha_output": "co1"})
    geetest_v4 = captcha_solver.build_injection_script(captcha_solver.CaptchaType.GEETEST_V4, v4_solution)
    assert geetest_v4 is not None
    assert "__2captcha_geetest_v4_solution" in geetest_v4
    assert "geetest_v4_solved" in geetest_v4


@check("all three engines actually call build_injection_script from their 'solved' branch — the gap Roman asked about ('в любых случаях использование решения должно быть') stays closed, not silently regressed")
def _():
    for path in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "build_injection_script" in src, f"{path} no longer imports/calls build_injection_script"
        assert "CaptchaType(result[\"captcha_type\"])" in src, f"{path} doesn't build a CaptchaType from the solved result"


@check("EVERY function that takes an 'autosolve' parameter actually calls the Captcha.setAutoSolve helper somewhere in its body — playwright/puppeteer only, selenium is exempt (cannot authenticate --cdp-endpoint at all, CLAUDE.md §6). Regression test for a real 2026-09-22 gap: puppeteer_scraper.py's scrape_product_page() took `autosolve` and silently never used it, so a direct --url <product page> run over --cdp-endpoint never armed auto-solve, asymmetric with playwright_scraper.py (which armed it at BOTH its scrape_search and scrape_product_page call sites) and asymmetric with puppeteer's own scrape_search(). Roman's flippa-scraper spec ('на любой странице... авторешение... должно попытаться её решить') is exactly this requirement, generalized to 'every page', so this is now enforced here rather than left to be rediscovered per repo.")
def _():
    import ast as _ast

    HELPER_NAME = "_enable_scraping_browser_auto_solve"
    for path in ("playwright_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        tree = _ast.parse(src, filename=path)
        checked_any = False
        for node in _ast.walk(tree):
            if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            arg_names = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
            if "autosolve" not in arg_names:
                continue
            if node.name == HELPER_NAME.lstrip("_"):  # never applies, defensive only
                continue
            checked_any = True
            calls_helper = any(
                isinstance(n, _ast.Call)
                and (
                    (isinstance(n.func, _ast.Name) and n.func.id == HELPER_NAME)
                    or (isinstance(n.func, _ast.Attribute) and n.func.attr == HELPER_NAME)
                )
                for n in _ast.walk(node)
            )
            assert calls_helper, (
                f"{path}: {node.name}() takes an 'autosolve' parameter but never calls "
                f"{HELPER_NAME}() — captcha auto-solve would silently never be armed on "
                f"this page/navigation path when --cdp-endpoint + --solve-captcha are set."
            )
        assert checked_any, f"{path}: expected at least one function with an 'autosolve' parameter (test itself may be stale)"


@check("scrape_product_page() actually attempts captcha solving in all three engines — regression test for a real, README-documented 2026-09-22 gap: only scrape_search()'s round loop ever called _maybe_solve_captcha, so a direct --url <product page> run never tried to solve a captcha at all, even with --twocaptcha-key configured and --solve-captcha not 'off'. Fixed by adding the same call to scrape_product_page(), with a product-page-shaped 'already has real content' check (did sp.parse_product_page() succeed?) instead of scrape_search()'s sp.count_result_cards default, which would misfire on every product page (zero search-result cards there by definition) — see _maybe_solve_captcha's own updated docstring in each engine.")
def _():
    import ast as _ast

    HELPER_NAME = "_maybe_solve_captcha"
    for path in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        tree = _ast.parse(src, filename=path)
        target = None
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and node.name == "scrape_product_page":
                target = node
                break
        assert target is not None, f"{path}: no scrape_product_page() found (test itself may be stale)"
        calls_helper = any(
            isinstance(n, _ast.Call)
            and (
                (isinstance(n.func, _ast.Name) and n.func.id == HELPER_NAME)
                or (isinstance(n.func, _ast.Attribute) and n.func.attr == HELPER_NAME)
            )
            for n in _ast.walk(target)
        )
        assert calls_helper, f"{path}: scrape_product_page() never calls {HELPER_NAME}() — a captcha on a product page would never be solved"
        # And it must NOT silently inherit the search-page-shaped default —
        # every call site inside scrape_product_page must pass its own
        # count_product_links override.
        calls = [
            n for n in _ast.walk(target)
            if isinstance(n, _ast.Call)
            and (
                (isinstance(n.func, _ast.Name) and n.func.id == HELPER_NAME)
                or (isinstance(n.func, _ast.Attribute) and n.func.attr == HELPER_NAME)
            )
        ]
        for call in calls:
            kw_names = {kw.arg for kw in call.keywords}
            assert "count_product_links" in kw_names, (
                f"{path}: scrape_product_page()'s call to {HELPER_NAME}() doesn't pass "
                f"count_product_links — it would silently inherit the search-page-shaped "
                f"sp.count_result_cards default, which reads every product page as blocked."
            )


@check("BEHAVIORAL proof (not just structural) of the same fix: solve_when_blocked with the OLD search-page-shaped count_product_links default (sp.count_result_cards, always 0 on a product page) misreads a COMPLETELY NORMAL product page as '0 products present' the instant any generic marker is on it — and the confirmed-real, site-wide reCAPTCHA v2 loader (README 'Known limitations') is exactly such a marker. That would have meant every single product-page scrape either logged a false 'unidentified widget' warning, or — if a real sitekey happens to also be on the page (also confirmed common) — actually called 2Captcha's PAID createTask API against a page that was never blocked at all. The product-page-shaped count (did sp.parse_product_page() succeed?) correctly recognises the page is fine and skips solving entirely.")
def _():
    html = (
        '<html><head>'
        '<script src="https://www.google.com/recaptcha/api.js"></script>'
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"ProductGroup","name":"Test Dress",'
        '"hasVariant":[{"@type":"Product","sku":"33704388","name":"Test Dress",'
        '"offers":{"@type":"Offer","price":"19.99","priceCurrency":"USD"}}]}'
        '</script></head><body>real product content here</body></html>'
    )

    class _FakeClient:
        api_key = "fake"

    assert captcha_solver.detect_from_html(html, sp.BOT_CHALLENGE_MARKERS), "test fixture must trip the marker check"
    assert sp.count_result_cards(html) == 0, "a product page must have zero search-result cards, by definition"
    assert sp.parse_product_page(html, url="https://x"), "test fixture must parse as a real product"

    old_shape = captcha_solver.solve_when_blocked(
        client=_FakeClient(), page_url="https://x", html=html,
        count_product_links=sp.count_result_cards, extra_markers=sp.BOT_CHALLENGE_MARKERS,
    )
    new_shape = captcha_solver.solve_when_blocked(
        client=_FakeClient(), page_url="https://x", html=html,
        count_product_links=lambda h: 1 if sp.parse_product_page(h, url="https://x") else 0,
        extra_markers=sp.BOT_CHALLENGE_MARKERS,
    )
    assert old_shape["action"] != "skipped_products_present", (
        "this assertion documents the OLD bug shape, not a requirement — if this ever starts "
        "failing it means sp.count_result_cards started recognising product pages, which would "
        "make the whole fix (and this test) moot, not broken"
    )
    assert new_shape["action"] == "skipped_products_present", (
        f"product-page-shaped count_product_links must recognise a normal, already-parsing "
        f"product page and skip solving — got {new_shape['action']!r} instead"
    )


# --------------------------------------------------------------------------- #
# env_config — SHEIN_* keys, placeholder detection, precedence (family-shared)
# --------------------------------------------------------------------------- #
@check("env_config.ENV_KEYS matches .env.example exactly, in both directions")
def _():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = {line.split("=", 1)[0] for line in example.splitlines() if "=" in line and not line.startswith("#")}
    assert documented == set(env_config.ENV_KEYS), (documented, set(env_config.ENV_KEYS))


@check("env_config uses SHEIN_ prefixed keys, not a leftover LIDL_/other-site name")
def _():
    for key in env_config.ENV_KEYS:
        assert key == "TWOCAPTCHA_KEY" or key.startswith("SHEIN_"), f"unexpected env key {key!r}"


@check("env_config._is_placeholder treats a braced {...} fragment as unset")
def _():
    assert env_config._is_placeholder("")
    assert env_config._is_placeholder(None)
    assert env_config._is_placeholder("{login}-zone-scraping_browser:{password}@cb.2captcha.com")
    assert not env_config._is_placeholder("a-real-looking-value-123")


@check("env_config.apply_env never overrides an explicitly-set CLI flag")
def _():
    import argparse
    import os as _os
    ns = argparse.Namespace(proxy="http://explicit:pass@host:1")
    _os.environ["SHEIN_PROXY"] = "http://from-env:pass@host:2"
    try:
        env_config.apply_env(ns, dotenv_path="/nonexistent/.env")
        assert ns.proxy == "http://explicit:pass@host:1"
    finally:
        del _os.environ["SHEIN_PROXY"]


# --------------------------------------------------------------------------- #
# shein_parser — URL building, sku, confirmed-real embedded state, JSON-LD,
# DOM fallback, robots.txt
# --------------------------------------------------------------------------- #
@check("search_url builds the confirmed-real /pdsearch/<query>/ URL, query takes priority over category")
def _():
    url = sp.search_url(query="summer dress", category_path="Women Jeans-c-1934.html")
    assert url == "https://us.shein.com/pdsearch/summer%20dress/"


@check("search_url builds the confirmed-real {Category}-c-{id}.html URL when no query is given")
def _():
    url = sp.search_url(category_path="Women Jeans-c-1934.html")
    assert url == "https://us.shein.com/Women Jeans-c-1934.html"


@check("search_url raises without a query or category")
def _():
    try:
        sp.search_url()
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


@check("diagnose_unexpected_page (added 2026-09-21, prompted by Roman's own first live engine run) distinguishes a real search page from a page that clearly isn't one")
def _():
    # The real shape found live: a fresh, cookie-less Playwright context's
    # first request to a confirmed-correct /pdsearch/ URL got a page with
    # an EMPTY <title> and no bffProductsInfo/pdsearch marker anywhere —
    # not a /risk/challenge redirect, not a >=400 status, just... a
    # different page. This is a reduced, clearly-synthetic reproduction of
    # that real shape's DIAGNOSTIC SURFACE (title + marker presence), not
    # a scrub of the actual ~1.58MB capture (which carries third-party
    # tracker noise not worth committing here) — see
    # shein_parser.diagnose_unexpected_page's own docstring for the real
    # incident this documents.
    not_a_search_page = "<!DOCTYPE html><html><head><title></title></head><body>some other shein.com page, no search markers here</body></html>"
    diag = sp.diagnose_unexpected_page(not_a_search_page)
    assert "title=''" in diag
    assert "search-page-markers-present=False" in diag

    real_search_page = '<html><head><title>Search summer dress | SHEIN USA</title></head><body><script>window.gbRawData={"results":{"bffProductsInfo":{"products":[]}}}</script></body></html>'
    diag2 = sp.diagnose_unexpected_page(real_search_page)
    assert "Search summer dress" in diag2
    assert "search-page-markers-present=True" in diag2


@check("all three engines log a diagnostic (not silence) when zero products are found but the page wasn't flagged as blocked — parity gap Roman's live run exposed (Playwright had this, Selenium/Puppeteer didn't)")
def _():
    for path in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "diagnose_unexpected_page" in src, f"{path} doesn't call the new diagnostic — silent zero-products gap regressed"
        assert 'result.source_used == "none" and round_num == 0 and not blocked' in src, (
            f"{path} is missing (or changed) the trigger condition for the diagnostic"
        )


@check("product_url builds the confirmed-real {slug}-p-{goods_id}.html shape")
def _():
    url = sp.product_url("dsbayvkj", "33704388")
    assert url == "https://us.shein.com/dsbayvkj-p-33704388.html"


@check("make_sku prefers goods_id over a URL fingerprint, and is deterministic")
def _():
    a = sp.make_sku("458057728", "https://us.shein.com/x-p-458057728.html")
    b = sp.make_sku("458057728", "https://us.shein.com/y-p-458057728.html")
    assert a == b == "458057728"
    c1 = sp.make_sku(None, "https://us.shein.com/dress-p-1.html")
    c2 = sp.make_sku(None, "https://us.shein.com/dress-p-1.html")
    c3 = sp.make_sku(None, "https://us.shein.com/other-p-2.html")
    assert c1 == c2, "same URL must fingerprint to the same sku across runs"
    assert c1 != c3


@check("is_disallowed_path matches every real robots.txt Disallow prefix")
def _():
    for path in ("/user/settings", "/cart/", "/geetest/foo", "/atomic/x", "/abt/userinfo", "/aimtell-worker.js", "/coupon/getCouponListForOrder"):
        assert sp.is_disallowed_path(path), f"{path} should be disallowed"
        assert sp.is_disallowed_path(f"https://us.shein.com{path}")
    assert not sp.is_disallowed_path("/pdsearch/dress/")
    assert not sp.is_disallowed_path("/dsbayvkj-p-33704388.html")


# window.gbRawData fixture — field NAMES and SHAPE match the real 2026-09-21
# capture exactly (see shein_parser.py module docstring); the actual
# product/brand/price VALUES are invented for this test row.
_GB_RAW_DATA_FIXTURE = {
    "currency": "USD",
    "results": {
        "sum": 2,
        "bffProductsInfo": {
            "result_count": 2,
            "products": [
                {
                    "goods_id": "111111111",
                    "goods_sn": "sz111111111",
                    "goods_name": "Test Floral Print {Wrap} Dress",  # deliberately contains braces
                    "goods_url_name": "Test-Floral-Print-Wrap-Dress",
                    "goods_img": "//img.ltwebstatic.com/example1.jpg",
                    "cat_id": "12475",
                    "cate_name": "Women Mini Dresses",
                    "store_code": "9998887",
                    "retailPrice": {"amount": "25.00", "amountWithSymbol": "$25.00"},
                    "salePrice": {"amount": "19.99", "amountWithSymbol": "$19.99"},
                    "retailDiscountPercent": "20",
                    "premiumFlagNew": {"brandName": "TestBrand"},
                    "is_on_sale": 1,
                    "soldOutStatus": False,
                    "stock": "5",
                    "is_clearance": "0",
                    "quickship": "1",
                    "comment_rank_average": "4.71",
                    "comment_num": 128,
                },
                {
                    "goods_id": "222222222",
                    "goods_sn": "sz222222222",
                    "goods_name": "Test Solid Tank Top",
                    "goods_url_name": "Test-Solid-Tank-Top",
                    "goods_img": "//img.ltwebstatic.com/example2.jpg",
                    "cat_id": "12480",
                    "cate_name": "Women Tank Tops",
                    "store_code": "9998888",
                    "retailPrice": {"amount": "12.00", "amountWithSymbol": "$12.00"},
                    "salePrice": {"amount": "12.00", "amountWithSymbol": "$12.00"},
                    "retailDiscountPercent": "0",
                    "premiumFlagNew": {"brandName": "SHEIN"},
                    "is_on_sale": 0,
                    "soldOutStatus": True,
                    "stock": "0",
                    "is_clearance": "0",
                    "quickship": "0",
                    "comment_rank_average": "4.20",
                    "comment_num": 9,
                },
            ],
        },
    },
}

_SEARCH_PAGE_HTML = (
    "<html><head><title>Search test</title></head><body>"
    "<script>window.gbRawData = " + json.dumps(_GB_RAW_DATA_FIXTURE) + ";</script>"
    "</body></html>"
)


@check("_extract_balanced_json_assignment correctly parses a nested object with braces inside a string value")
def _():
    # The first product's title deliberately contains literal { } characters
    # — a naive non-greedy regex over "window.gbRawData = {...}" would
    # truncate at the FIRST closing brace and fail to parse at all.
    data = sp.extract_gb_raw_data(_SEARCH_PAGE_HTML)
    assert data is not None
    assert data["results"]["bffProductsInfo"]["products"][0]["goods_name"] == "Test Floral Print {Wrap} Dress"


@check("parse_search_results uses window.gbRawData as the PRIMARY source, both from a passed-in dict and from HTML text")
def _():
    # Path A: raw_data passed directly (the engines' live page.evaluate() path)
    result_a = sp.parse_search_results("<html></html>", max_results=10, raw_data=_GB_RAW_DATA_FIXTURE)
    assert result_a.source_used == "gb_raw_data"
    assert len(result_a.products) == 2
    assert result_a.total_available == 2

    # Path B: extracted from raw HTML text (the offline/fallback path)
    result_b = sp.parse_search_results(_SEARCH_PAGE_HTML, max_results=10)
    assert result_b.source_used == "gb_raw_data"
    assert len(result_b.products) == 2

    p = next(p for p in result_a.products if p.sku == "111111111")
    assert p.title == "Test Floral Print {Wrap} Dress"
    assert p.category == "Women Mini Dresses"
    assert p.brand == "TestBrand"
    assert p.price == 19.99
    assert p.original_price == 25.00
    assert p.discount_pct == 20.0
    assert p.currency == "USD"
    assert p.price_source == "embedded_json"
    assert p.rating == 4.71
    assert p.review_count == 128
    assert p.store_code == "9998887"
    assert p.in_stock is True
    assert p.is_clearance is False
    assert p.quickship is True
    assert p.product_url == "https://us.shein.com/Test-Floral-Print-Wrap-Dress-p-111111111.html"
    assert p.image_url == "https://img.ltwebstatic.com/example1.jpg"

    sold_out = next(p for p in result_a.products if p.sku == "222222222")
    assert sold_out.in_stock is False
    assert sold_out.original_price is None, "same as sale price — not a real 'was' price, correctly omitted"


@check("parse_search_results against a REAL live capture (2026-09-21, tests/fixtures/shein_search_live_dress_20260921.json) round-trips through finish_run() as a clean complete run")
def _():
    # Unlike every other fixture in this file, this one is not synthetic —
    # it's a real window.gbRawData snapshot fetched live from
    # https://us.shein.com/pdsearch/dress/ (see the fixture's own
    # "_provenance" field). This is this repo's first end-to-end
    # confirmation that shein_parser.py's PRIMARY path produces a clean,
    # complete output_writer.finish_run() result against genuinely fresh
    # live data, not just against a hand-built fixture — see CHANGELOG.md.
    real = json.loads((ROOT / "tests" / "fixtures" / "shein_search_live_dress_20260921.json").read_text(encoding="utf-8"))
    assert "_provenance" in real, "this fixture should carry its own provenance note"

    result = sp.parse_search_results("", max_results=10, raw_data=real)
    assert result.source_used == "gb_raw_data"
    assert len(result.products) == 10
    assert result.total_available and result.total_available > 10000, (
        "the real query this was captured from had tens of thousands of matches"
    )

    # Every real row parsed to a plausible product — never a null-filled
    # row silently passed off as a match (same check TESTING.md step 2
    # tells a contributor to do by hand; this makes it a permanent
    # regression test instead of a one-time manual read).
    for p in result.products:
        assert p.sku and p.sku.isdigit()
        assert p.source == "shein.com"
        assert p.title
        assert p.price is not None and p.price > 0
        assert p.currency == "USD"
        assert p.price_source == "embedded_json"
        assert p.product_url and p.product_url.startswith("https://us.shein.com/")
        assert p.in_stock is True  # every product in this real batch was in stock

    with tempfile.TemporaryDirectory() as tmp:
        out_path = str(Path(tmp) / "shein_live10.json")
        exit_code = output_writer.finish_run(
            products=result.products,
            out_path=out_path,
            fmt="json",
            engine="playwright",
            url="https://us.shein.com/pdsearch/dress/",
            pages_requested=1,
            pages_completed=1,
            failed_pages=[],
            blocked=False,
            remote_api_error=False,
            allow_empty=False,
            started_at=0.0,
            price_confirmed_pct=1.0,
        )
        assert exit_code == output_writer.EXIT_OK
        meta = json.loads(Path(out_path + ".meta.json").read_text(encoding="utf-8"))
        assert meta["status"] == "complete"
        assert meta["product_count"] == 10


@check("parse_search_results respects max_results and honors --allow-empty style zero-result input")
def _():
    result = sp.parse_search_results(_SEARCH_PAGE_HTML, max_results=1)
    assert len(result.products) == 1
    empty = sp.parse_search_results("<html>no data here</html>", max_results=10)
    assert empty.products == []
    assert empty.source_used == "none"


@check("total_result_count reads the confirmed-real sum/result_count fields")
def _():
    assert sp.total_result_count(_SEARCH_PAGE_HTML) == 2
    assert sp.total_result_count("<html></html>") is None


@check("count_result_cards counts real gbRawData products without a full parse")
def _():
    assert sp.count_result_cards(_SEARCH_PAGE_HTML) == 2
    assert sp.count_result_cards("<html>nothing</html>") == 0


@check("safe_parse_search_results degrades to an empty result instead of raising, on a malformed gbRawData shape")
def _():
    broken_html = "<html><script>window.gbRawData = {\"results\": \"not-a-dict\"};</script></html>"
    result = sp.safe_parse_search_results(broken_html, max_results=10)
    assert result.products == []
    assert result.source_used == "none"


# Confirmed-real product-DETAIL page JSON-LD (ProductGroup + hasVariant),
# trimmed to the fields this parser actually reads — see module docstring
# for the live capture this is based on. Brand/price values kept from the
# real capture (SHEIN's own house brand, real confirmed price).
_PRODUCT_DETAIL_JSON_LD_HTML = """
<html><body>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[]}
</script>
<script type="application/ld+json">
{"@context":"https://schema.org/","@type":"ProductGroup","name":"SHEIN ICON Women Embroidered Bowknot Washed Denim Jeans",
"url":"https://us.shein.com/dsbayvkj-p-33704388.html","brand":{"@type":"Brand","name":"SHEIN"},
"productGroupID":"z24022812625","image":["https://img.ltwebstatic.com/example.jpg"],"color":"Light Wash",
"hasVariant":[{"@type":"Product","sku":"I93s3po0gvt5","name":"... - Size 28",
"image":["https://img.ltwebstatic.com/example_405x552.jpg"],
"offers":{"@type":"Offer","priceCurrency":"USD","price":"20.71","itemCondition":"https://schema.org/NewCondition","availability":"https://schema.org/InStock"},"size":"28"}]}
</script>
</body></html>
"""


@check("extract_json_ld finds nothing product-shaped on a search page (confirmed real: only BreadcrumbList there)")
def _():
    nodes = sp.extract_json_ld(_SEARCH_PAGE_HTML)
    assert sp._find_product_group_node(nodes) is None


@check("parse_product_page reads the confirmed-real ProductGroup JSON-LD shape from a product-detail page")
def _():
    product = sp.parse_product_page(_PRODUCT_DETAIL_JSON_LD_HTML, url="https://us.shein.com/dsbayvkj-p-33704388.html")
    assert product is not None
    assert product.sku == "33704388"
    assert product.title == "SHEIN ICON Women Embroidered Bowknot Washed Denim Jeans"
    assert product.brand == "SHEIN"
    assert product.price == 20.71
    assert product.currency == "USD"
    assert product.price_source == "json_ld"
    assert product.image_url == "https://img.ltwebstatic.com/example.jpg"


@check("_resolve_start_url routes a product-detail URL to the JSON-LD path, and a search/category URL to the listing path")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        _, is_product = mod._resolve_start_url(mod.build_arg_parser().parse_args(["--url", "https://us.shein.com/dsbayvkj-p-33704388.html"]))
        assert is_product is True, f"{mod.__name__}: product URL misrouted"
        _, is_product2 = mod._resolve_start_url(mod.build_arg_parser().parse_args(["--query", "dress"]))
        assert is_product2 is False, f"{mod.__name__}: query misrouted to product-page path"


# --------------------------------------------------------------------------- #
# diff_runs — added/removed/changed, refuses a non-complete run
# --------------------------------------------------------------------------- #
@check("diff_runs reports added/removed/changed correctly against two real finish_run() outputs")
def _():
    with tempfile.TemporaryDirectory() as td:
        old_out, new_out = str(Path(td) / "old.json"), str(Path(td) / "new.json")
        output_writer.finish_run(
            products=[_mk_product("1", price=10.0), _mk_product("2", price=20.0)],
            out_path=old_out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=1,
            failed_pages=None, blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        output_writer.finish_run(
            products=[_mk_product("1", price=12.0), _mk_product("3", price=30.0)],
            out_path=new_out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=1,
            failed_pages=None, blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        result = diff_runs.diff(old_out, new_out)
        assert result["added"] == ["3"]
        assert result["removed"] == ["2"]
        assert len(result["changed"]) == 1 and result["changed"][0]["sku"] == "1"


@check("diff_runs refuses to compare a non-'complete' run")
def _():
    with tempfile.TemporaryDirectory() as td:
        old_out, new_out = str(Path(td) / "old.json"), str(Path(td) / "new.json")
        output_writer.finish_run(
            products=[_mk_product("1")], out_path=old_out, fmt="json", engine="test", url="u",
            pages_requested=2, pages_completed=1, failed_pages=[2],
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        output_writer.finish_run(
            products=[_mk_product("1")], out_path=new_out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        try:
            diff_runs.diff(old_out, new_out)
            raise AssertionError("expected a refusal — old run is 'partial', not 'complete'")
        except SystemExit:
            pass


# --------------------------------------------------------------------------- #
# scraper_api_client — family-shared, unmodified
# --------------------------------------------------------------------------- #
@check("scraper_api_client.TwoCaptchaClient._require_key rejects a missing/empty key")
def _():
    client = scraper_api_client.TwoCaptchaClient("")
    try:
        client._require_key()
        raise AssertionError("expected TwoCaptchaAuthError")
    except scraper_api_client.TwoCaptchaAuthError:
        pass


@check("scraper_api_client honors --captcha-api override, not the module-level API_BASE")
def _():
    client = scraper_api_client.TwoCaptchaClient("fakekey", api_base="https://mock.example.test")
    assert client.api_base == "https://mock.example.test"
    assert client.api_base != scraper_api_client.API_BASE


@check("all three engines define --scraper-api-cdp/--scraper-api-country/--scraper-api-profile-id, and pass cdp_url through to scraper_api_client.scrape_url (added 2026-09-28, closing the 'no captcha solving, no locale pinning' gap --scraper-api's own help text used to document as simply unsolved)")
def _():
    for path in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert '"--scraper-api-cdp"' in src, f"{path}: no --scraper-api-cdp flag"
        assert '"--scraper-api-country"' in src, f"{path}: no --scraper-api-country flag"
        assert '"--scraper-api-profile-id"' in src, f"{path}: no --scraper-api-profile-id flag"
        assert "cdp_url=cdp_url" in src, f"{path}: _scrape_via_scraper_api is not called with cdp_url"
        assert "scraping_browser_connection_url(" in src, f"{path}: --scraper-api-cdp never builds a Scraping Browser URL"
        assert "--scraper-api-cdp requires --scraper-api" in src, f"{path}: --scraper-api-cdp isn't guarded to require --scraper-api"


@check("BEHAVIORAL proof (not just structural) of the same fix, all three engines: --scraper-api-cdp actually builds a country/profile-pinned Scraping Browser URL and it reaches scraper_api_client.scrape_url's cdp_url argument; without the flag cdp_url stays None (regression: default --scraper-api behavior unchanged); --scraper-api-cdp without --scraper-api is EXIT_BAD_USAGE, not a silent no-op")
def _():
    engines = (
        (playwright_scraper, "playwright"),
        (selenium_scraper, "selenium"),
        (puppeteer_scraper, "puppeteer"),
    )
    for mod, engine_name in engines:
        captured = {}
        original = scraper_api_client.TwoCaptchaClient.scrape_url
        original_connection = scraper_api_client.TwoCaptchaClient.scraping_browser_connection_url

        def _fake_connection(self, *, country=None, profile_id=None, account_id=None):
            captured["connection_args"] = (country, profile_id, account_id)
            return f"ws://browser-zone-scraping_browser-country-{country}-pid-{profile_id}:password@cb.2captcha.com:9222"

        def _fake_scrape_url(self, url, *, data_format="raw", timeout=60, wait_for=None, cdp_url=None):
            captured["cdp_url"] = cdp_url
            return scraper_api_client.ScrapeResult(
                target_status=200, headers={}, body="<html><script>window.gbRawData={}</script></html>",
            )

        scraper_api_client.TwoCaptchaClient.scrape_url = _fake_scrape_url
        scraper_api_client.TwoCaptchaClient.scraping_browser_connection_url = _fake_connection
        try:
            parser = mod.build_arg_parser()

            # --scraper-api-cdp without --scraper-api: EXIT_BAD_USAGE, all
            # three engines (checked before any network-shaped call is made).
            bad_args = parser.parse_args(["--url", "https://us.shein.com/x-p-1.html", "--scraper-api-cdp"])
            rc = asyncio_run_maybe(mod, bad_args)
            assert rc == output_writer.EXIT_BAD_USAGE, f"{engine_name}: --scraper-api-cdp without --scraper-api should be EXIT_BAD_USAGE, got {rc}"

            # With the flag: cdp_url is built and reaches scrape_url,
            # carrying the requested country + profile id.
            with tempfile.TemporaryDirectory() as td:
                on_args = parser.parse_args([
                    "--url", "https://us.shein.com/dsbayvkj-p-33704388.html",
                    "--twocaptcha-key", "fake-key-for-test-only",
                    "--scraper-api", "--scraper-api-cdp",
                    "--scraper-api-country", "us", "--scraper-api-account-id", "7",
                    "--scraper-api-profile-id", "smoke-test-profile",
                    "--out", str(Path(td) / "out.json"), "--allow-empty",
                ])
                asyncio_run_maybe(mod, on_args)
            cdp_url = captured.get("cdp_url")
            assert cdp_url, f"{engine_name}: --scraper-api-cdp did not produce a cdp_url"
            assert "-country-us" in cdp_url, f"{engine_name}: cdp_url does not carry the requested country: {cdp_url!r}"
            assert "-pid-smoke-test-profile" in cdp_url, f"{engine_name}: cdp_url does not carry the requested profile id: {cdp_url!r}"
            assert captured["connection_args"] == ("us", "smoke-test-profile", 7)

            # Without the flag: unchanged from before this feature existed.
            captured.clear()
            with tempfile.TemporaryDirectory() as td:
                off_args = parser.parse_args([
                    "--url", "https://us.shein.com/dsbayvkj-p-33704388.html",
                    "--twocaptcha-key", "fake-key-for-test-only",
                    "--scraper-api",
                    "--out", str(Path(td) / "out.json"), "--allow-empty",
                ])
                asyncio_run_maybe(mod, off_args)
            assert captured.get("cdp_url") is None, f"{engine_name}: cdp_url should be None without --scraper-api-cdp, got {captured.get('cdp_url')!r}"
        finally:
            scraper_api_client.TwoCaptchaClient.scrape_url = original
            scraper_api_client.TwoCaptchaClient.scraping_browser_connection_url = original_connection


@check(
    "--scraper-api-cdp: an automatic ONE-TIME fallback to --scraper-api's plain default pool "
    "when the Scraping Browser session itself fails (a Scraper API HTTP-level error, not a "
    "normal blocked-with-zero-products outcome), all three engines. Three cases: (1) the "
    "cdp-routed attempt fails, the fallback attempt (cdp_url=None) succeeds -> EXIT_OK, exactly "
    "2 scrape_url calls, first with a real cdp_url, second with None; (2) both attempts fail -> "
    "EXIT_REMOTE_API_ERROR, still exactly 2 calls (never retried a second time, never amplified "
    "by --block-retries); (3) regression: plain --scraper-api with no --scraper-api-cdp hitting "
    "remote_api_error triggers no fallback at all -- exactly 1 call, EXIT_REMOTE_API_ERROR, "
    "unchanged from before this feature existed."
)
def _():
    engines = (
        (playwright_scraper, "playwright"),
        (selenium_scraper, "selenium"),
        (puppeteer_scraper, "puppeteer"),
    )
    for mod, engine_name in engines:
        original = scraper_api_client.TwoCaptchaClient.scrape_url
        original_connection = scraper_api_client.TwoCaptchaClient.scraping_browser_connection_url
        scraper_api_client.TwoCaptchaClient.scraping_browser_connection_url = (
            lambda self, *, country=None, profile_id=None, account_id=None:
            "ws://browser-zone-scraping_browser-country-us:password@cb.2captcha.com:9222"
        )

        # Case 1: cdp attempt fails, fallback (cdp_url=None) recovers.
        calls = []

        def _fake_recovers(self, url, *, data_format="raw", timeout=60, wait_for=None, cdp_url=None):
            calls.append(cdp_url)
            if cdp_url is not None:
                raise scraper_api_client.TwoCaptchaError("Scraper API returned HTTP 422: bad cdpurl")
            return scraper_api_client.ScrapeResult(target_status=200, headers={}, body=_SEARCH_PAGE_HTML)

        scraper_api_client.TwoCaptchaClient.scrape_url = _fake_recovers
        try:
            parser = mod.build_arg_parser()
            with tempfile.TemporaryDirectory() as td:
                args = parser.parse_args([
                    "--query", "summer dress",
                    "--twocaptcha-key", "fake-key-for-test-only",
                    "--scraper-api", "--scraper-api-cdp", "--scraper-api-country", "us",
                    "--out", str(Path(td) / "out.json"), "--allow-empty",
                ])
                rc = asyncio_run_maybe(mod, args)
            assert rc == output_writer.EXIT_OK, f"{engine_name}/recovers: expected EXIT_OK after a successful fallback with real search results, got {rc}"
            assert len(calls) == 2, f"{engine_name}/recovers: expected exactly 2 scrape_url calls, got {calls!r}"
            assert calls[0], f"{engine_name}/recovers: first call should carry a real cdp_url, got {calls[0]!r}"
            assert calls[1] is None, f"{engine_name}/recovers: second (fallback) call should have cdp_url=None, got {calls[1]!r}"
        finally:
            scraper_api_client.TwoCaptchaClient.scrape_url = original

        # Case 2: both the cdp attempt AND the fallback fail -> still just 2 calls total.
        calls2 = []

        def _fake_both_fail(self, url, *, data_format="raw", timeout=60, wait_for=None, cdp_url=None):
            calls2.append(cdp_url)
            raise scraper_api_client.TwoCaptchaError("Scraper API returned HTTP 500")

        scraper_api_client.TwoCaptchaClient.scrape_url = _fake_both_fail
        try:
            parser = mod.build_arg_parser()
            with tempfile.TemporaryDirectory() as td:
                args = parser.parse_args([
                    "--url", "https://us.shein.com/dsbayvkj-p-33704388.html",
                    "--twocaptcha-key", "fake-key-for-test-only",
                    "--scraper-api", "--scraper-api-cdp",
                    "--out", str(Path(td) / "out.json"), "--allow-empty",
                ])
                rc = asyncio_run_maybe(mod, args)
            assert rc == output_writer.EXIT_REMOTE_API_ERROR, f"{engine_name}/both-fail: expected EXIT_REMOTE_API_ERROR, got {rc}"
            assert len(calls2) == 2, f"{engine_name}/both-fail: expected exactly 2 scrape_url calls (one fallback, never repeated), got {calls2!r}"
        finally:
            scraper_api_client.TwoCaptchaClient.scrape_url = original
            scraper_api_client.TwoCaptchaClient.scraping_browser_connection_url = original_connection

        # Case 3: regression -- plain --scraper-api (no --scraper-api-cdp) never falls back.
        calls3 = []

        def _fake_plain_fails(self, url, *, data_format="raw", timeout=60, wait_for=None, cdp_url=None):
            calls3.append(cdp_url)
            raise scraper_api_client.TwoCaptchaError("Scraper API returned HTTP 500")

        scraper_api_client.TwoCaptchaClient.scrape_url = _fake_plain_fails
        try:
            parser = mod.build_arg_parser()
            with tempfile.TemporaryDirectory() as td:
                args = parser.parse_args([
                    "--url", "https://us.shein.com/dsbayvkj-p-33704388.html",
                    "--twocaptcha-key", "fake-key-for-test-only",
                    "--scraper-api",
                    "--out", str(Path(td) / "out.json"), "--allow-empty",
                ])
                rc = asyncio_run_maybe(mod, args)
            assert rc == output_writer.EXIT_REMOTE_API_ERROR, f"{engine_name}/plain: expected EXIT_REMOTE_API_ERROR, got {rc}"
            assert len(calls3) == 1, f"{engine_name}/plain: no --scraper-api-cdp means no fallback attempt, expected exactly 1 call, got {calls3!r}"
        finally:
            scraper_api_client.TwoCaptchaClient.scrape_url = original


@check("Playwright CDP uses the persistent profile context and closes only its own page")
def _():
    import asyncio

    class FakeContext:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    class FakePage:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    class FakeBrowser:
        def __init__(self):
            self.default = FakeContext()
            self.contexts = [self.default]
            self.new_context_calls = 0

        async def new_context(self, **kwargs):
            self.new_context_calls += 1
            return FakeContext()

    async def exercise():
        browser = FakeBrowser()
        context = await playwright_scraper._new_context(browser, None, None, reuse_default=True)
        assert context is browser.default
        assert browser.new_context_calls == 0
        page = FakePage()
        await playwright_scraper._close_scrape_page(page, context, reuse_default=True)
        assert page.closed and not context.closed

        local = await playwright_scraper._new_context(browser, None, None)
        assert local is not browser.default and browser.new_context_calls == 1
        page = FakePage()
        await playwright_scraper._close_scrape_page(page, local, reuse_default=False)
        assert page.closed and local.closed

    asyncio.run(exercise())


@check(
    "Playwright CDP falls back to a fresh context (not a crash) when the provider's "
    "session has recycled and browser.contexts comes back empty -- regression test for a "
    "real live crash on Roman's own machine 2026-09-29: two blocked --block-retries "
    "attempts against a real Browser API profile, then a third attempt's "
    "browser.contexts was empty and _new_context raised RuntimeError uncaught, crashing "
    "the whole run instead of degrading that one attempt (CLAUDE.md §6)."
)
def _():
    import asyncio

    class FakeContext:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    class FakeEmptyBrowser:
        """No persistent default context left -- as observed live after a
        provider-side session recycle."""
        def __init__(self):
            self.contexts = []
            self.new_context_calls = 0

        async def new_context(self, **kwargs):
            self.new_context_calls += 1
            return FakeContext()

    async def exercise():
        browser = FakeEmptyBrowser()
        context = await playwright_scraper._new_context(browser, None, None, reuse_default=True)
        assert browser.new_context_calls == 1, "must fall back to a fresh context, not raise"
        assert context is not None

    asyncio.run(exercise())


@check("Browser API supplies its own CDP credentials and selects an existing country/account")
def _():
    from unittest.mock import patch

    class Response:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    accounts = {"status": "OK", "data": {
        "0": {"id": 10, "country": "eu"},
        "1": {"id": 11, "country": "us"},
    }}
    calls = []

    def fake_get(url, *, params, timeout):
        assert url.endswith("/browser/accounts")
        assert params == {"key": "fake-key-for-test-only"}
        return Response(accounts)

    def fake_post(url, *, json, timeout):
        calls.append(json)
        assert url.endswith("/browser/connection")
        return Response({"status": "OK", "connectionUri": "ws://browser-login-zone-scraping_browser-country-us:browser-password@cb.2captcha.com:9222"})

    client = scraper_api_client.TwoCaptchaClient("fake-key-for-test-only")
    with patch.object(scraper_api_client.requests, "get", fake_get), patch.object(scraper_api_client.requests, "post", fake_post):
        uri = client.scraping_browser_connection_url(country="us", profile_id="existing-profile")
        assert uri.startswith("ws://browser-login-zone-scraping_browser-country-us:browser-password@")
        assert calls == [{"key": "fake-key-for-test-only", "accountId": 11, "profileId": "existing-profile"}]
        try:
            client.scraping_browser_connection_url(country="fr")
        except scraper_api_client.TwoCaptchaError:
            pass
        else:
            raise AssertionError("missing country must fail before a Scraper API request")
        assert len(calls) == 1


@check("all three Scraper API modes stop after one /risk/action/limit response even with block retries")
def _():
    engines = (playwright_scraper, selenium_scraper, puppeteer_scraper)
    original = scraper_api_client.TwoCaptchaClient.scrape_url
    calls = []

    def fake_scrape(self, url, *, data_format="raw", timeout=60, wait_for=None, cdp_url=None):
        calls.append(url)
        return scraper_api_client.ScrapeResult(
            target_status=200, headers={},
            body="<html><body>/risk/action/limit You have too many requests</body></html>",
        )

    scraper_api_client.TwoCaptchaClient.scrape_url = fake_scrape
    try:
        for mod in engines:
            calls.clear()
            with tempfile.TemporaryDirectory() as td:
                args = mod.build_arg_parser().parse_args([
                    "--query", "dress", "--twocaptcha-key", "fake-key-for-test-only",
                    "--scraper-api", "--block-retries", "2", "--out", str(Path(td) / "out.json"),
                ])
                rc = asyncio_run_maybe(mod, args)
            assert rc == output_writer.EXIT_BLOCKED, (mod.__name__, rc)
            assert len(calls) == 1, (mod.__name__, calls)
    finally:
        scraper_api_client.TwoCaptchaClient.scrape_url = original


@check(
    "main() never short-circuits on 'driver not installed' BEFORE calling a monkeypatched "
    "run() -- regression test mirroring tipranks-scraper's own such check, added here "
    "2026-09-28 after finding this file had no equivalent: every check above drives "
    "run()/asyncio_run_maybe(mod, args) directly, bypassing main() entirely, so a duplicate "
    "'driver is None' guard placed straight in main() -- the exact bug briefly shipped in "
    "tipranks-scraper's selenium_scraper.py/puppeteer_scraper.py on 2026-09-28, see CLAUDE.md "
    "§6 -- would pass every other check in this file while silently short-circuiting on a "
    "machine without the driver installed (this was independently confirmed NOT present here "
    "via a live run on Roman's own machine with selenium genuinely absent, but that only "
    "covered one engine, once; this check covers all three, every time this suite runs, "
    "regardless of what happens to be installed in whatever environment runs it). Forces the "
    "driver symbol to None here, and drives main() itself via sys.argv -- this repo's main() "
    "takes no argv parameter, unlike tipranks-scraper's -- rather than run() directly, so this "
    "can only pass for the right reason."
)
def _():
    import sys as _sys

    async def _fake_run_ok_async(args):
        return output_writer.EXIT_OK

    def _fake_run_ok_sync(args):
        return output_writer.EXIT_OK

    driver_attr = {
        playwright_scraper: "async_playwright",
        selenium_scraper: "webdriver",
        puppeteer_scraper: "pyppeteer_launch",
    }
    fake_run = {
        playwright_scraper: _fake_run_ok_async,
        selenium_scraper: _fake_run_ok_sync,
        puppeteer_scraper: _fake_run_ok_async,
    }

    original_argv = _sys.argv
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        attr = driver_attr[mod]
        original_driver = getattr(mod, attr)
        original_run = mod.run
        setattr(mod, attr, None)
        mod.run = fake_run[mod]
        try:
            with tempfile.TemporaryDirectory() as td:
                _sys.argv = [
                    "prog.py", "--query", "summer dress",
                    "--out", str(Path(td) / "out.json"), "--allow-empty",
                ]
                code = mod.main()
            assert code == output_writer.EXIT_OK, (
                f"{mod.__name__}: got {code} with {attr}=None -- main() short-circuited "
                f"before calling the monkeypatched run(), instead of reaching EXIT_OK (0)"
            )
        finally:
            setattr(mod, attr, original_driver)
            mod.run = original_run
            _sys.argv = original_argv


@check("the single credential scanner passes; skip gracefully if it isn't in this checkout")
def _():
    # Guarded like sample_output/pyproject-changelog checks elsewhere in
    # this file: a Docker build's COPY list is a deliberately stripped-
    # down context that never includes .github/ (CLAUDE.md §14 — the
    # image ships no test suite and no CI plumbing), so this must skip
    # rather than crash there. The offline CI job (a full checkout) is
    # what actually exercises this check.
    scanner = ROOT / ".github" / "ci_checks.py"
    if not scanner.exists():
        return
    import subprocess
    import sys as _sys

    result = subprocess.run(
        [_sys.executable, str(scanner)], cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"ci_checks.py failed:\n{result.stdout}\n{result.stderr}"


@check("credential scanner recognizes quoted and unquoted secret assignments, and does not flag a Python type-hint token as a secret")
def _():
    import importlib.util

    scanner = ROOT / ".github" / "ci_checks.py"
    if not scanner.exists():
        return
    spec = importlib.util.spec_from_file_location("shein_ci_checks", scanner)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    oauth_name = "CLAUDE_CODE" + "_OAUTH_TOKEN"
    api_name = "api" + "_key"
    value = "realvalue" + "123456"
    for line in (
        f'{oauth_name}="{value}"',
        f"{oauth_name}={value}",
        f"{api_name}: {value}",
    ):
        assert module.SECRET_ASSIGNMENT.search(line), line
    # Regression: `api_key: Optional[str]` in a function signature matches
    # the same NAME(:|=)VALUE shape a real unquoted secret assignment
    # would — this is what scraper_api_client.py's __init__ looks like,
    # and it must NOT be reported.
    hint_line = f"{api_name}: Optional[str]"
    match = module.SECRET_ASSIGNMENT.search(hint_line)
    assert match is not None  # the regex itself still matches the shape
    assert module._looks_like_type_hint(match.group(3))


@check(
    "_jittered_delay (added 2026-09-29, in response to Roman asking to reduce "
    "block risk): a real, shared per-engine helper -- not just a flag -- that "
    "spreads --scroll-delay/--retry-delay/--rate-limit-cooldown over "
    "[1-jitter, 1+jitter] so repeated waits aren't perfectly periodic. Tested "
    "against the REAL function in all three engines, not a reimplementation: "
    "bounds hold over many draws, jitter<=0 or base<=0 is a no-op passthrough, "
    "and it never returns a negative delay even at jitter=1.0."
)
def _():
    import random as _random

    for mod in (playwright_scraper, puppeteer_scraper, selenium_scraper):
        fn = mod._jittered_delay
        # jitter disabled -> exact passthrough
        assert fn(3.0, 0.0) == 3.0
        assert fn(3.0, -1.0) == 3.0
        # non-positive base -> exact passthrough regardless of jitter
        assert fn(0.0, 0.3) == 0.0
        assert fn(-1.0, 0.3) == -1.0
        # bounds hold over many draws, and it's actually random (not a
        # constant that happens to lie in range)
        _random.seed(1234)
        samples = [fn(10.0, 0.3) for _ in range(200)]
        assert all(7.0 <= s <= 13.0 for s in samples), f"{mod.__name__}: jitter out of [1-jitter,1+jitter] bounds"
        assert len(set(samples)) > 1, f"{mod.__name__}: _jittered_delay looks non-random"
        # even at jitter=1.0 (worst case, factor could reach 0) it never
        # goes negative
        assert all(fn(5.0, 1.0) >= 0.0 for _ in range(50))


@check(
    "all three engines wire --delay-jitter through EVERY --scroll-delay/"
    "--retry-delay sleep -- no bare, unjittered sleep(args.scroll_delay)/"
    "sleep(args.retry_delay) call site left over from before this change "
    "(added 2026-09-29; a parity gap here would silently make one engine's "
    "request timing perfectly periodic again while the others jitter)"
)
def _():
    import re as _re

    bare_sleep = _re.compile(r"sleep\(args\.(scroll_delay|retry_delay)\)")
    for path in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "--delay-jitter" in src, f"{path} is missing the --delay-jitter flag"
        bare = bare_sleep.findall(src)
        assert not bare, f"{path} still has a bare, unjittered sleep call: {bare}"
        assert src.count("_jittered_delay(args.scroll_delay") >= 1, f"{path}: scroll_delay never jittered"
        assert src.count("_jittered_delay(args.retry_delay") >= 1, f"{path}: retry_delay never jittered"


@check(
    "all three engines implement '--rate-limit-cooldown' -- an OPT-IN "
    "(default 0, off) longer wait-and-retry-ONCE on the same session after "
    "hitting SHEIN's own /risk/action/limit gate, honoring the ~5 minute "
    "cooldown observed live on Roman's own machine 2026-09-29, instead of "
    "giving up on the first rate-limit hit the way this repo always has. "
    "Off by default so a normal invocation never silently grows by minutes -- "
    "structural + behavioral: the retry-once guard (_cooldown_used) actually "
    "prevents a second wait in the same run, checked against the real "
    "3-argument threading (rate_limit_cooldown -> delay_jitter -> _wait_s)."
)
def _():
    for path in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "--rate-limit-cooldown" in src, f"{path} is missing the --rate-limit-cooldown flag"
        assert "args._cooldown_used = False" in src, f"{path}: cooldown-used flag never initialized"
        # exactly two call sites (scraper-api loop + local/CDP browser loop),
        # matching the two pre-existing '_rate_limited... break' sites this
        # change modified -- a third or a missing one is a parity regression.
        assert src.count("args._cooldown_used = True") == 2, (
            f"{path}: expected exactly 2 rate-limit-cooldown retry sites, "
            f"got {src.count('args._cooldown_used = True')}"
        )
        assert src.count("not args._cooldown_used") == 2, f"{path}: retry-once guard missing at a call site"
        # the guard must come BEFORE the flag is set, in program order, at
        # each site, or a run could cooldown-retry forever
        for m in __import__("re").finditer(r"if args\.rate_limit_cooldown > 0 and not args\._cooldown_used:\n\s*args\._cooldown_used = True", src):
            pass  # presence alone (matched via the combined pattern) proves ordering
        assert __import__("re").search(
            r"if args\.rate_limit_cooldown > 0 and not args\._cooldown_used:\s*\n\s*args\._cooldown_used = True",
            src,
        ), f"{path}: retry-once guard is not checked before being set (would allow more than one cooldown wait)"


@check(
    "the rate-limit-cooldown retry sleeps with the SAME sync/async style as "
    "its surrounding function -- a regression this change could easily "
    "introduce by copy-pasting one style into both call sites (playwright's "
    "scraper-api path is sync, its local/CDP browser path is async; "
    "puppeteer is async in both; selenium is sync in both -- see each "
    "engine's own pre-existing --retry-delay sleep immediately below each "
    "site, which this new code must match)"
)
def _():
    import re as _re

    # playwright: first site (scraper-api, sync) uses time.sleep; second
    # site (local/CDP browser loop, inside `async def run`) must use
    # `await asyncio.sleep`, matching its own sibling retry-delay sleep a
    # few lines below (`await asyncio.sleep(_jittered_delay(args.retry_delay`).
    pw_src = (ROOT / "playwright_scraper.py").read_text(encoding="utf-8")
    cooldown_sleeps = _re.findall(r"(await asyncio\.sleep\(_wait_s\)|time\.sleep\(_wait_s\))", pw_src)
    assert cooldown_sleeps == ["time.sleep(_wait_s)", "await asyncio.sleep(_wait_s)"], (
        f"playwright_scraper.py: expected [sync, async] cooldown sleeps (scraper-api site sync, "
        f"local/CDP browser-loop site async), got {cooldown_sleeps}"
    )

    # puppeteer: both sites live inside `async def run` even for the
    # scraper-api path (unlike playwright) -- both must be async.
    pup_src = (ROOT / "puppeteer_scraper.py").read_text(encoding="utf-8")
    assert pup_src.count("await asyncio.sleep(_wait_s)") == 2, "puppeteer_scraper.py: expected both cooldown sleeps to be async"
    assert "time.sleep(_wait_s)" not in pup_src

    # selenium: fully sync, no asyncio at all -- both must be time.sleep.
    sel_src = (ROOT / "selenium_scraper.py").read_text(encoding="utf-8")
    assert sel_src.count("time.sleep(_wait_s)") == 2, "selenium_scraper.py: expected both cooldown sleeps to be sync"
    assert "asyncio.sleep(_wait_s)" not in sel_src


@check(
    "--scraper-api-cdp without --scraper-api-profile-id logs a warning "
    "nudging toward a reused, warmed profile instead of a fresh one from "
    "2Captcha's default pool every run (added 2026-09-29 -- "
    "scraping_browser_connection_url's own docstring already recommended "
    "reuse; this surfaces it at the moment it matters, not just in docs). "
    "Checked in all three engines, and that the warning is gated correctly "
    "(fires only with --scraper-api-cdp, not on a plain --scraper-api run)."
)
def _():
    for path in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "args.scraper_api_cdp and not args.scraper_api_profile_id" in src, (
            f"{path}: missing the profile-reuse warning gate"
        )
        assert "each run gets a fresh" in src, f"{path}: profile-reuse warning text missing/changed"


# --------------------------------------------------------------------------- #
# SHEIN /risk/challenge automated pass (shein_challenge.py, 2026-09-30)
# --------------------------------------------------------------------------- #
_CHALLENGE_URL = "https://us.shein.com/risk/challenge?captcha_type=909&redirection=https%3A%2F%2Fus.shein.com%2Fpdsearch%2Fdress%2F"


def _nine_tiles():
    # The live 3x3 layout: 120px tiles on a 132px pitch.
    return [[528 + c * 132, 185 + r * 132, 120, 120] for r in range(3) for c in range(3)]


class _FakeChallenge:
    """Scripted shein_challenge.ChallengeDriver: one_pass -> nine_captcha,
    with `grid_results` deciding each round's outcome ("fail"/"success")."""

    def __init__(self, *, grid_results, escalate=True, state_raises_after_success=False):
        self.url_now = _CHALLENGE_URL
        self.stage = "one_pass"
        self.grid_results = list(grid_results)
        self.escalate = escalate
        self.state_raises_after_success = state_raises_after_success
        self.clicks = []
        self.shots = []
        self.round_clicks = 0
        self.image_set = 0
        self.result = None
        self.redirect_in = None

    async def url(self):
        if self.redirect_in is not None:
            self.redirect_in -= 1
            if self.redirect_in <= 0:
                self.url_now = "https://us.shein.com/pdsearch/dress/"
        return self.url_now

    async def state(self):
        if self.result == "success" and self.state_raises_after_success:
            raise RuntimeError("Execution context was destroyed, most likely because of a navigation")
        if self.stage == "one_pass":
            return {"stage": "one_pass", "checkbox": [793, 495, 73, 19], "scroll": [0, 0]}
        if self.stage == "nine_captcha":
            return {"stage": "nine_captcha", "tiles": list(reversed(_nine_tiles())), "icon": [853, 113, 54, 54],
                    "refresh": [528, 609, 387, 46], "srcs": [f"set{self.image_set}-{i}" for i in range(9)],
                    "loading": False, "result": self.result, "scroll": [0, 0]}
        return {"stage": "none"}

    async def screenshot(self, clip):
        self.shots.append(clip)
        return b"\x89PNG-fake"

    async def click(self, x, y):
        self.clicks.append((x, y))
        if self.stage == "one_pass":
            if self.escalate:
                self.stage = "nine_captcha"
            else:
                self.redirect_in = 1
            return
        if self.stage == "nine_captcha":
            if 528 <= x <= 528 + 387 and 609 <= y <= 609 + 46:  # the refresh button: new images, no pick
                self.round_clicks = 0
                self.image_set += 1
                return
            self.round_clicks += 1
            if self.round_clicks == 3:  # live: the widget auto-submits on the 3rd pick
                self.round_clicks = 0
                outcome = self.grid_results.pop(0)
                if outcome == "success":
                    self.result = "success"
                    self.redirect_in = 2
                else:
                    self.result = None
                    self.image_set += 1  # a failed round swaps in fresh images

    async def sleep(self, seconds):
        return None


def _grid_solver(answers):
    calls = []

    async def solve(task):
        calls.append(task)
        return json.dumps({"click": answers[len(calls) - 1]})
    return solve, calls


@check("shein_challenge pure helpers: row-major tile order, grid clip, GridTask payload, defensive click parsing")
def _():
    tiles = shein_challenge.order_tiles(list(reversed(_nine_tiles())))
    assert tiles == _nine_tiles(), "tiles must be ordered top-to-bottom, left-to-right (GridTask numbering)"
    jittered = [[t[0], t[1] + (0.4 if i % 2 else -0.4), t[2], t[3]] for i, t in enumerate(_nine_tiles())]
    assert [t[0] for t in shein_challenge.order_tiles(jittered)[:3]] == [528, 660, 792], "sub-pixel y noise reordered a row"
    clip = shein_challenge.grid_clip(tiles, scroll=(0, 100))
    assert clip == {"x": 528, "y": 285, "width": 384, "height": 384}, clip
    task = shein_challenge.build_grid_task(b"grid", b"icon")
    assert task["type"] == "GridTask" and task["rows"] == 3 and task["columns"] == 3
    assert task["body"] == "Z3JpZA==" and task["imgInstructions"] == "aWNvbg=="
    assert "imgInstructions" not in shein_challenge.build_grid_task(b"grid", None)
    assert shein_challenge.parse_grid_clicks('{"click": [1, "7", 8, 8, 0, 10, "x"]}') == [1, 7, 8]
    assert shein_challenge.parse_grid_clicks("not json") == []
    assert shein_challenge.parse_grid_clicks('{"token": "abc"}') == []


@check("shein_challenge: checkbox -> image grid, a SHEIN-rejected round is retried with FRESH images (live 2026-09-30: a correct answer still got code 9001), then success + redirect -> passed")
def _():
    fake = _FakeChallenge(grid_results=["fail", "success"])
    solve, calls = _grid_solver([[1, 7, 8], [1, 6, 9]])
    out = asyncio.run(shein_challenge.pass_risk_challenge(fake, None, max_rounds=3, solve=solve, step_timeout=2, redirect_timeout=5))
    assert out.passed, out
    assert out.solves == 2 and out.rounds == 2, out
    assert len(calls) == 2 and all("imgInstructions" in c for c in calls), "the icon must be sent with every GridTask"
    x, y = fake.clicks[0]
    assert (x, y) == (773, 504.5), f"checkbox click should land just left of its label, got {(x, y)}"
    tiles = _nine_tiles()
    picked = fake.clicks[1:4]
    for (px, py), n in zip(picked, [1, 7, 8]):
        t = tiles[n - 1]
        assert t[0] <= px <= t[0] + t[2] and t[1] <= py <= t[1] + t[3], f"click {(px, py)} missed tile {n}"


@check("shein_challenge: passes at the checkbox alone when SHEIN does not escalate, with no key and no solve spent")
def _():
    fake = _FakeChallenge(grid_results=[], escalate=False)
    out = asyncio.run(shein_challenge.pass_risk_challenge(fake, None, max_rounds=3, step_timeout=2))
    assert out.passed and out.solves == 0, out
    assert len(fake.clicks) == 1


@check("shein_challenge: no TWOCAPTCHA_KEY still clicks the free checkbox step, then stops at the grid with a clear reason (no crash, no solve)")
def _():
    fake = _FakeChallenge(grid_results=["success"])
    out = asyncio.run(shein_challenge.pass_risk_challenge(fake, scraper_api_client.TwoCaptchaClient(None), max_rounds=3, step_timeout=2))
    assert not out.passed and out.solves == 0, out
    assert "TWOCAPTCHA_KEY" in out.detail, out.detail
    assert len(fake.clicks) == 1


@check("shein_challenge: an answer that does not pick exactly 3 tiles (live: 1, 4 and 5 picks all came back) is never clicked — the grid is refreshed and the next round tried")
def _():
    fake = _FakeChallenge(grid_results=["success"])
    solve, calls = _grid_solver([[1, 2, 7, 8], [6], [2, 5, 8]])
    out = asyncio.run(shein_challenge.pass_risk_challenge(fake, None, max_rounds=5, solve=solve, step_timeout=2, redirect_timeout=5))
    assert out.passed and len(calls) == 3, out
    refresh_center = (528 + 387 / 2, 609 + 46 / 2)
    grid_clicks = [c for c in fake.clicks[1:] if c != refresh_center]
    assert fake.clicks[1:].count(refresh_center) == 2, f"expected two refresh clicks, got {fake.clicks[1:]}"
    assert len(grid_clicks) == 3, f"only the 3-pick answer should reach the tiles, got {grid_clicks}"
    assert "exactly 3" in calls[0]["comment"]


@check("shein_challenge: a state read racing SHEIN's own success redirect ('execution context was destroyed', seen live) is treated as navigation, not a failure")
def _():
    fake = _FakeChallenge(grid_results=["success"], state_raises_after_success=True)
    solve, _calls = _grid_solver([[2, 5, 8]])
    out = asyncio.run(shein_challenge.pass_risk_challenge(fake, None, max_rounds=3, solve=solve, step_timeout=2, redirect_timeout=5))
    assert out.passed, out


@check("shein_challenge: gives up after --risk-challenge-rounds, 0 disables it, and a driver error degrades to passed=False instead of raising")
def _():
    fake = _FakeChallenge(grid_results=["fail", "fail", "fail"])
    solve, calls = _grid_solver([[1, 2, 3]] * 3)
    out = asyncio.run(shein_challenge.pass_risk_challenge(fake, None, max_rounds=2, solve=solve, step_timeout=2))
    assert not out.passed and len(calls) == 2 and out.rounds == 2, out

    out = asyncio.run(shein_challenge.pass_risk_challenge(_FakeChallenge(grid_results=[]), None, max_rounds=0))
    assert not out.passed and "disabled" in out.detail

    class Broken(_FakeChallenge):
        async def screenshot(self, clip):
            raise RuntimeError("Target closed")
    solve, _calls = _grid_solver([[1, 2, 3]])
    out = asyncio.run(shein_challenge.pass_risk_challenge(Broken(grid_results=["success"]), None, max_rounds=2, solve=solve, step_timeout=2))
    assert not out.passed and "Target closed" in out.detail, out


@check("all three engines expose --risk-challenge-rounds (default 5) and call the challenge pass on BOTH the search and product-page paths, before the gateway URL is judged blocked")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        args = mod.build_arg_parser().parse_args(["--query", "dress"])
        assert args.risk_challenge_rounds == 5, f"{mod.__name__}: default should be 5"
        src = (ROOT / f"{mod.__name__}.py").read_text(encoding="utf-8")
        assert src.count("_maybe_pass_risk_challenge(driver, args, client)" if mod is selenium_scraper
                         else "await _maybe_pass_risk_challenge(page, args, client)") == 2, (
            f"{mod.__name__}: expected the challenge pass on both scrape paths"
        )
        for block in src.split("_maybe_pass_risk_challenge(")[2:]:
            assert "RISK_GATEWAY_URL_MARKERS" in block[:400], (
                f"{mod.__name__}: the gateway-URL blocked check must come right AFTER the challenge pass"
            )


@check("the engines' challenge gate skips cleanly when off the gateway, with --solve-captcha off, or --risk-challenge-rounds 0")
def _():
    class Page:
        url = "https://us.shein.com/pdsearch/dress/"

    class Driver:
        current_url = "https://us.shein.com/pdsearch/dress/"

    for argv in (["--query", "x"], ["--query", "x", "--solve-captcha", "off"], ["--query", "x", "--risk-challenge-rounds", "0"]):
        for mod in (playwright_scraper, puppeteer_scraper):
            args = mod.build_arg_parser().parse_args(argv)
            page = Page()
            if argv == ["--query", "x"]:
                assert asyncio.run(mod._maybe_pass_risk_challenge(page, args, None)) is False
            page.url = _CHALLENGE_URL
            if argv != ["--query", "x"]:
                assert asyncio.run(mod._maybe_pass_risk_challenge(page, args, None)) is False, (mod.__name__, argv)
        args = selenium_scraper.build_arg_parser().parse_args(argv)
        d = Driver()
        if argv != ["--query", "x"]:
            d.current_url = _CHALLENGE_URL
        assert selenium_scraper._maybe_pass_risk_challenge(d, args, None) is False, argv


def _png_header(w, h):
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"


@check("shein_challenge icon_click helpers: PNG size read, CoordinatesTask payload, and solver points mapped from device px (2x Retina, seen live) back to viewport CSS px, dropping out-of-image points")
def _():
    assert shein_challenge.png_size(_png_header(572, 572)) == (572, 572)
    assert shein_challenge.png_size(b"nope") == (0, 0)
    task = shein_challenge.build_coordinates_task(b"img", b"icons")
    assert task["type"] == "CoordinatesTask" and task["imgInstructions"] == "aWNvbnM=" and "order" in task["comment"]
    sol = json.dumps({"coordinates": [{"x": 572, "y": 286}, {"x": "100", "y": 50}, {"x": 900, "y": 10}, {"y": 1}]})
    pts = shein_challenge.parse_coordinates(sol, _png_header(572, 572), [457, 155, 286, 286])
    assert pts == [(457 + 286, 155 + 143), (457 + 50, 155 + 25)], pts
    assert shein_challenge.parse_coordinates("garbage", b"", [0, 0, 10, 10]) == []


class _FakeIconClick(_FakeChallenge):
    """one_pass -> icon_click; Confirm submits; `grid_results` decides each."""

    def __init__(self, *, grid_results, loaded_after=0):
        super().__init__(grid_results=grid_results)
        self.picks = []
        self.loaded_after = loaded_after
        self.reads = 0

    async def state(self):
        if self.stage == "icon_click":
            self.reads += 1
            return {"stage": "icon_click", "image": [457, 155, 286, 286], "icons": [457, 119, 286, 30],
                    "confirm": [457, 449, 286, 36], "refresh": [457, 494, 286, 36],
                    "srcs": [f"sprite{self.image_set}"], "loaded": self.reads > self.loaded_after,
                    "tips": "", "scroll": [0, 0]}
        return await super().state()

    async def screenshot(self, clip):
        self.shots.append(clip)
        return _png_header(int(clip["width"] * 2), int(clip["height"] * 2))

    async def click(self, x, y):
        self.clicks.append((x, y))
        if self.stage == "one_pass":
            self.stage = "icon_click"
            return
        if 449 <= y <= 485:  # Confirm
            if self.grid_results.pop(0) == "success":
                self.redirect_in = 1
            else:
                self.image_set += 1
                self.reads = 0
            self.picks = []
        elif 494 <= y <= 530:  # Refresh
            self.image_set += 1
            self.reads = 0
        else:
            self.picks.append((x, y))


@check("shein_challenge: the 'click icons in sequence' widget (live 2026-09-30, local Chrome) — waits for the sprite to LOAD (blank screenshots went to the solver live), clicks the points in order, presses Confirm, retries a rejected round, passes on redirect")
def _():
    fake = _FakeIconClick(grid_results=["fail", "success"], loaded_after=2)
    answers = [{"coordinates": [{"x": 100, "y": 100}, {"x": 300, "y": 200}, {"x": 500, "y": 400}, {"x": 60, "y": 520}]}] * 2
    calls = []

    async def solve(task):
        calls.append(task)
        return json.dumps(answers[len(calls) - 1])
    out = asyncio.run(shein_challenge.pass_risk_challenge(fake, None, max_rounds=3, solve=solve, step_timeout=3, redirect_timeout=5))
    assert out.passed and out.solves == 2, out
    assert all(c["type"] == "CoordinatesTask" for c in calls)
    first_round = fake.clicks[1:6]
    assert first_round[:4] == [(457 + 50, 155 + 50), (457 + 150, 155 + 100), (457 + 250, 155 + 200), (457 + 30, 155 + 260)], first_round
    assert first_round[4] == (457 + 143, 449 + 18), "Confirm must be pressed after the points"


@check("shein_challenge: an icon_click answer with fewer than 2 points is never submitted — refresh instead of Confirm")
def _():
    fake = _FakeIconClick(grid_results=["success"])
    answers = [{"coordinates": [{"x": 10, "y": 10}]}, {"coordinates": [{"x": 10, "y": 10}, {"x": 200, "y": 200}, {"x": 400, "y": 400}]}]
    calls = []

    async def solve(task):
        calls.append(task)
        return json.dumps(answers[len(calls) - 1])
    out = asyncio.run(shein_challenge.pass_risk_challenge(fake, None, max_rounds=3, solve=solve, step_timeout=3, redirect_timeout=5))
    assert out.passed and len(calls) == 2, out
    assert fake.clicks[1] == (457 + 143, 494 + 18), f"first action should be Refresh, got {fake.clicks[1]}"


_NINE_CAPTCHA_REPLICA = """<!doctype html><html><body>
<img class="header-content-img" style="display:none" src="data:,">
<nine-captcha-custom id="nine-captcha-custom"></nine-captcha-custom>
<script>
const host = document.getElementById('nine-captcha-custom');
const root = host.attachShadow({mode: 'open'});
const pics = Array.from({length: 9}, (_, i) =>
  `<div class="nine-content-pic" style="width:120px;height:120px;margin:6px;float:left">
     <img class="nine-content-img" style="width:120px;height:120px;display:block" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=#${i}">
     <div class="nine-content-select" style="display:none"></div>
     <div class="nine-content-loading" style="display:none"></div></div>`).join('');
root.innerHTML = `<div class="sui-dialog risk-nine-dialog__content"><div style="width:480px">
  <div class="nine-header-content"><div class="nine-header-content-title">Please select all images according to the icon</div>
  <div class="nine-header-content-img"><img class="header-content-img" style="width:54px;height:54px" src="data:image/gif;base64,R0lGODlhAQABAAAAACw="></div></div>
  <div class="nine-content-area"><div class="nine-content" style="width:396px;overflow:hidden">${pics}
    <div class="nine-success" style="display:none">Verification Success</div>
    <div class="nine-fail" style="display:none">Authentication failed</div></div>
  <div class="nine-refresh"><span class="nine-refresh-word">refresh</span></div></div></div></div>`;
</script></body></html>"""


_SPRITE = "data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACwAAAAAAQABAAACAkQBADs="
_ICON_CLICK_REPLICA = f"""<!doctype html><html><body>
<div class="geetest_panel"><div class="geetest_panel_box" id="self-click-x">
 <div class="captcha_click_wrapper" style="position:relative;margin:21px 14px;overflow:hidden;font-size:0;width:286px">
  <div class="title_wrapper" style="font-size:14px;height:32px">Please click the following icons from left to right in sequence.</div>
  <div class="pic_elg_wrapper" style="height:30px;background-image:url('{_SPRITE}');background-size:286px 316px"></div>
  <div class="pic_wrapper" style="height:286px;background-image:url('{_SPRITE}');background-size:286px 316px"></div>
  <div class="captcha_click_tips_box"> </div></div>
 <div class="captcha_btn_click_wrapper"><div class="captcha_click_confirm" style="height:36px;width:286px"><span>Confirm</span></div></div>
 <div class="captcha_btn_click_wrapper"><div class="captcha_click_refresh" style="height:36px;width:286px">Refresh</div></div>
</div></div></body></html>"""


@check("STATE_JS in a REAL headless Chromium against a replica of the live nine_captcha shadow DOM (class names from the 2026-09-30 capture): finds 9 tiles, the VISIBLE icon despite an earlier hidden decoy (the live bug), and each result state; skips if Chromium is unavailable")
def _():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return

    async def go():
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except Exception:
                return None
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.set_content(_NINE_CAPTCHA_REPLICA)
            first = await page.evaluate(shein_challenge.STATE_JS)
            await page.evaluate("() => document.getElementById('nine-captcha-custom').shadowRoot.querySelector('.nine-fail').style.display = 'block'")
            failed = await page.evaluate(shein_challenge.STATE_JS)
            await page.set_content('<div><span></span><span>I am human</span></div>')
            one_pass = await page.evaluate(shein_challenge.STATE_JS)
            await page.set_content(_ICON_CLICK_REPLICA)
            await page.wait_for_timeout(300)
            icon_click = await page.evaluate(shein_challenge.STATE_JS)
            await browser.close()
            return first, failed, one_pass, icon_click

    res = asyncio.run(go())
    if res is None:
        return
    first, failed, one_pass, icon_click = res
    assert first["stage"] == "nine_captcha" and len(first["tiles"]) == 9, first
    assert first["icon"] and first["icon"][2] == 54, f"visible icon not found: {first['icon']}"
    assert first["refresh"] and first["result"] is None and first["loading"] is False
    assert len(set(first["srcs"])) == 9
    assert failed["result"] == "fail"
    assert one_pass["stage"] == "one_pass" and one_pass["checkbox"], one_pass
    assert icon_click["stage"] == "icon_click", icon_click
    assert [round(v) for v in icon_click["image"][2:]] == [286, 286] and round(icon_click["icons"][3]) == 30, icon_click
    assert icon_click["confirm"] and icon_click["refresh"] and icon_click["loaded"] is True, icon_click


@check("every top-level module is in the Dockerfile COPY and pyproject py-modules (audit 2026-09-30: the wheel and image lacked shein_challenge, so every install failed on import)")
def _():
    import re as _re
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    listed = set(_re.findall(r'"([a-z_]+)"', pyproject.split("py-modules", 1)[1].split("]", 1)[0]))
    for mod in sorted(pth.stem for pth in ROOT.glob("*.py")):
        assert f"{mod}.py" in docker, f"{mod}.py missing from the Dockerfile COPY"
        assert mod in listed, f"{mod} missing from pyproject py-modules"


@check("BEHAVIORAL: the one --rate-limit-cooldown retry runs even with --block-retries 0 (audit 2026-09-30: `continue` on the last range() iteration ended the loop, so the promised retry never happened), is still only ONE retry, and never spends a --block-retries attempt — all three engines, driven through run()")
def _():
    rate_limited = "<html><body>redirected to /risk/action/limit</body></html>"
    original = scraper_api_client.TwoCaptchaClient.scrape_url
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        for bodies, expected_calls, expected_rc in (
            ([rate_limited, _SEARCH_PAGE_HTML], 2, output_writer.EXIT_OK),
            ([rate_limited, rate_limited, _SEARCH_PAGE_HTML], 2, None),
        ):
            calls = []

            def fake(self, url, *, data_format="raw", timeout=60, wait_for=None, cdp_url=None, _b=bodies):
                calls.append(url)
                return scraper_api_client.ScrapeResult(target_status=200, headers={}, body=_b[min(len(calls), len(_b)) - 1])

            scraper_api_client.TwoCaptchaClient.scrape_url = fake
            try:
                with tempfile.TemporaryDirectory() as td:
                    args = mod.build_arg_parser().parse_args([
                        "--query", "dress", "--twocaptcha-key", "fake-key-for-test-only", "--scraper-api",
                        "--block-retries", "0", "--rate-limit-cooldown", "0.001", "--delay-jitter", "0",
                        "--out", str(Path(td) / "o.json"),
                    ])
                    rc = asyncio_run_maybe(mod, args)
            finally:
                scraper_api_client.TwoCaptchaClient.scrape_url = original
            assert len(calls) == expected_calls, f"{mod.__name__}: {len(calls)} scrape calls for {len(bodies)} bodies"
            if expected_rc is not None:
                assert rc == expected_rc, f"{mod.__name__}: rc {rc}"
            else:
                assert rc != output_writer.EXIT_OK, f"{mod.__name__}: a second rate limit must not be retried again"


def _real_gb():
    return json.loads((ROOT / "tests" / "fixtures" / "shein_search_live_dress_20260921.json").read_text(encoding="utf-8"))


@check("one malformed record in the REAL gbRawData keeps the other nine and is counted (audit 2026-09-30: a string salePrice on record 2 turned 10 products into 0, exit 4)")
def _():
    import copy as _copy
    data = _copy.deepcopy(_real_gb())
    data["results"]["bffProductsInfo"]["products"][1]["salePrice"] = "9.99"
    res = sp.parse_search_results("", max_results=10, raw_data=data)
    assert res.source_used == "gb_raw_data" and len(res.products) == 9, (res.source_used, len(res.products))
    assert res.rejected_rows == 1 and "record 1" in res.rejected_reasons[0]
    clean = sp.parse_search_results("", max_results=10, raw_data=_real_gb())
    assert clean.rejected_rows == 0 and len(clean.products) == 10


def _ld_page(node):
    return f'<script type="application/ld+json">{json.dumps(node)}</script>'


@check("a ProductGroup's price is the LOWEST variant price whatever the variant order, marked price_source=json_ld_min_variant (audit 2026-09-30: S=10/M=20 reordered flipped the price with no change on the site)")
def _():
    v = [{"@type": "Product", "offers": {"price": "10", "priceCurrency": "USD"}},
         {"@type": "Product", "offers": {"price": "20", "priceCurrency": "USD"}}]
    a = sp.parse_product_page(_ld_page({"@type": "ProductGroup", "name": "X", "hasVariant": v}), url="https://us.shein.com/x-p-1.html")
    b = sp.parse_product_page(_ld_page({"@type": "ProductGroup", "name": "X", "hasVariant": v[::-1]}), url="https://us.shein.com/x-p-1.html")
    assert a.price == b.price == 10.0 and a.currency == "USD", (a.price, b.price)
    assert a.price_source == b.price_source == "json_ld_min_variant"
    one = sp.parse_product_page(_ld_page({"@type": "Product", "name": "Y", "offers": {"price": "7"}}), url="https://us.shein.com/y-p-2.html")
    assert one.price == 7.0 and one.price_source == "json_ld"


@check("JSON-LD shapes that are legal and broke the parser (CLAUDE.md §4; audit 2026-09-30): offers list/null/AggregateOffer, brand as a string, Product inside @graph, ImageObject — none crash, each reads the right value")
def _():
    u = "https://us.shein.com/z-p-3.html"
    p = sp.parse_product_page(_ld_page({"@type": "Product", "name": "A", "offers": [None, {"price": "5", "priceCurrency": "USD"}]}), url=u)
    assert (p.price, p.currency) == (5.0, "USD")
    p = sp.parse_product_page(_ld_page({"@type": "Product", "name": "A", "brand": "SHEIN", "offers": None}), url=u)
    assert p.brand == "SHEIN" and p.price is None
    p = sp.parse_product_page(_ld_page({"@graph": [{"@type": "BreadcrumbList"}, {"@type": "Product", "name": "G", "offers": {"price": "7"}}]}), url=u)
    assert p is not None and p.title == "G" and p.price == 7.0
    p = sp.parse_product_page(_ld_page({"@type": ["Product"], "name": "A", "image": [{"@type": "ImageObject", "contentUrl": "https://i/x.jpg"}],
                                        "offers": {"@type": "AggregateOffer", "lowPrice": "3.5"}}), url=u)
    assert p.image_url == "https://i/x.jpg" and p.price == 3.5


def _diff_run(td, name, rows, url, **kw):
    out = str(Path(td) / name)
    kw.setdefault("allow_empty", False)
    output_writer.finish_run(products=rows, out_path=out, fmt="json", engine="t", url=url, pages_requested=1,
                             pages_completed=1, failed_pages=None, blocked=False, remote_api_error=False,
                             started_at=0.0, **kw)
    return out


@check("diff_runs refuses different selections, never calls a currency switch a price change (even at the same number), reads a capped top-N's missing SKU as left_selection, and rejects a sidecar that does not describe its file (audit 2026-09-30)")
def _():
    dress, jeans = "https://us.shein.com/pdsearch/dress/", "https://us.shein.com/pdsearch/jeans/"
    with tempfile.TemporaryDirectory() as td:
        a = _diff_run(td, "a.json", [_mk_product("s1", 9.93, currency="USD")], dress)
        b = _diff_run(td, "b.json", [_mk_product("s1", 19.93, currency="EUR")], jeans)
        try:
            diff_runs.diff(a, b)
            raise AssertionError("different selections must be refused")
        except SystemExit as exc:
            assert "different selections" in str(exc)
        r = diff_runs.diff(a, b, allow_different_scope=True)
        assert not r["changed"] and len(r["currency_changed"]) == 1

        c = _diff_run(td, "c.json", [_mk_product("s1", 10.0, currency="USD")], dress)
        e = _diff_run(td, "e.json", [_mk_product("s1", 10.0, currency="EUR")], dress + "?")
        r = diff_runs.diff(c, e)
        assert r["currency_changed"] and not r["changed"], "same number, other currency must still be reported"

        f = _diff_run(td, "f.json", [_mk_product("s1"), _mk_product("s2")], dress, max_results=2)
        g = _diff_run(td, "g.json", [_mk_product("s1"), _mk_product("s3")], dress, max_results=2)
        r = diff_runs.diff(f, g)
        assert r["capped"] and r["left_selection"] == ["s2"] and r["removed"] == [] and r["added"] == ["s3"]
        h = _diff_run(td, "h.json", [_mk_product("s1"), _mk_product("s2")], dress, max_results=50)
        i = _diff_run(td, "i.json", [_mk_product("s1")], dress, max_results=50)
        r = diff_runs.diff(h, i)
        assert r["removed"] == ["s2"] and not r["capped"], "an uncapped run's missing SKU really is removed"

        Path(g).write_text("[]", encoding="utf-8")
        try:
            diff_runs.diff(f, g)
            raise AssertionError("a sidecar whose hash does not match must be refused")
        except SystemExit as exc:
            assert "output_sha256" in str(exc)


@check("ONE paid-solve budget per run (--max-solves, CLAUDE.md §23 'a cap nothing enforces is a bill'): every task-creating call site is guarded — counted, not assumed — and the grid/icon rounds stop at the cap")
def _():
    src = (ROOT / "shein_challenge.py").read_text(encoding="utf-8")
    assert src.count("await solve(") == src.count("budget.try_spend()") == 2, "each paid solve in shein_challenge needs its own guard"
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        esrc = (ROOT / f"{mod.__name__}.py").read_text(encoding="utf-8")
        assert esrc.count("solve_when_blocked(") == 1 and esrc.count("budget.remaining() == 0") == 1, mod.__name__
        assert esrc.count("budget=_budget(args)") == esrc.count("max_rounds=args.risk_challenge_rounds") == 1, mod.__name__
        calls = esrc.count("_maybe_solve_captcha(") - 1
        assert esrc.count("args=args)") + esrc.count("args=args,\n") >= calls, f"{mod.__name__}: a solve call site without the run's budget"
        assert mod.build_arg_parser().parse_args(["--query", "x"]).max_solves == 8
        assert '"solves_spent": _budget(args).spent' in esrc

    fake = _FakeChallenge(grid_results=["fail", "fail", "fail"])
    solve, calls = _grid_solver([[1, 2, 3]] * 3)
    budget = shein_challenge.SolveBudget(1)
    out = asyncio.run(shein_challenge.pass_risk_challenge(fake, None, max_rounds=5, solve=solve, step_timeout=2, budget=budget))
    assert len(calls) == 1 and budget.spent == 1 and not out.passed, (len(calls), budget.spent)
    assert "budget exhausted" in out.detail
    zero = shein_challenge.SolveBudget(0)
    out = asyncio.run(shein_challenge.pass_risk_challenge(_FakeChallenge(grid_results=["success"]), None, max_rounds=5,
                                                          solve=_grid_solver([[1, 2, 3]])[0], step_timeout=2, budget=zero))
    assert zero.spent == 0 and "budget exhausted" in out.detail, "--max-solves 0 must never pay"


@check("_maybe_solve_captcha does not even ask the solver once the run's budget is spent, and counts a billed task (solved or solver error) against it — all three engines")
def _():
    import captcha_solver as _cs
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        calls = []
        original = mod.solve_when_blocked
        mod.solve_when_blocked = lambda **kw: calls.append(1) or {"action": "warning_solver_error", "detail": "x"}
        try:
            args = mod.build_arg_parser().parse_args(["--query", "x", "--max-solves", "1"])
            client = scraper_api_client.TwoCaptchaClient("fake-key-for-test-only")
            for _i in range(3):
                r = mod._maybe_solve_captcha(html="<html></html>", url="u", client=client, policy="when-blocked", args=args)
                if _inspect.iscoroutine(r):
                    asyncio.run(r)
        finally:
            mod.solve_when_blocked = original
        assert len(calls) == 1 and mod._budget(args).spent == 1, (mod.__name__, len(calls))
    assert _cs  # imported for parity with the engines' own import


@check("a 2Captcha account error (revoked key / zero balance, seen live 2026-09-30) stops the challenge after ONE round instead of spending every round on it")
def _():
    calls = []

    async def solve(task):
        calls.append(task)
        raise scraper_api_client.TwoCaptchaError("createTask failed: ERROR_KEY_DOES_NOT_EXIST The API key is missing")
    out = asyncio.run(shein_challenge.pass_risk_challenge(_FakeChallenge(grid_results=[]), None, max_rounds=5, solve=solve, step_timeout=2))
    assert len(calls) == 1 and not out.passed and "refused the account" in out.detail, (len(calls), out.detail)


def run() -> int:
    """All @check-decorated functions above already ran at import time
    (that's the point — see the `check()` docstring) and self-registered
    into RESULTS. This just reports them."""
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"smoke_test: {passed}/{len(RESULTS)} checks passed")
    for name, detail in failed:
        print(f"  FAIL: {name}\n        {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    import sys
    sys.exit(run())
