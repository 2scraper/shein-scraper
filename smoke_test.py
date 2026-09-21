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
synthetic). A green run here proves the architecture (exit codes,
dedupe, precedence, credential redaction, CLI validation, engines
importing cleanly) AND that the confirmed data shapes parse correctly —
it does NOT prove this repo's own engines get the same treatment SHEIN
gave the browser-rendering tool that captured this data (see TESTING.md).

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


@check("finish_run precedence: remote_api_error status is never laundered into 'complete' just because products were present")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[_mk_product("1")], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=True, remote_api_error=True, allow_empty=True, started_at=0.0,
        )
        assert code == output_writer.EXIT_REMOTE_API_ERROR
        assert Path(out).exists(), "already-collected products must still be written out"
        meta = json.loads(Path(f"{out}.meta.json").read_text())
        assert meta["status"] == "remote_api_error", meta["status"]


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
    for marker in sp.BOT_CHALLENGE_MARKERS:
        assert marker.lower() in real_block_fixture.lower(), f"{marker!r} does not match the actual captured incident"
    assert captcha_solver.detect_from_html(real_block_fixture, sp.BOT_CHALLENGE_MARKERS), (
        "detect_from_html must flag the real captured risk/challenge page as a block, with extra_markers passed"
    )
    assert not captcha_solver.detect_from_html(real_block_fixture), (
        "confirms the GENERIC markers alone do NOT catch this incident — extra_markers is load-bearing here, "
        "not just corroboration, unlike every prior family incident"
    )


@check("captcha_solver.identify_widget extracts a Turnstile sitekey")
def _():
    html = '<div class="cf-turnstile" data-sitekey="0x4AAA_example"></div>'
    signal = captcha_solver.identify_widget(html)
    assert signal is not None
    assert signal.captcha_type == captcha_solver.CaptchaType.CLOUDFLARE_TURNSTILE
    assert signal.sitekey == "0x4AAA_example"


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
