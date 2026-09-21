#!/usr/bin/env python3
"""captcha_solver.py — detection + solving policy. Comments may name a site
for context; no site-specific selector or URL lives here. Per-site markers
are passed in by the caller (see BOT_CHALLENGE_MARKERS in product_parser.py)
rather than hardcoded, so this module stays reusable across the family.

Policy, matching the family's hard-won rule that "detected != blocking":

  - Detection stays broad — run it on every page, unconditionally.
  - A captcha WIDGET present on a page whose products are already rendered
    guards nothing and is not solved: `--solve-captcha when-blocked` (the
    default) only pays for a solve when `count_product_links(html) == 0`.
  - A missing key or a solver-side error is a WARNING, never a crash — the
    run continues, and only reports EXIT_BLOCKED if the page really was
    gated (see output_writer.EXIT_BLOCKED).

No JavaScript crosses this module's boundary: it hands back a plain
(captcha_type, sitekey, token) result. The engine — which already speaks
its own driver's dialect (Playwright/Selenium/pyppeteer) — is the one that
calls page.evaluate / execute_script to inject the token, because that is
exactly the kind of per-engine primitive `page_flow.py`-style code should
own instead of a shared module quietly picking one driver's dialect.
**No engine in this family actually does that injection yet** for a
locally-launched (non `--cdp-endpoint`) browser — every engine's own
`_maybe_solve_captcha` only logs "solved", it never writes the token back
into the page. Over `--cdp-endpoint`, this doesn't matter: 2Captcha's own
`Captcha.setAutoSolve` CDP domain (see each engine's own
`_enable_scraping_browser_auto_solve`) solves AND injects entirely inside
their infrastructure, for whichever widget types their Scraping Browser
extension recognizes (confirmed live, 2026-09-14: Turnstile, Amazon WAF,
Yandex SmartCaptcha, Lemin — GeeTest was NOT in that confirmed list,
though it may be covered and simply wasn't seen in that one capture). This
module's own solve path (this file + scraper_api_client.py) is what a
LOCAL browser run would need for a widget the CDP extension doesn't
already cover — GeeTest support was added here 2026-09-21 for exactly that
reason (see CaptchaType.GEETEST_V3/GEETEST_V4 below), but token injection
for ANY type on a local browser remains a real, open gap, not something
this GeeTest addition closes on its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Sequence

from scraper_api_client import TwoCaptchaAuthError, TwoCaptchaClient, TwoCaptchaError

# Generic signals any family site might show. A per-site BOT_CHALLENGE_MARKERS
# list (product_parser.py) is unioned with this, never a replacement for it.
GENERIC_BOT_CHALLENGE_MARKERS: Sequence[str] = (
    "cf-turnstile", "challenges.cloudflare.com", "cdn-cgi/challenge-platform",
    "g-recaptcha", "recaptcha/api.js", "grecaptcha",
    "h-captcha", "hcaptcha.com/1/api.js",
    "px-captcha", "perimeterx",
    "datadome",
    "attention required", "just a moment", "verify you are human",
)


class CaptchaType(str, Enum):
    CLOUDFLARE_TURNSTILE = "cloudflare_turnstile"
    RECAPTCHA_V2 = "recaptcha_v2"
    RECAPTCHA_V3 = "recaptcha_v3"
    HCAPTCHA = "hcaptcha"
    GEETEST_V3 = "geetest_v3"
    GEETEST_V4 = "geetest_v4"


_SITEKEY_PATTERNS = {
    CaptchaType.CLOUDFLARE_TURNSTILE: re.compile(
        r'class="[^"]*cf-turnstile[^"]*"[^>]*data-sitekey="([^"]+)"'
    ),
    CaptchaType.RECAPTCHA_V2: re.compile(
        r'class="[^"]*g-recaptcha[^"]*"[^>]*data-sitekey="([^"]+)"'
    ),
    CaptchaType.HCAPTCHA: re.compile(
        r'class="[^"]*h-captcha[^"]*"[^>]*data-sitekey="([^"]+)"'
    ),
}
# v3 ships no visible widget — the sitekey rides on the loader's `render=`
# query param instead of a data-sitekey attribute.
_RECAPTCHA_V3_LOADER_RE = re.compile(r"recaptcha/api\.js\?render=([\w-]+)")
_RECAPTCHA_EXPLICIT_LOADER_RE = re.compile(r"recaptcha/api\.js\?render=explicit")

# GeeTest — added 2026-09-21 after shein.com's own risk gateway
# (captcha_type=909, see shein_parser.BOT_CHALLENGE_MARKERS) correlated
# with GeeTest via three circumstantial signals documented in
# shein_parser.py's module docstring (the site's global stylesheet defines
# .geetest_wind/.geetest_panel on EVERY page, robots.txt disallows
# /geetest/, and the numeric captcha_type code is consistent with GeeTest's
# own convention) — NOT a confirmed vendor string the way every other
# CaptchaType below was found (an actual `cf-turnstile`/`g-recaptcha`/
# `h-captcha` class or loader URL, seen live). No family site, shein.com
# included, has ever had its actual GeeTest widget markup captured — only
# a waiting/redirect shell page, never what renders when a real challenge
# is actually shown. These two patterns are therefore UNCONFIRMED
# best-effort, built from GeeTest's own public client-integration
# documentation (a `data-captcha-id`/`captchaId` attribute for v4; `gt`
# and `challenge` fields together for v3 — both are 32-character
# hex-like ids in GeeTest's own docs), not a shein.com-specific fact.
# `# TODO: verify live` applies here exactly like shein_parser.py's own
# DOM fallback selectors — update these the moment a real capture exists.
_GEETEST_V4_ID_PATTERNS = (
    re.compile(r'data-captcha-id=["\']([a-f0-9]{32})["\']'),
    re.compile(r'captchaId["\']?\s*:\s*["\']([a-f0-9]{32})["\']'),
)
_GEETEST_V3_RE = re.compile(
    r'gt["\']?\s*:\s*["\']([a-f0-9]{32})["\'][^{}]*?challenge["\']?\s*:\s*["\']([a-f0-9]{32})["\']',
    re.S,
)


@dataclass
class CaptchaSignal:
    captcha_type: CaptchaType
    sitekey: Optional[str]
    invisible: bool = False
    # GeeTest-only fields (see the module-docstring note above on why
    # GeeTest doesn't fit the plain sitekey shape every other type uses).
    gt: Optional[str] = None
    challenge: Optional[str] = None
    captcha_id: Optional[str] = None
    api_server: Optional[str] = None


# The Scraping Browser API's managed Chromium ships 2Captcha's OWN
# captcha-solving extension pre-installed (confirmed live, 2026-09-14:
# extension id kjmkgkdkpedkejedfhmfcenooemhbpbo, content scripts named
# .../captcha/{turnstile,amazon_waf,yandex,lemin}/{interceptor,hunter}.js).
# `page.content()` over --cdp-endpoint captures THOSE injected <script>
# tags too, and their src/data attributes contain marker substrings like
# "cf-turnstile" regardless of whether the actual page has a real widget —
# on a real StockX 403 that turned out to be a plain, static Cloudflare
# WAF page with no captcha at all ("Sorry, you have been blocked" — no
# `data-sitekey` anywhere), this alone made GENERIC_BOT_CHALLENGE_MARKERS
# fire and identify_widget() correctly find nothing to solve, which then
# logged as a confusing "captcha-like marker detected but no known
# widget/sitekey" rather than a clean "no captcha present". Stripped here
# so the extension's own always-there code never counts as the page's.
_EXTENSION_SCRIPT_RE = re.compile(
    r'<script\b[^>]*\bchrome-extension://[^>]*>.*?</script>', re.I | re.S
)


def _strip_extension_noise(html: str) -> str:
    return _EXTENSION_SCRIPT_RE.sub("", html)


def detect_from_html(html: str, extra_markers: Sequence[str] = ()) -> bool:
    """Broad detection: True if ANY known marker string appears. Cheap and
    deliberately over-inclusive — see module docstring on why detection and
    blocking are decided separately."""
    haystack = _strip_extension_noise(html).lower()
    for marker in (*GENERIC_BOT_CHALLENGE_MARKERS, *extra_markers):
        if marker.lower() in haystack:
            return True
    return False


def identify_widget(html: str) -> Optional[CaptchaSignal]:
    """Best-effort: which widget, and its sitekey. Reconciles the v3-vs-
    v2-invisible ambiguity by trusting the LOADER'S query param over any
    wrapper metadata — a site can label its own wrapper "v3" while shipping
    a `render=explicit` (v2-invisible) loader; sending v3 parameters for a
    v2-invisible widget buys a token the site rejects. `render=<sitekey>`
    means v3; `render=explicit` means v2."""
    html = _strip_extension_noise(html)
    m = _RECAPTCHA_V3_LOADER_RE.search(html)
    if m and not _RECAPTCHA_EXPLICIT_LOADER_RE.search(html):
        return CaptchaSignal(CaptchaType.RECAPTCHA_V3, sitekey=m.group(1))
    for ctype, pattern in _SITEKEY_PATTERNS.items():
        m = pattern.search(html)
        if m:
            return CaptchaSignal(ctype, sitekey=m.group(1))
    # GeeTest — v4 first (current-generation; see the UNCONFIRMED note on
    # the patterns themselves, above).
    for pattern in _GEETEST_V4_ID_PATTERNS:
        m = pattern.search(html)
        if m:
            return CaptchaSignal(CaptchaType.GEETEST_V4, sitekey=None, captcha_id=m.group(1))
    m = _GEETEST_V3_RE.search(html)
    if m:
        return CaptchaSignal(CaptchaType.GEETEST_V3, sitekey=None, gt=m.group(1), challenge=m.group(2))
    return None


def _task_payload(signal: CaptchaSignal, page_url: str, *, proxyless: bool, min_score: float = 0.3) -> dict:
    # GeeTest's task shape has no `websiteKey` at all (v3 sends `gt`/
    # `challenge`, v4 sends `captchaId`) — confirmed against 2Captcha's own
    # published API reference for GeeTestTask(Proxyless)/GeeTestV4Task
    # (Proxyless), unlike identify_widget()'s extraction patterns above,
    # which are NOT confirmed against a real shein.com capture.
    if signal.captcha_type == CaptchaType.GEETEST_V4:
        return {
            "type": "GeeTestV4TaskProxyless" if proxyless else "GeeTestV4Task",
            "websiteURL": page_url,
            "captchaId": signal.captcha_id,
        }
    if signal.captcha_type == CaptchaType.GEETEST_V3:
        task = {
            "type": "GeeTestTaskProxyless" if proxyless else "GeeTestTask",
            "websiteURL": page_url,
            "gt": signal.gt,
            "challenge": signal.challenge,
        }
        if signal.api_server:
            task["geetestApiServerSubdomain"] = signal.api_server
        return task

    type_map = {
        CaptchaType.CLOUDFLARE_TURNSTILE: "TurnstileTaskProxyless" if proxyless else "TurnstileTask",
        CaptchaType.RECAPTCHA_V2: "RecaptchaV2TaskProxyless" if proxyless else "RecaptchaV2Task",
        CaptchaType.RECAPTCHA_V3: "RecaptchaV3TaskProxyless",
        CaptchaType.HCAPTCHA: "HCaptchaTaskProxyless" if proxyless else "HCaptchaTask",
    }
    task = {"type": type_map[signal.captcha_type], "websiteURL": page_url, "websiteKey": signal.sitekey}
    if signal.captcha_type == CaptchaType.RECAPTCHA_V3:
        # 2Captcha's own field name for RecaptchaV3TaskProxyless (confirmed
        # against their API reference) — the token 2Captcha hands back is
        # produced to clear THIS threshold; it is a solve-request input, not
        # something to validate against the response afterwards. `--min-
        # score` is the CLI knob; omitting it would leave 2Captcha's own
        # server-side default in effect, silently, which is the same
        # "documented flag nobody reads" shape this family has shipped
        # before (CLAUDE.md §17) — so this module always sends one.
        task["minScore"] = min_score
    return task


def solve_when_blocked(
    *,
    client: TwoCaptchaClient,
    page_url: str,
    html: str,
    count_product_links: Callable[[str], int],
    extra_markers: Sequence[str] = (),
    proxyless: bool = True,
    min_score: float = 0.3,
) -> dict:
    """The default `--solve-captcha when-blocked` policy. Cheap first: count
    product links on the page AS-IS — no readiness wait, no scroll — because
    a page whose products are already rendered is not blocked, and running
    a 20s readiness wait first would burn it even when solving is what
    actually makes products appear on a page that IS gated.
    """
    if not detect_from_html(html, extra_markers):
        return {"action": "no_captcha_detected"}

    if count_product_links(html) > 0:
        return {"action": "skipped_products_present"}

    signal = identify_widget(html)
    if signal is None:
        return {"action": "detected_unidentified_widget"}

    try:
        task = _task_payload(signal, page_url, proxyless=proxyless, min_score=min_score)
        token = client.solve_and_wait(task)
        return {"action": "solved", "captcha_type": signal.captcha_type.value, "token": token}
    except TwoCaptchaAuthError as exc:
        return {"action": "warning_no_key", "detail": str(exc)}
    except TwoCaptchaError as exc:
        return {"action": "warning_solver_error", "detail": str(exc)}
