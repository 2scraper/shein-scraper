#!/usr/bin/env python3
"""shein_challenge.py — automated pass of SHEIN's own `/risk/challenge`
gateway (captcha_type=909), captured live 2026-09-30 through a fresh US
Browser API profile.

What the gateway actually is (confirmed live, not guessed): no third-party
vendor at all. SHEIN's own `risk_challenge` bundle drives a two-step flow
over `/risk/verify/identity/validation/{token,resources,check}`:

  1. `validate_type=one_pass` — a modal "Please click to complete the
     following actions to verify you are human" with a single
     "I am human" checkbox. Clicking it posts `/validation/check`; the
     server either lets the session through or escalates to step 2.
  2. `validate_type=nine_captcha` — an open-shadow-DOM web component
     `<nine-captcha-custom>`: "Please select all images according to the
     icon", a 54x54 `.header-content-img` icon and nine 120x120
     `.nine-content-img` tiles (3x3). There is NO confirm button — the
     widget posts `/validation/check` by itself once enough tiles are
     picked, then shows `.nine-success` ("Verification Success", followed
     by a redirect back to the original URL) or `.nine-fail`
     ("Authentication failed", followed by a fresh set of images).

A third widget can appear in place of step 2 (seen live the same day on
a local Selenium Chrome): `icon_click`, "Please click the following icons
from left to right in sequence", a GeeTest-style light-DOM panel
(`.geetest_panel_box > .captcha_click_wrapper`) whose one 286x316 sprite is
the CSS background of both the 286x286 `.pic_wrapper` picture and the 30px
`.pic_elg_wrapper` icon strip, with explicit `.captcha_click_confirm` /
`.captcha_click_refresh` buttons. It maps onto 2Captcha's CoordinatesTask
(icon strip as `imgInstructions`); "Verification Failed" in
`.captcha_click_tips_box` is SHEIN's rejection.

Step 2 maps directly onto 2Captcha's GridTask (rows=3, columns=3, the icon
as `imgInstructions`), whose `solution.click` is the 1-based tile list.
Live result on 2026-09-30: round 1's answer was correct by inspection but
SHEIN still replied `code=9001 "System error"`; round 2 passed and
redirected to `/pdsearch/dress/` with `window.gbRawData` present. So a
single failed round is not proof of a wrong answer — this module retries
with a fresh image set instead of reporting the solve as incorrect. The
same pattern showed up on a second fresh profile the same day: the first
correct submission was rejected, so budget at least two correct rounds.

Driver-agnostic by design, same split as captcha_solver.py: this module
owns the JS source, the geometry and the retry policy; each engine passes
a small adapter (see `ChallengeDriver`) that actually evaluates JS, takes a
clipped screenshot and moves the mouse in its own dialect.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Protocol, Sequence

from scraper_api_client import TwoCaptchaAuthError, TwoCaptchaClient, TwoCaptchaError

log = logging.getLogger("shein_challenge")

CHALLENGE_URL_MARKER = "/risk/challenge"
# 2Captcha answers that no retry can change (live 2026-09-30: a revoked key
# failed all five rounds, one createTask each, before giving up).
TERMINAL_SOLVER_ERRORS = ("ERROR_KEY_DOES_NOT_EXIST", "ERROR_WRONG_USER_KEY", "ERROR_ZERO_BALANCE", "ERROR_IP_NOT_ALLOWED")
CHECKBOX_LABEL = "I am human"
GRID_ROWS = 3
GRID_COLUMNS = 3
# The widget submits on the third pick and a two-pick answer was seen to
# sit unsubmitted (live, 2026-09-30), so a correct answer is three tiles.
# An answer with any other count is never clicked: a wrong submission may
# raise the profile's risk score, a refresh does not.
EXPECTED_PICKS = 3
GRID_COMMENT = "Select exactly 3 images that show the same action or object as the small icon"
SEQUENCE_COMMENT = "Click the icons shown in the instruction, in the same order, left to right"
MIN_SEQUENCE_POINTS = 2
SOLVABLE_STAGES = ("nine_captcha", "icon_click")
_SUCCESS_TIPS_RE = re.compile(r"success", re.I)  # "Successful!" live; "Verification Failed" on a rejection
STAGES = ("one_pass",) + SOLVABLE_STAGES

# Walks the document plus every OPEN shadow root (both widgets live in
# one) and reports the current stage with viewport-relative rects. Scroll
# offsets are returned separately because screenshot clips are in page
# coordinates while mouse events are in viewport coordinates.
STATE_JS = r"""
() => {
  const roots = [];
  const collect = (root) => {
    roots.push(root);
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) collect(el.shadowRoot);
  };
  collect(document);
  const visible = (e) => {
    if (!e) return false;
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(e);
    return s.display !== 'none' && s.visibility !== 'hidden';
  };
  const rect = (e) => { const r = e.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; };
  const qa = (sel) => roots.flatMap((r) => [...r.querySelectorAll(sel)]);
  const out = {stage: 'none', scroll: [window.scrollX, window.scrollY],
               viewport: [window.innerWidth, window.innerHeight],
               tiles: [], icon: null, refresh: null, checkbox: null, result: null};
  const tiles = qa('.nine-content-img').filter(visible);
  if (tiles.length) {
    out.stage = 'nine_captcha';
    out.tiles = tiles.map(rect);
    // First VISIBLE match: live, a hidden element with the same class
    // came first in document order and the icon was silently dropped.
    const icon = qa('.header-content-img').find(visible);
    out.icon = icon ? rect(icon) : null;
    const refresh = qa('.nine-refresh').find(visible);
    out.refresh = refresh ? rect(refresh) : null;
    out.srcs = tiles.map((t) => t.currentSrc || t.src || '');
    out.loading = qa('.nine-content-loading').some(visible);
    if (qa('.nine-success').some(visible)) out.result = 'success';
    else if (qa('.nine-fail').some(visible)) out.result = 'fail';
    return out;
  }
  // "Click the following icons from left to right in sequence" — a
  // GeeTest-style panel in the light DOM: one 286x316 sprite painted as
  // the background of both the 286x286 picture and the 30px icon strip.
  const pic = qa('.captcha_click_wrapper .pic_wrapper').find(visible);
  if (pic) {
    out.stage = 'icon_click';
    out.image = rect(pic);
    const strip = qa('.captcha_click_wrapper .pic_elg_wrapper').find(visible);
    out.icons = strip ? rect(strip) : null;
    const confirm = qa('.captcha_click_confirm').find(visible);
    out.confirm = confirm ? rect(confirm) : null;
    const refresh = qa('.captcha_click_refresh').find(visible);
    out.refresh = refresh ? rect(refresh) : null;
    const bg = getComputedStyle(pic).backgroundImage;
    out.srcs = bg && bg !== 'none' ? [bg] : [];
    // A CSS background has no load event; after a refresh the new URL is
    // set before the sprite arrives (live: blank white screenshots went
    // to the solver). Probe the same URL — it resolves from cache once
    // the background has actually loaded.
    const m = /url\(["']?(.*?)["']?\)/.exec(bg || '');
    if (m) { const probe = new Image(); probe.src = m[1]; out.loaded = probe.complete && probe.naturalWidth > 0; }
    else out.loaded = false;
    const tips = qa('.captcha_click_tips_box')[0];
    out.tips = tips ? (tips.textContent || '').trim() : '';
    return out;
  }
  for (const r of roots) {
    for (const e of r.querySelectorAll('*')) {
      if (e.childElementCount === 0 && (e.textContent || '').trim() === '__LABEL__' && visible(e)) {
        out.stage = 'one_pass';
        out.checkbox = rect(e);
        return out;
      }
    }
  }
  return out;
}
""".replace("__LABEL__", CHECKBOX_LABEL)


class ChallengeDriver(Protocol):
    """What an engine has to provide. Every method is async; a sync
    engine (Selenium) wraps its calls — see selenium_scraper.py."""

    async def url(self) -> str: ...
    async def state(self) -> dict: ...  # result of STATE_JS
    async def screenshot(self, clip: dict) -> bytes: ...  # clip in PAGE coordinates, CSS px
    async def click(self, x: float, y: float) -> None: ...  # VIEWPORT coordinates, CSS px
    async def sleep(self, seconds: float) -> None: ...


class SolveBudget:
    """One cap on PAID solves for a whole run, shared by every place that
    creates a 2Captcha task (this module's grid/icon rounds and the
    engines' generic widget solver). CLAUDE.md §23: a per-pass limit that
    nothing sums is a bill — here up to 5 rounds x 3 block-retry passes,
    plus one generic solve per scroll round. `limit=0` means no paid solve
    at all."""

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self.spent = 0

    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    def try_spend(self) -> bool:
        if self.spent >= self.limit:
            return False
        self.spent += 1
        return True


@dataclass
class ChallengeOutcome:
    passed: bool
    rounds: int = 0
    solves: int = 0
    detail: str = ""


def on_challenge(url: str) -> bool:
    return CHALLENGE_URL_MARKER in (url or "")


def order_tiles(tiles: Sequence[Sequence[float]]) -> List[List[float]]:
    """Row-major order (top-to-bottom, then left-to-right), which is how
    GridTask numbers its answer. A tile joins the current row while its
    top is within half a tile height of that row's first tile, so
    sub-pixel layout noise cannot split or reorder a row."""
    rows: List[List[List[float]]] = []
    for t in sorted((list(t) for t in tiles), key=lambda t: t[1]):
        if rows and abs(t[1] - rows[-1][0][1]) < t[3] / 2:
            rows[-1].append(t)
        else:
            rows.append([t])
    return [t for row in rows for t in sorted(row, key=lambda t: t[0])]


def grid_clip(tiles: Sequence[Sequence[float]], scroll: Sequence[float] = (0, 0)) -> dict:
    x0 = min(t[0] for t in tiles)
    y0 = min(t[1] for t in tiles)
    x1 = max(t[0] + t[2] for t in tiles)
    y1 = max(t[1] + t[3] for t in tiles)
    return {"x": x0 + scroll[0], "y": y0 + scroll[1], "width": x1 - x0, "height": y1 - y0}


def rect_clip(rect: Sequence[float], scroll: Sequence[float] = (0, 0)) -> dict:
    return {"x": rect[0] + scroll[0], "y": rect[1] + scroll[1], "width": rect[2], "height": rect[3]}


def checkbox_point(label_rect: Sequence[float]) -> tuple:
    """The checkbox square sits just left of its label (live: square at
    x≈772, label starting at x=793, 19px tall)."""
    return label_rect[0] - 20, label_rect[1] + label_rect[3] / 2


def png_size(png: bytes) -> tuple:
    """(width, height) from a PNG's IHDR — screenshots come back at the
    device pixel ratio (2x on a Retina Mac), not in CSS px."""
    if len(png) >= 24 and png[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
    return 0, 0


def build_coordinates_task(image_png: bytes, icons_png: bytes) -> dict:
    return {
        "type": "CoordinatesTask",
        "body": base64.b64encode(image_png).decode(),
        "imgInstructions": base64.b64encode(icons_png).decode(),
        "comment": SEQUENCE_COMMENT,
    }


def parse_coordinates(solution: str, image_png: bytes, image_rect: Sequence[float]) -> List[tuple]:
    """CoordinatesTask's `{"coordinates": [{"x":..,"y":..}, ...]}` are in the
    screenshot's own pixels, in click order; map them back to viewport
    CSS px. Points outside the image are dropped."""
    try:
        data = json.loads(solution)
    except (TypeError, ValueError):
        return []
    raw = data.get("coordinates") if isinstance(data, dict) else None
    w, h = png_size(image_png)
    sx = (w / image_rect[2]) if w and image_rect[2] else 1.0
    sy = (h / image_rect[3]) if h and image_rect[3] else 1.0
    points = []
    for pt in raw or []:
        try:
            x, y = float(pt["x"]) / sx, float(pt["y"]) / sy
        except (TypeError, ValueError, KeyError):
            continue
        if 0 <= x <= image_rect[2] and 0 <= y <= image_rect[3]:
            points.append((image_rect[0] + x, image_rect[1] + y))
    return points


def build_grid_task(grid_png: bytes, icon_png: Optional[bytes]) -> dict:
    task = {
        "type": "GridTask",
        "body": base64.b64encode(grid_png).decode(),
        "rows": GRID_ROWS,
        "columns": GRID_COLUMNS,
        "comment": GRID_COMMENT,
    }
    if icon_png:
        task["imgInstructions"] = base64.b64encode(icon_png).decode()
    return task


def parse_grid_clicks(solution: str, tile_count: int = GRID_ROWS * GRID_COLUMNS) -> List[int]:
    """`TwoCaptchaClient.solve_and_wait` JSON-encodes a token-less
    solution; GridTask's is `{"click": [1, 4, 7]}`. Out-of-range and
    duplicate indices are dropped rather than trusted."""
    try:
        data = json.loads(solution)
    except (TypeError, ValueError):
        return []
    raw = data.get("click") if isinstance(data, dict) else None
    clicks: List[int] = []
    for n in raw or []:
        try:
            i = int(n)
        except (TypeError, ValueError):
            continue
        if 1 <= i <= tile_count and i not in clicks:
            clicks.append(i)
    return clicks


def tile_click_points(clicks: Sequence[int], tiles: Sequence[Sequence[float]], rng: random.Random) -> List[tuple]:
    points = []
    for n in clicks:
        t = tiles[n - 1]
        jx = t[2] * 0.25
        jy = t[3] * 0.25
        points.append((t[0] + t[2] / 2 + rng.uniform(-jx, jx), t[1] + t[3] / 2 + rng.uniform(-jy, jy)))
    return points


async def _read(driver: ChallengeDriver) -> tuple:
    """(url, state). A state read racing SHEIN's own success redirect
    fails with "execution context was destroyed" (seen live) — that is
    the page moving on, not an error, so it reads as an empty state."""
    try:
        state = await driver.state() or {}
    except Exception as exc:  # noqa: BLE001
        log.debug("challenge state read failed (likely mid-navigation): %s", exc)
        state = {}
    return await driver.url(), state


async def _wait_for(driver: ChallengeDriver, predicate: Callable[[str, dict], bool], timeout: float, step: float = 0.5):
    """Polls url()+state() until `predicate` holds; returns the last
    (url, state) either way."""
    waited = 0.0
    url, state = await _read(driver)
    while not predicate(url, state) and waited < timeout:
        await driver.sleep(step)
        waited += step
        url, state = await _read(driver)
    return url, state


def _left_gateway(url: str, _state: dict) -> bool:
    return not on_challenge(url)


def _center(rect: Sequence[float]) -> tuple:
    return rect[0] + rect[2] / 2, rect[1] + rect[3] / 2


def _ready(stage_state: dict, previous_srcs) -> bool:
    """A fresh, fully loaded widget of either solvable kind."""
    stage = stage_state.get("stage")
    if stage == "nine_captcha":
        return (len(stage_state.get("tiles") or []) == GRID_ROWS * GRID_COLUMNS and not stage_state.get("loading")
                and not stage_state.get("result") and stage_state.get("srcs") != previous_srcs)
    if stage == "icon_click":
        return bool(stage_state.get("image") and stage_state.get("icons") and stage_state.get("srcs")
                    and stage_state.get("loaded") and stage_state.get("srcs") != previous_srcs)
    return False


async def pass_risk_challenge(
    driver: ChallengeDriver, client: Optional[TwoCaptchaClient], *, max_rounds: int = 5,
    rng: Optional[random.Random] = None, solve: Optional[Callable[[dict], Awaitable[str]]] = None,
    step_timeout: float = 12.0, redirect_timeout: float = 20.0, debug_dir: Optional[str] = None,
    budget: Optional[SolveBudget] = None,
) -> ChallengeOutcome:
    """Drives one_pass -> (nine_captcha | icon_click) until the page leaves
    `/risk/challenge`. Each round handles whichever solvable widget SHEIN
    is showing at that moment. Never raises for anything page- or
    solver-side: a failure comes back as `passed=False` with a `detail`,
    and the caller keeps its normal blocked-outcome handling (CLAUDE.md §6).

    `debug_dir`, when set, receives each round's images and the widget as
    it looked after the clicks, for checking answers by eye.

    `solve` exists for tests; by default it runs the blocking
    `client.solve_and_wait` on a worker thread so an async engine's event
    loop stays responsive.
    """
    rng = rng or random.Random()
    outcome = ChallengeOutcome(passed=False)
    if max_rounds <= 0:
        outcome.detail = "disabled (--risk-challenge-rounds 0)"
        return outcome
    if solve is None:
        if client is None or not client.api_key:
            outcome.detail = "no TWOCAPTCHA_KEY — the image step cannot be solved"
        else:
            async def solve(task: dict) -> str:  # noqa: E306
                return await asyncio.to_thread(client.solve_and_wait, task, 5.0, 180.0)

    def dump(name: str, png: Optional[bytes]) -> None:
        if debug_dir and png:
            try:
                os.makedirs(debug_dir, exist_ok=True)
                with open(os.path.join(debug_dir, name), "wb") as fh:
                    fh.write(png)
            except OSError as exc:
                log.debug("could not write %s: %s", name, exc)

    async def refresh(state: dict) -> None:
        if state.get("refresh"):
            await driver.click(*_center(state["refresh"]))

    async def nine_round(round_num: int, state: dict) -> Optional[str]:
        """None = keep going; otherwise a terminal detail string."""
        tiles = order_tiles(state.get("tiles") or [])
        scroll = state.get("scroll") or (0, 0)
        grid_png = await driver.screenshot(grid_clip(tiles, scroll))
        icon_png = await driver.screenshot(rect_clip(state["icon"], scroll)) if state.get("icon") else None
        dump(f"round{round_num}_grid.png", grid_png)
        dump(f"round{round_num}_icon.png", icon_png)
        if budget is not None and not budget.try_spend():
            return f"solve budget exhausted ({budget.limit} paid solve(s) per run, --max-solves)"
        try:
            solution = await solve(build_grid_task(grid_png, icon_png))
        except TwoCaptchaAuthError as exc:
            return f"2Captcha auth error: {exc}"
        except TwoCaptchaError as exc:
            log.warning("SHEIN risk challenge round %d: GridTask failed: %s", round_num, exc)
            if any(code in str(exc) for code in TERMINAL_SOLVER_ERRORS):
                return f"2Captcha refused the account: {exc}"
            await refresh(state)
            return None
        outcome.solves += 1
        clicks = parse_grid_clicks(solution)
        log.info("SHEIN risk challenge round %d: GridTask answer %s.", round_num, clicks)
        if len(clicks) != EXPECTED_PICKS:
            log.warning(
                "SHEIN risk challenge round %d: answer picks %d tile(s), not %d — refreshing the grid "
                "instead of submitting a certainly-wrong answer.", round_num, len(clicks), EXPECTED_PICKS,
            )
            await refresh(state)
            return None
        for x, y in tile_click_points(clicks, tiles, rng):
            await driver.click(x, y)
            await driver.sleep(rng.uniform(0.25, 0.6))
        if debug_dir:
            try:
                dump(f"round{round_num}_after.png", await driver.screenshot(grid_clip(tiles, scroll)))
            except Exception as exc:  # noqa: BLE001 — debug only; the page may already be redirecting
                log.debug("after-click screenshot failed: %s", exc)
        url, after = await _wait_for(
            driver, lambda u, s: not on_challenge(u) or s.get("result") in ("success", "fail"), step_timeout,
        )
        if not on_challenge(url) or after.get("result") == "success":
            return "success"
        if after.get("result") == "fail":
            log.warning("SHEIN risk challenge round %d rejected by SHEIN — retrying with fresh images.", round_num)
        else:
            # Nothing auto-submitted — ask for a new set.
            await refresh(after)
        return None

    async def icon_click_round(round_num: int, state: dict) -> Optional[str]:
        scroll = state.get("scroll") or (0, 0)
        image_png = await driver.screenshot(rect_clip(state["image"], scroll))
        icons_png = await driver.screenshot(rect_clip(state["icons"], scroll))
        dump(f"round{round_num}_image.png", image_png)
        dump(f"round{round_num}_icons.png", icons_png)
        if budget is not None and not budget.try_spend():
            return f"solve budget exhausted ({budget.limit} paid solve(s) per run, --max-solves)"
        try:
            solution = await solve(build_coordinates_task(image_png, icons_png))
        except TwoCaptchaAuthError as exc:
            return f"2Captcha auth error: {exc}"
        except TwoCaptchaError as exc:
            log.warning("SHEIN risk challenge round %d: CoordinatesTask failed: %s", round_num, exc)
            if any(code in str(exc) for code in TERMINAL_SOLVER_ERRORS):
                return f"2Captcha refused the account: {exc}"
            await refresh(state)
            return None
        outcome.solves += 1
        points = parse_coordinates(solution, image_png, state["image"])
        log.info("SHEIN risk challenge round %d: CoordinatesTask answer %d point(s).", round_num, len(points))
        if len(points) < MIN_SEQUENCE_POINTS:
            log.warning("SHEIN risk challenge round %d: too few points — refreshing instead of submitting.", round_num)
            await refresh(state)
            return None
        for x, y in points:
            await driver.click(x, y)
            await driver.sleep(rng.uniform(0.35, 0.8))
        if debug_dir:
            try:
                dump(f"round{round_num}_after.png", await driver.screenshot(rect_clip(state["image"], scroll)))
            except Exception as exc:  # noqa: BLE001
                log.debug("after-click screenshot failed: %s", exc)
        if state.get("confirm"):
            await driver.click(*_center(state["confirm"]))
        await driver.sleep(1.0)
        _u, shown = await _read(driver)
        if shown.get("tips"):
            log.info("SHEIN risk challenge round %d: widget says %r.", round_num, shown["tips"])
        # Live 2026-09-30: an accepted answer shows "Successful!" and then
        # redirects; waiting only for the redirect or a new sprite read the
        # success as a rejection and burned a round.
        if _SUCCESS_TIPS_RE.search(shown.get("tips") or ""):
            return "success"
        srcs = state.get("srcs")
        url, after = await _wait_for(
            driver, lambda u, s: not on_challenge(u) or (s.get("srcs") and s.get("srcs") != srcs)
            or s.get("stage") not in ("icon_click", "none"), step_timeout,
        )
        if not on_challenge(url):
            return "success"
        # Live 2026-09-30, twice: an accepted answer swapped the sprite
        # BEFORE redirecting, with no "Successful!" readable yet — give the
        # redirect a moment before calling the round a rejection.
        if not _SUCCESS_TIPS_RE.search(after.get("tips") or "") and "fail" not in (after.get("tips") or "").lower():
            url, _late = await _wait_for(driver, _left_gateway, 4.0)
            if not on_challenge(url):
                return "success"
        log.warning("SHEIN risk challenge round %d not accepted (widget said %r) — retrying with the next image.",
                    round_num, after.get("tips") or "")
        return None

    try:
        # The widget is injected by SHEIN's own bundle after
        # domcontentloaded — live, it was still absent ~5s in on a slow
        # profile, so wait for any stage to render first.
        url, state = await _wait_for(
            driver, lambda u, s: not on_challenge(u) or s.get("stage") in STAGES, step_timeout,
        )
        if not on_challenge(url):
            outcome.passed = True
            outcome.detail = "not on the challenge page"
            return outcome

        if state.get("stage") == "one_pass" and state.get("checkbox"):
            x, y = checkbox_point(state["checkbox"])
            log.info("SHEIN risk challenge: clicking the 'I am human' checkbox.")
            await driver.click(x, y)
            url, state = await _wait_for(
                driver, lambda u, s: not on_challenge(u) or s.get("stage") in SOLVABLE_STAGES, step_timeout,
            )
            if not on_challenge(url):
                outcome.passed = True
                outcome.detail = "passed at the checkbox step"
                return outcome

        if state.get("stage") not in SOLVABLE_STAGES:
            outcome.detail = f"unrecognised challenge stage {state.get('stage')!r}"
            if debug_dir and state.get("viewport"):
                w, h = state["viewport"]
                sx, sy = state.get("scroll") or (0, 0)
                dump("unrecognised.png", await driver.screenshot({"x": sx, "y": sy, "width": w, "height": h}))
            return outcome
        if solve is None:
            return outcome

        previous_srcs = None
        for round_num in range(1, max_rounds + 1):
            outcome.rounds = round_num
            url, state = await _wait_for(
                driver, lambda u, s: not on_challenge(u) or _ready(s, previous_srcs), step_timeout,
            )
            if not on_challenge(url):
                outcome.passed = True
                outcome.detail = outcome.detail or f"passed (redirected after round {round_num - 1})"
                return outcome
            if not _ready(state, previous_srcs):
                outcome.detail = f"no fresh solvable widget (stage {state.get('stage')!r})"
                return outcome
            previous_srcs = state.get("srcs")
            if state["stage"] == "nine_captcha":
                result = await nine_round(round_num, state)
            else:
                result = await icon_click_round(round_num, state)
            if result == "success":
                url, _ = await _wait_for(driver, _left_gateway, redirect_timeout)
                outcome.passed = not on_challenge(url)
                outcome.detail = (f"passed at the {state['stage']} step" if outcome.passed
                                  else "success shown but no redirect")
                return outcome
            if result is not None:
                outcome.detail = result
                return outcome
        outcome.detail = outcome.detail or f"image step not passed after {max_rounds} round(s)"
        return outcome
    except Exception as exc:  # noqa: BLE001 — a driver failure degrades to "still blocked", never a crash
        outcome.detail = f"challenge driver error: {exc}"
        return outcome
