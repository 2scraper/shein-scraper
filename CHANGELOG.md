# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/) as closely as a CLI toolkit can manage. A patch
release means "fixes", not that every flag and default is frozen — a
behaviour-changing default gets called out explicitly in its entry below
rather than being a silent violation of that.

## [Unreleased]

### Added — 2026-09-30, third challenge widget (`icon_click`), plus the first live Selenium and Puppeteer runs
- **Puppeteer, live over `--cdp-endpoint`**: connected and scraped 10
  products, `status=complete`. That profile was already verified, so the
  challenge path itself has not run under Puppeteer yet.
- **Selenium cannot use the Browser API.** chromedriver's `debuggerAddress`
  takes a bare host:port with no credentials, and `run()` already refuses
  that. It was run live against a local Chrome instead:
  - A direct connection and headless Chrome through a US residential exit
    both got `/risk/action/limit` (a plain rate limit, nothing to solve).
  - `--headful` through the residential exit got the real
    `/risk/challenge`. The checkbox click registered, and SHEIN then
    showed a THIRD widget this repo had not handled yet: "click the
    following icons from left to right in sequence".
- New `icon_click` stage in `shein_challenge.py`. It sends a 2Captcha
  `CoordinatesTask` with the icon strip as `imgInstructions`, maps the
  answer from device pixels back to CSS px (the PNG's own width, since
  Retina screenshots are 2x), clicks the points in order and presses
  Confirm. It waits for the sprite to actually load (probed via `new
  Image()` on the background URL): live, three rounds' screenshots went
  to the solver blank white before this. Answers with fewer than 2 points
  are refreshed, not submitted. Every round now handles whichever widget
  SHEIN shows, so the stages can alternate.
- **Not passed live under Selenium.** After the load fix, all 5 rounds
  got "Verification Failed", including answers checked correct by eye
  (bike → deer → scissors → owl, in order). That is the same kind of risk
  rejection as `9001` above. Most likely cause: the chromedriver-
  controlled Chrome itself (`navigator.webdriver`), not the solver.
- `smoke_test.py`: 87 → 90 checks, including a real-Chromium replica of
  the `icon_click` panel.

### Added — 2026-09-30, automated pass of SHEIN's `/risk/challenge` (`shein_challenge.py`, `--risk-challenge-rounds`)
- Prompted by Roman asking for the scraper to get past the blocked page
  itself. This reverses the 2026-09-29 scope note below ("stays a manual,
  human step"), on Roman's explicit request.
- Captured live first, through a fresh US Browser API profile: the gateway
  is SHEIN's own, no third-party vendor. `validate_type=one_pass` shows an
  "I am human" checkbox. On click the server either lets the session
  through or escalates to `validate_type=nine_captcha`, an open-shadow-DOM
  `<nine-captcha-custom>` 3x3 image grid ("Please select all images
  according to the icon"). It has no confirm button: it auto-submits after
  the third pick, then shows `.nine-success` and redirects to the original
  URL, or shows `.nine-fail` and swaps in new images.
- New `shein_challenge.py` (driver-agnostic, same split as
  `captcha_solver.py`): `STATE_JS` reads the stage across every open
  shadow root. The checkbox is clicked with a human-ish mouse path; the
  grid and icon are screenshotted and sent as a 2Captcha `GridTask`
  (`rows=3`, `columns=3`, icon as `imgInstructions`); the returned tiles
  are clicked. A SHEIN-rejected round is retried with fresh images rather
  than reported as a bad answer: live, a verifiably correct answer still
  got `code=9001 "System error"`. All three engines call it on both the
  search and product-page paths, before the gateway URL is judged
  blocked, and it never raises (CLAUDE.md §6).
- `--risk-challenge-rounds N` (default 5, `0` disables; also skipped with
  `--solve-captcha off`). The checkbox step needs no key; the grid needs
  `TWOCAPTCHA_KEY`. With `--dump-html`, each round's grid/icon/after-click
  PNGs go to `<out>_challenge/`.
- Two bugs were found live and fixed before this shipped. (1) A hidden
  `.header-content-img` came first in document order, so the icon was
  silently dropped and the solver guessed blind. `STATE_JS` now takes the
  first visible match, pinned by a real-Chromium check against a replica
  of the captured shadow DOM. (2) A state read racing SHEIN's own success
  redirect raised "execution context was destroyed"; that now reads as
  navigation.
- Live results (Playwright, `--cdp-endpoint`): the prototype passed on
  round 2. The CLI run with `--risk-challenge-rounds 4` passed on round 3
  (before the icon fix) and finished `status=complete`, exit 0, 10
  products with prices on all 10. The earlier CLI run, before the widget-
  render wait was added, failed all rounds and exited 3. Selenium and
  Puppeteer are wired identically but have NOT been run live; neither
  driver is installed in this checkout's venv.
- A second fresh US profile, the same day, did NOT pass. With the icon
  fix in place, its first correct answer was rejected, and then 2Captcha
  workers returned 4-, 5- and 1-tile answers. The widget submitted the
  first three picks of each, so wrong answers reached SHEIN. On the next
  two runs every submission, including answers verified correct by eye,
  got `validation/check` `code=9001 "System error"` (10 of 10). Read as:
  `9001` is a risk-score rejection, not a wrong-answer verdict, and this
  profile is burned. Unconfirmed whether the wrong submissions caused
  that or the profile started out low-trust.
- In response, an answer that does not pick exactly 3 tiles is never
  clicked: the grid is refreshed instead. The widget submits on the third
  pick, and a two-pick answer was seen to sit unsubmitted, so a correct
  answer is three tiles. The GridTask comment now says "exactly 3". The
  Playwright engine logs every `validation/check` verdict
  (`code`/`msg`), the one signal separating a wrong answer from a risk
  rejection. Re-verified on a third fresh US profile: the checkbox
  escalated to the grid (`code=0`, `type=nine_captcha`), rounds 1 and 2
  were correct by eye (all cyclists, then all football) and still got
  `9001`, and round 3 got `code=0` and redirected. The CLI finished with
  `status=complete`, exit 0, 10 products with prices on all 10. Across
  the three fresh profiles, SHEIN rejected the first one or two correct
  answers every time, hence the default of 5 rounds.
- `smoke_test.py`: 78 → 87 checks.

### Added — 2026-09-29, block-risk-reduction: delay jitter, opt-in rate-limit cooldown, profile-reuse warning
- Prompted by Roman asking to reduce block risk after the captcha/rate-limit
  incidents above. Explicitly **not** in scope: identifying or
  auto-solving SHEIN's own bot-mitigation captcha (the real "click icons
  in sequence" coordinate widget confirmed live at
  `/risk/verify/identity/validation/resources` — see README "Known
  limitations") — that stays a manual, human step; this entry is only
  about not tripping the gate as often, and recovering better when it
  fires.
- **`--delay-jitter` (default 0.3, all three engines)**: every
  `--scroll-delay`/`--retry-delay`/`--rate-limit-cooldown` wait is now
  multiplied by a random factor in `[1-jitter, 1+jitter]` via a new
  `_jittered_delay()` helper, so a run's own request timing isn't
  perfectly periodic — previously every wait was the exact configured
  constant, every time. `0` restores the old exact-delay behavior (e.g.
  for reproducible tests). Verified behaviorally (not just structurally):
  `smoke_test.py` draws 200 real samples from each engine's own function
  and checks the bounds hold and the output is actually non-constant.
- **`--rate-limit-cooldown` (default `0.0`, off — all three engines)**:
  on SHEIN's own rate-limit gate (`/risk/action/limit`, confirmed live
  2026-09-21), this repo has always given up immediately with a log
  message suggesting "try again after at least five minutes" without
  ever actually doing that itself. Setting `--rate-limit-cooldown 300`
  (or any positive value) now waits that long (jittered) and retries
  ONCE on the same session before giving up for real, honoring that same
  observed cooldown instead of just printing it. Deliberately **off by
  default**: turning it on unconditionally would silently make a normal
  invocation take minutes longer the first time it gets rate-limited,
  which is a bigger behavior change than a patch-level default should
  make. Not yet exercised against a real SHEIN rate-limit live with a
  real 5-minute wait — `smoke_test.py` proves the retry-once wiring
  (guarded by a new `args._cooldown_used` flag so it can only fire once
  per run) and, separately, that the two call sites use the correct
  sync/async sleep style for their surrounding function (a real mistake
  this change introduced and caught in review before shipping — see
  below).
- **`--scraper-api-cdp` without `--scraper-api-profile-id` now logs a
  warning** at the moment it matters, instead of only in
  `scraping_browser_connection_url`'s own docstring: each run otherwise
  gets a fresh profile from 2Captcha's default pool rather than a warmed,
  reused identity, which is the opposite of what this repo's own
  `--block-retries`/context-reuse work above is trying to achieve.
- Caught in review before this shipped: the first draft of the
  `--rate-limit-cooldown` retry used a blocking `time.sleep()` inside
  Playwright's local/CDP browser loop, which runs inside `async def
  run()` — copied from the sibling `--scraper-api` code path, which really
  is sync. Fixed to `await asyncio.sleep()` there; a new structural
  `smoke_test.py` check now pins the sync/async style expected at each of
  the two call sites per engine so this can't silently regress.
- `smoke_test.py`: 72 → 77 checks (5 new: `_jittered_delay` behavior,
  jitter wired into every scroll/retry sleep site with no bare call left
  over, the cooldown flag/guard/retry-once wiring, the sync/async sleep
  style at each site, and the profile-reuse warning). All 77 confirmed
  passing live on Roman's own machine, alongside `.github/ci_checks.py`'s
  credential scan.


### Fixed — 2026-09-29, crash when a recycled Browser API session leaves `browser.contexts` empty
- Found live on Roman's own machine testing the context-reuse fix above:
  a real `--block-retries` run hit SHEIN's `/risk/challenge` twice in a
  row on the same Browser API profile (`captcha_type=909`, then `903`).
  On the third attempt, `browser.contexts` came back empty and
  `_new_context` raised `RuntimeError`, uncaught — crashing the whole run
  (exit 1, no `.meta.json`) instead of degrading that one attempt, which
  is exactly the CLAUDE.md §6 invariant this family is supposed to hold.
  The provider appears to recycle a profile's underlying session during a
  long block; this codebase had never observed that before now.
- Fixed: `_new_context` now falls back to a fresh, empty context when the
  persistent one is gone, logging a warning, instead of raising. That
  attempt loses whatever cookies the persistent context carried (a
  previously solved challenge no longer applies to it), but the run
  itself survives to report a normal blocked/zero-product outcome.
- New `smoke_test.py` regression check drives `_new_context` against a
  fake browser with `contexts == []` and asserts it falls back to
  `new_context()` instead of raising (72 checks total, up from 71).

### Fixed — 2026-09-29, keep Browser API profile cookies in Playwright
- A live US Browser API profile was redirected to SHEIN's
  `/risk/challenge?captcha_type=909`. The user completed the visible
  "I am human" step in Browser API Live. A diagnostic request using the
  profile's default CDP context then returned 10 products, while the
  scraper still created a fresh empty context for each run.
- Playwright now reuses the provider's default context for `--cdp-endpoint`
  and closes only the page it opened. Local browser runs still create and
  close their own isolated contexts. The regression check covers both.
- The full Playwright CLI then completed a live `dress` search with exit
  `0`, `status=complete`, 10 unique products, and prices on all 10.
  Passing the initial SHEIN challenge remains a manual step for this
  profile; this change preserves its result rather than automating it.

### Fixed — 2026-09-28, `puppeteer_scraper.py main()`: real `asyncio.get_event_loop()` crash, plus a new permanent regression guard for the whole `main()` entry point
- Found while re-verifying this repo end to end after Roman's direct
  feedback that "done" claims here have previously turned out to be
  incomplete — rather than repeat a claim, a new `smoke_test.py` check was
  written to actually drive each engine's real `main()` (not `run()`
  directly, which is all every prior check did) with its own driver
  symbol forced to `None`, mirroring a check `tipranks-scraper` already
  has for a real short-circuit bug found there on this same date. The new
  check confirmed no such short-circuit exists in any of shein-scraper's
  three `main()` functions (they were already thin wrappers) — but it
  also surfaced a real, independent, previously-undiscovered bug:
  `puppeteer_scraper.py`'s `main()` called
  `asyncio.get_event_loop().run_until_complete(run(args))`, which raises
  `RuntimeError: There is no current event loop in thread 'MainThread'`
  in any process where `asyncio.run()` has already run and closed a loop
  earlier — for example inside this very test suite, once it drives more
  than one engine's `main()`, or inside any pytest run, or any embedder
  that uses `asyncio.run()` elsewhere. A standalone `python3
  puppeteer_scraper.py ...` invocation in a fresh process was NOT
  affected (confirmed by reproducing the crash only after an earlier
  `asyncio.run()` call in the same process, and confirming its absence
  without one) — but this is still a real robustness gap, not a
  hypothetical one.
- Fixed by switching to `asyncio.run(run(args))`, matching
  `playwright_scraper.py`'s already-correct pattern in this same repo —
  also closes a small, previously-unnoticed parity gap between the two
  async engines' entry points (CLAUDE.md §4).
- **Not yet checked**: `tipranks-scraper` and `g2-scraper`'s
  `puppeteer_scraper.py` use the same `get_event_loop().run_until_complete`
  pattern (confirmed present via a quick grep, not yet fixed or even
  fully diagnosed there) — out of scope for this pass since Roman asked
  for shein-scraper only, but worth knowing this may be a family-wide
  gap, not unique to this repo.
- The new `smoke_test.py` check (68 total, up from 67) stays in the suite
  permanently, so this exact bug class — a real short-circuit OR a real
  crash hiding behind `main()`, invisible to every check that calls
  `run()` directly instead — cannot silently reappear in any of the three
  engines without failing the suite.
- **Confirmed on Roman's own machine, not just the sandbox** (see
  `TESTING.md` §10): 68/68 over 5 consecutive `smoke_test.py` runs, the
  credential scanner passing, a clean working tree, and two direct real
  CLI runs — `selenium_scraper.py` still correctly reports "not
  installed" with selenium genuinely absent, and `puppeteer_scraper.py`
  with pyppeteer genuinely present got past the asyncio setup into a real
  CDP connection attempt with no `RuntimeError`, where before the fix it
  crashed immediately in the same conditions.

### Added — 2026-09-28, `--scraper-api-cdp` automatic fallback when the Scraping Browser session itself fails
- Prompted directly by Roman asking what happens today if `--scraper-api-cdp`'s
  Scraping Browser session fails — the honest answer at the time was
  "nothing automatic": a failed CDP session just surfaced as
  `remote_api_error` (exit 5) like any other Scraper API error, with no
  attempt to fall back to `--scraper-api`'s plain default pool even though
  that pool was still a real, working option.
- All three engines now make exactly ONE automatic fallback attempt,
  without `cdp_url`, when the cdp-routed attempt itself fails with a
  Scraper API HTTP-level error (bad/expired `cdpurl`, or any other
  Scraper API problem — there's no clean signal to tell those apart, so
  any such failure gets the one fallback try). This is deliberately NOT
  triggered by a normal "blocked with zero products" outcome — that's
  what `--block-retries` is already for, and falling back to a pool with
  *less* captcha-solving capability would not help a genuinely-blocked
  page. The fallback is logged loudly (not silent), permanently drops
  `cdp_url` for the rest of that run (so a remaining `--block-retries`
  attempt doesn't keep hitting the same broken session), and is never
  attempted more than once per run — confirmed by `smoke_test.py`, which
  also confirms plain `--scraper-api` (no `--scraper-api-cdp`) is
  completely unaffected: still exactly one attempt, no fallback logic
  touched at all.
- This does trade away `--scraper-api-cdp`'s own benefits (country/profile
  pinning, 2Captcha's own captcha auto-solve) for the rest of a run that
  falls back — an intentional "degrade, don't just fail" choice, not a
  silent one: the warning names exactly what was lost.

### Added — 2026-09-28, `--scraper-api-cdp`: real captcha solving and country pinning for `--scraper-api`
- `--scraper-api` on its own has two documented gaps: no captcha solving
  at all (a solved token has no live page/DOM in that mode to be injected
  into), and no way to pin the exit country/locale (a clean fetch could
  land on a non-US shein.com storefront this repo's parser doesn't
  recognise). `scraper_api_client.scrape_url()` already had an untested
  `cdp_url` parameter for exactly this — 2Captcha's Scraper API accepts a
  caller-supplied CDP session (their `cdpurl` field) instead of using
  their own default browser pool — but nothing in this codebase had ever
  called it with one.
- `--scraper-api-cdp` (all three engines) fills that parameter with
  `TwoCaptchaClient.scraping_browser_connection_url()`'s own output —
  chaining the Scraper API to 2Captcha's OWN Scraping Browser product,
  not a caller-supplied `--cdp-endpoint` (kept separate on purpose: an
  arbitrary CDP session isn't known to support this field the way
  2Captcha's own does, and `--scraper-api` already ignores
  `--cdp-endpoint` for the same reason). This is what actually gives
  `--scraper-api` real captcha auto-solve (2Captcha's own Scraping
  Browser solves it on their side of the session before the HTML ever
  reaches this repo) and country pinning, via the new
  `--scraper-api-country`/`--scraper-api-profile-id` flags (the latter
  reuses a profile across runs the same way `scraping_browser_connection_
  url()`'s own docstring already recommends for `--cdp-endpoint`).
- `--scraper-api-cdp` requires `--scraper-api` (`EXIT_BAD_USAGE`
  otherwise, not a silent no-op); `--scraper-api-country`/
  `--scraper-api-profile-id` without `--scraper-api-cdp` log a warning
  rather than doing nothing silently.
- **Honesty note, same as every other live-testing gap in this repo**:
  wired and covered by `smoke_test.py` (structural checks across all
  three engines, plus a behavioral one that injects a fake Scraper API
  response and confirms the built `cdpurl` — country, profile id — really
  reaches `scrape_url()`, and that plain `--scraper-api` is unaffected
  when the flag is absent). None of this has been exercised against a
  real 2Captcha/shein.com session yet — see TESTING.md's new "Scraper
  API" section. This is a real, tested-as-wired fix, not a claim that
  captcha solving or country pinning have been confirmed working live.

### Fixed — 2026-09-22, all three engines: `scrape_product_page()` never attempted captcha solving, and the naive fix would have wasted 2Captcha spend
- Previously documented in README "Known limitations" as a known, real,
  not-yet-fixed gap: only `scrape_search()`'s scroll loop ever called
  `_maybe_solve_captcha`, so a direct `--url <product page>` run never
  tried to solve a captcha at all, even with `--twocaptcha-key` configured
  and `--solve-captcha` not `off`.
- Fixing it surfaced a second, more important bug along the way:
  `_maybe_solve_captcha`'s "are products already present, so don't bother
  solving" check defaults to `sp.count_result_cards` — correct for
  `scrape_search()`, but SEARCH-page-shaped, so it reads `0` on literally
  every product page by definition. Wiring that default straight into
  `scrape_product_page()` would have meant every normal product-page
  scrape misread itself as "0 products, might be blocked" the instant any
  generic bot-challenge marker was on the page — and the confirmed-real,
  site-wide reCAPTCHA v2 loader (see the earlier sitekey-detection entry
  in this file) is exactly such a marker. Best case, a false "unidentified
  widget" warning on a perfectly normal page; worst case, a real, paid
  2Captcha solve attempt against a page that was never blocked at all,
  whenever a real sitekey also happened to be present.
- Fixed by giving `_maybe_solve_captcha` an optional `count_product_links`
  override (all three engines), and passing a product-page-shaped one from
  `scrape_product_page()`: "did `sp.parse_product_page()` already find
  real data?" instead of "are there search-result cards?". Also added a
  single re-fetch of the page after a successful solve — there's no
  scroll/round loop here to pick an injected solution up naturally the way
  `scrape_search()`'s next round does, so without this the solve attempt
  would be purely theatrical (spend money, inject a token, never look at
  the result again).
- `smoke_test.py` gained two new checks: a structural one (every engine's
  `scrape_product_page()` calls `_maybe_solve_captcha` AND passes its own
  `count_product_links`, via AST inspection, not a substring grep) and a
  behavioral one (constructs a synthetic-but-realistic normal product page
  carrying the real site-wide reCAPTCHA loader, and proves the OLD default
  would NOT have skipped solving on it while the fix correctly does).

### Fixed — 2026-09-22, puppeteer engine: `Captcha.setAutoSolve` was never armed on the product-page path
- Found while building a sibling family member (flippa-scraper) around Roman's
  explicit requirement that captcha auto-solve must be armed on EVERY page an
  engine touches, not just its main search entry point — checking this repo
  for the same coverage surfaced a real, live gap: `puppeteer_scraper.py`'s
  `scrape_product_page()` accepted an `autosolve` parameter but never called
  `_enable_scraping_browser_auto_solve()` with it, unlike `scrape_search()` in
  the same file and unlike `playwright_scraper.py`, which arms it at BOTH its
  `scrape_search` and `scrape_product_page` call sites. Practical effect: a
  direct `python3 puppeteer_scraper.py --url <product page> --cdp-endpoint ...`
  run silently never armed 2Captcha's own auto-solve over the Scraping Browser
  API's CDP session — a captcha on a product page in that mode would not have
  been solved automatically, with no warning that coverage was missing.
- Fixed by adding the same `if autosolve: await _enable_scraping_browser_auto_
  solve(page)` call `scrape_search()` already had. `selenium_scraper.py` is
  correctly exempt (it refuses `--cdp-endpoint` outright — CLAUDE.md §6 — so
  there is no CDP session to arm this on in the first place).
- `smoke_test.py` gained a new AST-based regression check: every function
  across `playwright_scraper.py`/`puppeteer_scraper.py` that takes an
  `autosolve` parameter must actually call the helper somewhere in its body.
  This is a structural check (walks the real function bodies), not a
  substring grep, so it can't be satisfied by an unrelated mention of the
  helper's name elsewhere in the file.

### Added — 2026-09-22, `--scraper-api`: fetch via 2Captcha's Scraper API, all three engines
- Directly prompted by Roman asking us to actually use ALL FIVE of 2Captcha's
  products this repo has a key for — captcha solving, the Scraping Browser
  (CDP), the Fingerprint API, proxies, and the Scraper API — the last of
  which had never been wired in at all, despite `scraper_api_client.py`
  already being the shared HTTP client every other product goes through.
  `--scraper-api` (off by default) is a genuinely different mode from
  everything else this repo does: instead of a local or `--cdp-endpoint`
  browser navigating the page, ONE browserless HTTP call goes to
  `scraper.2captcha.com/tasks/sync` and 2Captcha's own infrastructure (their
  own headless browser, not ours) fetches and renders the page, handing back
  the HTML directly. No Playwright/Selenium/pyppeteer import is required at
  all in this mode (confirmed: it runs with no engine driver installed).
- **Live-tested against real shein.com, all three engines, 2026-09-22 —
  works, but is not a confirmed bypass.** Real product-page HTML does come
  back (confirmed: `gbRawData`, `S-product-card`, real JSON-LD all present),
  and the same `/risk/challenge` interstitial this repo already knows about
  from the browser engines shows up here too, intermittently — 2 blocked
  attempts then a clean one was the actual live pattern seen, so
  `--block-retries` (already implemented, reused unchanged) is exactly as
  useful here as on the browser path. **New, separate finding**: 2Captcha's
  Scraper API has no documented parameter to pin the exit country/locale —
  a clean (non-blocked) response landed on shein.com's Netherlands storefront
  in every live test run today, which this repo's parser (tuned for the US/
  English markup) correctly reads as zero products rather than crashing or
  miscounting (`EXIT_ZERO_PRODUCTS`, not `EXIT_OK` or `EXIT_BLOCKED`) — but
  it means a real run needs to retry until it happens to land on a
  recognised locale, which today is down to luck, not a setting. One
  response in testing also came back with an empty body at HTTP 200 for no
  clear reason — rare, not reproduced on immediate retry, handled the same
  as any other zero-product outcome rather than crashing.
- **Not implemented**: `scrape_url()`'s `cdp_url` parameter (2Captcha's
  `cdpurl` field, documented but untested) would let this mode's fetch run
  through a caller-controlled CDP session instead of 2Captcha's own default
  pool — e.g. a `--cdp-endpoint` Scraping Browser session, which DOES
  support country pinning. That's the most promising real fix for the
  locale problem above, left for a future pass rather than shipped
  untested.
- Single fetch only in this mode: `--max-scrolls`/`--stall-rounds`/
  `--scroll-delay` don't apply (nothing to scroll — a static HTML snapshot
  can't paginate itself), and `--solve-captcha` is a documented no-op (a
  solved token has no live page/DOM to be injected into). `--proxy`/
  `--cdp-endpoint`/`--fingerprint` are ignored with a warning, not silently
  dropped (CLAUDE.md §4/§6) — this mode brings its own exit IP/device via
  2Captcha's infrastructure.

### Added — 2026-09-22, `--block-retries`: retry a blocked outcome on the same session before giving up
- Directly prompted by Roman asking why this repo can't clear SHEIN's
  defenses the way other family members clear theirs, and pointing at
  https://github.com/2scraper for comparison. Checked: `etsy-scraper`
  (DataDome-protected, a comparably hard target) documents measuring
  exactly this pattern against its own site — one profile was refused
  (`t=bv`) on its first two requests, then cleared from the third attempt
  onward, with the explicit guidance "retry before you rotate" and "use a
  handful [of profile IDs] rather than minting one per run." This repo had
  no equivalent: every engine gave up on the FIRST blocked, zero-product
  outcome, never retrying on the same browser/proxy/CDP identity at all.
- All three engines now accept `--block-retries` (default `2`) and retry
  a blocked-with-zero-products outcome that many extra times on the SAME
  browser session — same exit IP, same CDP-provided device identity when
  `--cdp-endpoint` is set — before giving up, waiting `--retry-delay`
  between attempts. This is separate from `--retries` (navigation
  failures) and from rotating to a different `--proxy`/`--cdp-endpoint`,
  which stays a manual, between-runs decision. See README's "Known
  limitations" for what this can and can't be expected to fix — in
  particular, it does nothing for the `captcha_type=909` incident
  documented above, where the browser never leaves the gateway URL at all
  regardless of how many times the page is reloaded on the same session.
- Also worth citing directly: `farfetch-scraper`'s own README states "A
  real browser clears it silently — including an ordinary local Chromium
  on a clean residential IP... what a managed browser and proxies buy you
  is running at volume from many addresses without burning your own, not
  access to the first page," and separately warns that hammering one
  address with concurrent/repeated requests "is a faster way to get that
  address scored than to gather data." Today's own testing session (many
  rapid CLI runs plus prior browser-tool navigations, all against the
  same `"summer dress"` query) plausibly did exactly that to whatever
  identity/reputation SHEIN tracks — this is not yet proven for SHEIN
  specifically, but it's the same category of risk every sibling repo
  that's actually cleared a hard target warns about, and this repo's own
  docs didn't call it out anywhere before now.

### Corrected — 2026-09-22 (same day, right after the fix below shipped), what `captcha_type=909` actually does
- A second real hit (new `--cdp-endpoint`, after the first one failed with
  `proxy_timeout`) let the url-corroboration fix below prove something the
  first incident could only guess at: `page.url` stays on `/risk/
  challenge?captcha_type=909...` for the entire round loop — it does NOT
  redirect off to an unrelated page. What renders is SHEIN's own homepage
  content with no captcha DOM ever mounting. The earlier "browser moved
  past the gateway to an unrelated page" theory (below, and in
  `shein_parser.py`) is superseded by this — kept in place as a record of
  what the evidence looked like before per-round URL checking existed, not
  deleted. The fix itself stays; it's still correct for a genuinely
  different case where the URL DOES move on.

### Fixed — 2026-09-22 (later the same day), a stale-text false positive that made every engine re-flag "captcha detected" on a page the browser had already moved past
- With `TWOCAPTCHA_KEY` finally configured, a live `/risk/challenge?
  captcha_type=909` hit gave this repo's own solve path its first real
  chance to run — and exposed a real bug instead: SHEIN's redirect target
  is embedded as literal TEXT in the page's own SSR state (an
  `"originalUrl"` field, i.e. "where this session came from"), and that
  text can keep matching `BOT_CHALLENGE_MARKERS` on later scroll rounds
  even after the browser has navigated to a completely different,
  unrelated page (confirmed live: SHEIN's own homepage, zero captcha
  widgets, zero challenge iframes — only ad-tracking pixels). This
  produced five consecutive misleading `"detected_unidentified_widget"`
  warnings in one real run, none of which reflected an actual widget the
  solver failed on.
- All three engines now corroborate a marker match against the round's
  own current URL (`page.url` / `driver.current_url`) for every round
  after the first — round 0 stays unconditionally trusted, since it's
  corroborated by the page.url check that already runs right after the
  initial navigation. A stale match past round 0 now logs a clear
  "page has moved on, not re-flagging" warning instead of silently
  re-triggering captcha-solve logic (and the resulting `blocked` bump)
  every round. See `shein_parser.py`'s module docstring for the full
  write-up, including why the real `captcha_type=909` widget markup
  STILL hasn't been captured — whatever this gate is appears to resolve
  or time out on its own within seconds, handing the browser to an
  unrelated page rather than back to the original search URL, with no
  interactive challenge ever rendered along the way.

### Documented — 2026-09-22, a FOURTH real block shape (a genuine SHEIN "outOfService" 403 page) and a correction to the GeeTest/CDP-autosolve coverage claim
- `TWOCAPTCHA_KEY` is now configured; a live `--cdp-endpoint` +
  `--dump-html` run hit a plain HTTP 403 whose body was SHEIN's own
  "outOfService"/"System Updating" page (real `img.ltwebstatic.com`
  asset, per-request `EVENT ID:`), not a captcha and not either
  previously-seen `/risk/` redirect. No code change was needed — the
  existing `status >= 400` check already reports this correctly as
  `blocked` (exit 3) — but it's now a documented, confirmed-live fourth
  member of the "SHEIN declined to serve this request" family, alongside
  `/risk/challenge`, `/risk/action/limit`, and any other `>=400`.
- The same capture's `<head>` shows 2Captcha's Scraping Browser extension
  injecting `geetest/interceptor.js` and `geetest_v4/interceptor.js` into
  every page, which the 2026-09-14 capture that established the
  "confirmed CDP-autosolve coverage" list (Turnstile/Amazon WAF/
  Yandex/Lemin) didn't happen to show. `shein_parser.py`'s module
  docstring and `README.md`'s "Known limitations" are updated to say
  GeeTest is watched-for by the extension (plausible support), not
  "unconfirmed/absent" as previously implied — while being explicit this
  is still not a confirmed solve, since no live GeeTest widget has gone
  through the CDP session yet.

### Fixed — 2026-09-21 (later still, same day), a THIRD real bot-mitigation incident (`/risk/action/limit`) that silently reported as "empty" instead of "blocked"
- Direct follow-up to the diagnostic-logging fix just below: Roman re-ran
  the exact same command with the new logging in place, and the final
  URL gave a real answer — `https://us.shein.com/risk/action/limit?
  risk-id=E4913845744991097345`. A distinct real endpoint under SHEIN's
  own `/risk/` gateway family, different from the previously-documented
  `/risk/challenge`: the path name ("action/limit") and the complete
  absence of any captcha-shaped content on the redirect target both point
  at a plain RATE LIMIT, not a challenge — nothing for
  `captcha_solver.identify_widget()` to find, because there's nothing
  there to solve. A much simpler explanation than the "cookie-jar
  content swap" theory the previous entry left open: most plausibly this
  repo's own recent testing (several rapid CLI runs from Roman, plus an
  earlier research session's many rapid browser-tool navigations to the
  same site) tripped an ordinary rate limiter.
- **The real bug this exposed**: none of `BOT_CHALLENGE_MARKERS` or any
  engine's URL check knew about `/risk/action/limit` at all, so a run
  that hit it silently reported `empty` (exit 4) — wrong data about why
  zero products were found — instead of `blocked` (exit 3). `risk-id=`
  is shared across BOTH `/risk/` endpoints (confirmed present on the
  original `/risk/challenge` capture too), so it's deliberately NOT used
  as a marker by itself; the distinct path segments are.
- **Fixed**: `BOT_CHALLENGE_MARKERS` gained `/risk/action/limit`; a new
  `shein_parser.RISK_GATEWAY_URL_MARKERS = ("/risk/challenge",
  "/risk/action/limit")` tuple replaces every engine's old hardcoded
  `if "/risk/challenge" in page.url` check (six call sites across the
  three engines — two per engine, `scrape_search()` and
  `scrape_product_page()`) with `any(marker in page.url for marker in
  sp.RISK_GATEWAY_URL_MARKERS)`, so a future third incident only needs
  one tuple updated, not six call sites found by hand. The
  `detected_unidentified_widget` log message in all three engines is
  reworded to name both incidents instead of assuming `/risk/challenge`
  specifically.
- Three new `smoke_test.py` checks (57/57 total, up from 55): the
  existing "real captured incident" check is scoped to only the
  `/risk/challenge`-specific markers it can actually verify against that
  fixture (it would have broken the moment `/risk/action/limit` was
  added to the same tuple, since that marker doesn't appear in the
  `/risk/challenge` fixture); a new check for the rate-limit markers
  against the real URL plus a synthetic reproduction (no scrubbed real
  HTML capture committed for this one — the actual page carries
  third-party tracker noise not worth preserving just to prove a path
  marker matches); and a parity grep confirming all three engines use
  the shared tuple rather than a hardcoded single-incident string.
- No behavior change beyond correct classification — a rate-limited run
  still just fails (correctly, as `blocked` now); no automatic backoff or
  differentiated retry logic was added for this, since the real
  underlying trigger (this repo's own recent request volume, most
  likely) isn't something a code change here can fix, and guessing at
  retry tuning without confirming the actual mechanism would just be
  another unconfirmed guess layered on top of this one.

### Fixed — 2026-09-21 (later still, same day), a silent zero-products diagnostic gap found on Roman's own first live engine run
- Roman ran `playwright_scraper.py --query "summer dress" --max-results 10`
  himself for the first time — this repo's own execution environments had
  been network-blocked from a live run until now (see `TESTING.md`). It
  returned exit `4` ("empty") with only a generic warning. A re-run with
  `--dump-html` and a side-by-side check via the built-in browser tool
  (which already had shein.com cookies from earlier, unrelated browsing
  in the same profile) found a real, new failure shape: the exact
  confirmed-correct URL (`https://us.shein.com/pdsearch/summer%20dress/`)
  returned a completely normal 20-product page through the cookied
  session, but a freshly-launched, cookie-less Playwright context got a
  page with an EMPTY `<title>` and zero `bffProductsInfo`/`pdsearch`
  markers anywhere in ~1.58MB of HTML — no `/risk/challenge`, no `>=400`
  status, nothing `BOT_CHALLENGE_MARKERS`/`GENERIC_BOT_CHALLENGE_MARKERS`
  catches. Consistent with (not proven to be) shein.com serving a
  different page — its own landing/marketing SSR variant — for a request
  from a bare cookie jar. This correctly does NOT get reported as
  `blocked`, since there is no bot-mitigation signal at all — but the old
  generic warning gave no way to tell that apart from "this query
  genuinely has zero results" without a manual `--dump-html` + grep
  session.
- **Fixed**: `shein_parser.diagnose_unexpected_page(html)` — a small,
  non-classifying diagnostic (the page's actual `<title>`, whether either
  marker a real search page always has is present, byte length) — is now
  logged, along with the final URL, in every engine's "zero products,
  page not flagged as blocked" warning.
- **Also fixed, a real parity gap this exposed**: only
  `playwright_scraper.py` had this warning at all before — Selenium and
  Puppeteer silently said nothing in the identical situation. All three
  log it now, worded identically.
- Two new `smoke_test.py` checks (55/55 total, up from 53): one exercising
  `diagnose_unexpected_page()`'s title/marker extraction against a
  reduced synthetic reproduction of the real shape found (not a scrub of
  the actual capture, which carries third-party tracker noise not worth
  committing), one grepping all three engines for parity.
- **Still UNCONFIRMED, and now the highest-value next live test**: what
  actually causes this — a consent/locale gate on a bare cookie jar? A
  "new visitor" landing-page swap independent of the requested path?
  Something else? Worth checking directly: does a SECOND request from the
  SAME freshly-launched browser context (cookies now set from the first)
  succeed, or does every fresh launch hit this regardless of request
  history? See `shein_parser.py`'s module docstring, "First real engine
  run" section, for the full write-up and README "Known limitations" for
  the user-facing version.

### Fixed — 2026-09-21 (later still, same day), a real reCAPTCHA v2 detection gap; documented a new undetermined risk-fingerprint layer
- Prompted directly by Roman asking "точно ли гитест там? может еще какие
  то капчи есть?" (is it really GeeTest there? maybe there are other
  captchas too?) after the injection work above shipped — went back to a
  fresh live browser session against shein.com to check, rather than
  re-reading the existing three circumstantial GeeTest signals.
- **Found real, concrete evidence Google reCAPTCHA v2 is ALSO live on
  shein.com** — not circumstantial like GeeTest: an actual
  `google.com/recaptcha/api.js` script tag, a live `window.grecaptcha` v2
  object (`render`/`execute`/`getResponse`/`reset`/`ready`, confirmed NOT
  `.enterprise`), and a real sitekey
  (`window.gbCommonInfo.GOOGLE_VERIFY_SITEKEY =
  "6LcoBR4UAAAAAIi5xU3U_q37C3nFaSckeMaT-P5j"`) sitting in the page's own
  JS config object. This exposed a genuine, previously-undetected gap:
  `captcha_solver.identify_widget()`'s reCAPTCHA v2 pattern only matched
  a static `<div class="g-recaptcha" data-sitekey="...">`, which never
  appears on this site — the sitekey only ever lives in that JS variable,
  presumably handed to `grecaptcha.render()` programmatically later. The
  old code would have silently never detected this real, live vendor at
  all, meaning it could never even attempt to solve it.
- **Fixed**: `captcha_solver.py` gained
  `_RECAPTCHA_SITEKEY_ANYWHERE_RE` — reCAPTCHA's own sitekey shape (`6L`
  + 38 more URL-safe-base64 characters, 40 total, confirmed against
  Google's documented format and against shein.com's own real sitekey)
  searched for ANYWHERE on the page, gated on the v2 loader script
  actually being present so it can't misfire on an unrelated
  40-character token elsewhere. `identify_widget()` tries this as a
  fallback after the existing static-markup patterns, so a page that DOES
  render static markup is unaffected and the v3 `render=` loader still
  takes priority when it's the one present. A new `smoke_test.py` check
  (53/53 total, up from 52) reproduces the real page shape and asserts
  the fallback fires correctly, the v3-priority and static-markup paths
  are unaffected, and there's no false positive without the loader
  present.
- **Documented, not solvable from static analysis alone**: a third,
  previously-undocumented risk/fingerprinting layer, apparently
  proprietary and branded "Armor" (`armor.ltwebstatic.com`, a device-
  fingerprint SDK at `sc.ltwebstatic.com/.../devices/fpv2.7.js` calling
  `/devices/v3/profile/web` and a separate `/risk/verify/identity/
  validation/publish/sign/rule` endpoint), runs on every ordinary page
  load regardless of whether a challenge ever fires. Both scripts are
  minified/obfuscated with zero plaintext vendor markers found in them
  (checked directly — no "geetest"/"shumei"/"recaptcha" string anywhere
  in the ~188KB fingerprint script), so the vendor behind it is genuinely
  UNDETERMINED, not just unconfirmed. Read as: this layer likely scores
  every request silently and decides whether `/risk/challenge` fires at
  all, with whatever widget (if any) appears there as a step-up behind
  it — which still means the actual interactive widget inside a real
  `/risk/challenge` page remains uncaptured, for any vendor. Five more
  rapid category searches in this session did not reproduce the
  redirect, consistent with the original incident's own "not
  reproducible on every request" note.
- Net effect: GeeTest remains the best-supported specific guess for what
  `/risk/challenge` itself shows (unchanged — still three circumstantial
  signals, still no live capture of the actual widget), but this work
  closes a real blind spot in coverage of the one OTHER vendor that
  turned out to be concretely confirmed, and documents a third layer this
  repo previously didn't know existed. See README "Known limitations"
  and `shein_parser.py`'s module docstring for the full write-up.

### Added — 2026-09-21 (later still, same day), captcha solution injection into the page
- Closes the gap the GeeTest work below flagged without fixing: every
  engine's `_maybe_solve_captcha` got a solved token/solution back from
  2Captcha but only logged "Captcha solved..." — nothing ever wrote it
  into the page. Prompted by a direct follow-up question after that
  entry shipped: "ну нужно чтобы в любых случаях использование решение
  гитест было" (roughly: the GeeTest solution needs to actually get
  used/applied, not just obtained). Since the underlying gap was
  identical for every widget type this repo recognizes, not just
  GeeTest, it's closed generically for all of them.
- `captcha_solver.build_injection_script(captcha_type, solution) ->
  Optional[str]` builds (never runs) the JavaScript that writes a solved
  captcha back into the page. It stays inside this shared module's
  existing "no site knowledge" charter, reinterpreted slightly: no
  page-execution primitive (`page.evaluate` / `execute_script`) crosses
  the module's boundary, only JS *source text* — the engine, which
  already speaks its own driver's dialect, is the one that actually runs
  it. All three engines (`playwright_scraper.py` — `await
  page.evaluate(script)`; `puppeteer_scraper.py` — same, pyppeteer's
  async `page.evaluate`; `selenium_scraper.py` — synchronous
  `driver.execute_script("return " + script)`, since `execute_script`
  needs an explicit `return` to hand a value back, unlike the other two)
  now call it from their "solved" branch, wrapped in try/except so a
  failed injection degrades to a logged warning, never crashes the run.
- Each widget type gets its own STANDARD, publicly-documented
  client-integration convention — **not** anything confirmed against a
  real shein.com (or any family-site) widget capture, which has never
  happened for ANY type: Turnstile writes `cf-turnstile-response` (field
  + `data-callback`); reCAPTCHA v2 writes `g-recaptcha-response` (field +
  `data-callback`); hCaptcha writes `h-captcha-response` (field +
  `data-callback`); GeeTest v3 writes named `geetest_challenge`/
  `geetest_validate`/`geetest_seccode` fields, trying both prefixed and
  unprefixed solution keys since 2Captcha's own docs are inconsistent
  about naming; GeeTest v4 (weakest-confidence branch — no standard field
  convention documented anywhere) stashes the solution on
  `window.__2captcha_geetest_v4_solution`, dispatches a
  `CustomEvent('geetest_v4_solved', ...)`, and calls
  `window.geetest_validate_callback(...)` IF that global happens to
  exist. reCAPTCHA v3 gets no injection script at all — it's invisible,
  and the token is typically consumed the instant the SITE'S OWN
  JavaScript resolves `grecaptcha.execute()`'s promise (often straight
  into an XHR, never read back off a DOM element), so guessing at
  site-specific consumption code would violate this module's own
  no-site-knowledge charter; `build_injection_script()` returns `None`
  for it on purpose, and the raw token is still returned to the caller
  for anyone with actual site-specific knowledge to use.
- Two new permanent `smoke_test.py` checks (52/52 total, up from 50):
  one exercising `build_injection_script()`'s JS text/field names per
  widget type (including the GeeTest JSON-decode path and the
  `RECAPTCHA_V3 -> None` case), one grepping all three engine files to
  assert they actually import and call it from their "solved" branch —
  parity regression protection, same pattern as this file's existing
  "all engines check the confirmed-real /risk/challenge redirect" check.
- Discovered, not fixed, while wiring this in: `scrape_product_page()` —
  the `--url` path pointed at a single product page, in every engine —
  never calls `_maybe_solve_captcha` at all. Only `scrape_search()`'s
  scroll loop attempts a captcha solve; a `/risk/challenge` redirect hit
  while fetching a single product page is still correctly detected as
  `blocked` (the URL check runs independently) but no 2Captcha solve is
  ever attempted for it. See README "Known limitations".
- Over `--cdp-endpoint`, none of this local-injection code runs — see
  the GeeTest entry below for the CDP `Captcha.setAutoSolve` path, which
  is unaffected by this change.

### Added — 2026-09-21 (later still), GeeTest support in captcha_solver.py
- `captcha_solver.py` gained `CaptchaType.GEETEST_V3`/`GEETEST_V4`,
  detection patterns in `identify_widget()`, and a `_task_payload()` branch
  that builds the correct 2Captcha task (`GeeTestTaskProxyless`/
  `GeeTestV4TaskProxyless`, with `gt`/`challenge` or `captchaId` — no
  `websiteKey` at all, unlike every other type this module already
  supported). Prompted by shein.com's `/risk/challenge` incident
  correlating with GeeTest (three circumstantial signals, see
  `shein_parser.py`'s module docstring) and a direct question about why
  GeeTest wasn't already supported given this repo's `--cdp-endpoint` path
  solves captchas.
- Fixed a real crash this surfaced in `scraper_api_client.solve_and_wait()`:
  it only ever returned `solution.token`/`solution.gRecaptchaResponse` and
  raised `TwoCaptchaError` on anything else — correct for every widget it
  previously supported (a single opaque token), but GeeTest's solution is
  several fields together (v3: `challenge`/`validate`/`seccode`; v4:
  `captcha_id`/`lot_number`/`pass_token`/`gen_time`/`captcha_output`), so
  every successful GeeTest solve would have raised instead of returning.
  Now falls back to a JSON-encoded solution dict when there's no single
  token field, with a caller-facing docstring explaining the shape
  difference.
- Documented at the time, not fixed yet in this entry (a real,
  pre-existing, family-wide gap, not new): no engine in this family
  actually injects a solved token back into a locally-launched
  (non-`--cdp-endpoint`) page — `_maybe_solve_captcha` only logs
  "solved". This GeeTest work makes the detection/task-building plumbing
  correct up to that point; the injection step remains open for every
  widget type, not just GeeTest, and needs real captured widget markup
  (still never seen, for GeeTest or any other type, on this site) to
  implement correctly rather than guess at.
  **Closed later the same day** — see the "captcha solution injection
  into the page" entry above (it's listed first because it's newer);
  the "needs real captured widget markup to implement correctly" framing
  turned out to not be a hard blocker: each widget's own standard, public
  convention was enough to implement the injection itself, just not
  enough to CONFIRM it against shein.com's real markup, which is still
  unconfirmed and called out plainly in that entry.
- `identify_widget()`'s new GeeTest patterns are explicitly marked
  UNCONFIRMED (built from GeeTest's own public integration docs, not a
  shein.com capture) — this incident's actual challenge widget was never
  reached, only the `/risk/challenge` redirect shell. See README "Known
  limitations".

### Verified live, 2026-09-21 (later the same day) — real end-to-end parse of live data
- `shein_parser.parse_search_results()` was run for the first time against
  a genuinely fresh, real `window.gbRawData` snapshot — fetched live via
  the built-in browser-rendering tool navigating
  `https://us.shein.com/pdsearch/dress/` (no `/risk/challenge` redirect
  this time), not a hand-written fixture. All 10 of the first batch's
  products parsed cleanly (`source_used=gb_raw_data`), and the resulting
  `Product` rows round-tripped through `output_writer.finish_run()` as a
  clean `status=complete`, exit `0`, `price_confirmed_pct: 1.0` run.
  Real example row: `{"sku": "460570094", "title": "Louniche Women's
  Summer Blue And White Stripe Round Neck Waist Gathered Maxi Dress...",
  "brand": "Louniche", "price": 9.93, "original_price": 15.89,
  "discount_pct": 38.0, "rating": 4.23, "review_count": 300, "in_stock":
  true}`.
- Saved as `tests/fixtures/shein_search_live_dress_20260921.json` (a real
  capture, not synthetic — see its own `_provenance` field) with a new
  permanent `smoke_test.py` regression check
  (`parse_search_results against a REAL live capture...`) asserting all
  10 rows parse to plausible products and the `finish_run()` round-trip
  stays clean, so this doesn't silently regress.
- **Still NOT closed by this**: this exercised `shein_parser.py`'s parsing
  logic against real data, not this repo's own `playwright_scraper.py` /
  `selenium_scraper.py` / `puppeteer_scraper.py` end-to-end (browser
  launch, navigation, scroll loop, retries). Attempting that live run
  found that both this repo's cloud build environment and the linked
  device's own shell currently have network egress policies that block
  `us.shein.com` directly (`403 blocked-by-allowlist`) AND block
  downloading the Chromium binary Playwright itself needs
  (`cdn.playwright.dev` also `blocked-by-allowlist`) — so a full engine
  run needs either a network policy change or a different execution
  environment, not a code fix. See `TESTING.md` for what to try.

### Added — initial build, fifth member of the 2scraper family
- First build of `shein-scraper`, following `stockx-scraper` /
  `skyscanner-scraper` / `lidl-scraper` / `perplexity-scraper`'s established
  shape: three engine scripts (`playwright_scraper.py` primary,
  `selenium_scraper.py`, `puppeteer_scraper.py`), the same family-shared,
  no-site-knowledge modules ported near-verbatim from `lidl-scraper`
  (`output_writer.py`, `proxy_pool.py`, `captcha_solver.py`,
  `fingerprint_client.py`, `scraper_api_client.py`, `diff_runs.py`,
  `env_config.py` — per CLAUDE.md §7), and `shein_parser.py` as this repo's
  own site knowledge.
- Unlike most of this family's first builds, `shein_parser.py` is grounded
  in a real, live browser capture from the start, not a guess awaiting live
  correction: the confirmed-real search URL (`/pdsearch/{query}/`), category
  URL (`{Category}-c-{id}.html`), product URL (`{slug}-p-{goods_id}.html`),
  the primary `window.gbRawData` embedded-state data source for a listing
  page (a JS global, not JSON-LD — a search page's only JSON-LD is a plain
  `BreadcrumbList`), the product-detail page's real schema.org
  `ProductGroup` JSON-LD block, and a real, twice-confirmed bot-mitigation
  incident (`/risk/challenge?captcha_type=909&...`, SHEIN's own in-house
  risk gateway, not reproducible on an immediate retry in the same
  session). See README "Read this before trusting a run" and
  `shein_parser.py`'s own module docstring for the full write-up.
- `_extract_balanced_json_assignment()`: a brace/quote/escape-aware scanner
  (not a naive regex) for pulling `window.gbRawData = {...}` out of raw
  HTML — needed because the assignment's value can contain arbitrarily
  nested braces inside quoted string fields (e.g. a product title), which
  would truncate a lazy regex at the first unrelated `}`. A dedicated
  `smoke_test.py` check exercises this against a fixture with a
  deliberately brace-containing title.
- A new architectural pattern for this family: a single `--url` flag
  auto-routes between two different real page types and parsers —
  `_resolve_start_url()` detects a product-detail URL (`-p-{id}.html`,
  no `pdsearch` segment) and routes it to the JSON-LD single-product parser
  (`parse_product_page()`) instead of the `window.gbRawData` listing parser
  used for a search/category URL. All three engines agree on this routing;
  a `smoke_test.py` check asserts it.
- `Product` schema extended with fashion-marketplace-specific fields not
  present in any prior sibling repo: `original_price`, `discount_pct`,
  `rating`, `review_count`, `store_code`, `in_stock`, `is_clearance`,
  `quickship` — SHEIN is a multi-seller marketplace with per-listing
  rating/review counts and fulfilment flags that neither grocery items,
  flight itineraries, nor marketplace resale listings needed to represent
  the same way.
- `shein_parser.BOT_CHALLENGE_MARKERS` (`/risk/challenge`, `captcha_type=
  909`) is, unlike every prior family incident, the ONLY detection path for
  its incident — `captcha_solver.GENERIC_BOT_CHALLENGE_MARKERS` does not
  catch it on its own, since SHEIN's own gateway page carries no vendor-
  identifiable widget markup and the generic list has no "geetest" entry.
  `tests/fixtures/shein_risk_challenge_real.html` is a scrubbed real
  capture of the incident; a `smoke_test.py` check asserts the generic
  detector alone does NOT flag it, to keep this distinction from silently
  regressing.
- `robots.txt`-confirmed disallowed paths (`/user/`, `/cart/`, `/geetest/`,
  `/atomic/`, `/abt/userinfo`, and a few narrower exact paths) are refused
  outright by every engine's `_resolve_start_url()`, even for a
  human-supplied `--url` — never silently requested.

### Family-shared modules, ported unchanged in substance from lidl-scraper
- `env_config.ENV_KEYS` renamed to this repo's own `SHEIN_PROXY` /
  `SHEIN_CDP_ENDPOINT` / `SHEIN_URL` (kept in sync with `.env.example`,
  verified by a `smoke_test.py` check per CLAUDE.md §17).
- `output_writer.py`'s exit codes, `STATUS_BY_EXIT` map, and
  `finish_run()` precedence logic are byte-for-byte identical to every
  prior family member — only `Product`'s site-specific tail differs.
- `diff_runs.py`'s description string updated to name this repo; its
  sku-diff logic is unchanged and unit-tested against two real
  `finish_run()` outputs in `smoke_test.py`.

### Known open items (see README "Known limitations" / `TESTING.md`)
- No engine here has been run live against the real site yet — the research
  above comes from a browser-rendering tool, not this repo's own code. A
  first live engine run is the highest-value remaining check.
- Whether scrolling a search page past its first `window.gbRawData` batch
  (confirmed 20 products) actually grows it with new products, fires a
  separate request, or needs a different trigger is unconfirmed.
- `--sort`'s real query-parameter shape is unconfirmed and not yet wired
  into `search_url()`.
- DOM fallback selectors in `shein_parser.py` are unverified guesses,
  never needed against the real site so far.

[Unreleased]: https://github.com/2scraper/shein-scraper/compare/main...HEAD
