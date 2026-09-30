# Testing with real credentials and the live site

**Updated 2026-09-29:** this repository's own Playwright engine has now
completed a live search through a US Browser API profile after a manual
"I am human" verification in that profile. The run returned 10 distinct
products with prices, `status=complete`, exit `0`. The earlier 2026-09-21
network-allowlist blocker described in this document is no longer present
on the machine used for this run. A fresh unverified profile is still
sent to `/risk/challenge`; the successful run demonstrates persistence of
the manually verified profile, not automatic bypass of that challenge.

Still requiring live verification: Selenium and Puppeteer end-to-end, whether
scroll-driven pagination actually grows `window.gbRawData` past its first
batch, the DOM fallback selectors, and `--sort`'s real query-parameter
shape. Offline checks (`smoke_test.py`) remain useful, but do not
substitute for these runs.

Run everything below from a normal terminal on your own machine — wherever
this repo lives for you.

## 1. Basic setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-playwright.txt
playwright install chromium
cp .env.example .env
```

Leave `.env` blank for the first run — the whole point of "local-first" is
that nothing in it is required. Fill in `TWOCAPTCHA_KEY` /
`SHEIN_PROXY` / `SHEIN_CDP_ENDPOINT` later, only if you want to test those
specifically.

## 2. The most important run you can do: the first live engine run

```bash
python3 playwright_scraper.py --query "summer dress" \
  --max-results 10 --format json --out /tmp/shein_test.json --dump-html
echo "exit code: $?"
cat /tmp/shein_test.json.meta.json 2>/dev/null || echo "(no sidecar — see below)"
```

Four outcomes, and what each one means:

- **`exit code: 0`, a `.meta.json` with `"status": "complete"` and a
  believable `product_count`**: `extract_gb_raw_data()` matched the real
  page, same as the manual capture this repo's parser is built from. Open
  `/tmp/shein_test.json` and actually look at a few rows — plausible-
  looking `price`/`brand`/`title`/`rating` is what "confirmed" looks like.
  This is the expected outcome given how solid the underlying research is,
  but it has not been observed from this repo's own code yet — confirming
  it is the whole point of this step.
- **`exit code: 4` (zero products), no `.meta.json` written** (by design —
  see `output_writer.finish_run`): open `shein_test_debug.html` and check,
  in this order: (1) does it actually contain a `window.gbRawData =`
  assignment at all — if the site has changed this shape, that's genuinely
  new information, not a bug in the brace-scanner; (2) if `gbRawData` IS
  present, does `_extract_balanced_json_assignment()` actually parse it —
  a change to the assignment's own syntax (e.g. a trailing semicolon
  moved, a different variable name) is the likely culprit if the JSON
  itself looks fine; (3) compare against `shein_parser.py`'s documented
  key names (`goods_id`, `salePrice`, etc.) in case SHEIN renamed a field.
- **`exit code: 3` (blocked)**: either shein.com's own `/risk/challenge`
  redirect (the confirmed, captured incident — see README "Known
  limitations") or a generic marker hit. If you land on `/risk/challenge`,
  note whether it recurred on an immediate retry — the one capture so far
  was NOT reproducible on the very next request in the same session, which
  is the single most useful thing to either confirm or refute here. If you
  hit a genuinely NEW block page (different URL shape, different query
  param), save a scrubbed capture and extend `shein_parser.
  BOT_CHALLENGE_MARKERS` the same way this repo's own fixture was built
  (see `CONTRIBUTING.md`).
- **A real page renders with fewer products than expected after scrolling**:
  this is the other likely first-run finding given "Read this before
  trusting a run" in the README (scroll-driven `gbRawData` growth is
  unconfirmed) — if so, that's exactly the open question `shein_parser.py`
  and every engine's scroll loop need resolved: does scrolling grow
  `gbRawData` in place, fire a separate XHR, or need a different trigger?
  Capture the network activity (`--dump-html` alone won't show it) and
  document the answer.

Whatever you find, **updating `shein_parser.py`'s selectors/heuristics to
match what you actually saw — with a saved, scrubbed fixture under
`tests/fixtures/` and a new `smoke_test.py` check against it — is the
single most valuable contribution this repo can receive** (see
`CONTRIBUTING.md`).

## 3. Selenium, for real

```bash
python3 -m venv .venv-selenium   # separate venv — see README "Engines"
source .venv-selenium/bin/activate
pip install -r requirements-selenium.txt
python3 selenium_scraper.py --query "summer dress" --out /tmp/shein_selenium.json
```

## 4. Puppeteer (pyppeteer), for real

```bash
python3 -m venv .venv-puppeteer
source .venv-puppeteer/bin/activate
pip install -r requirements-puppeteer.txt
python3 puppeteer_scraper.py --query "summer dress" --out /tmp/shein_puppeteer.json
```

## 5. The 2Captcha REST API, with your real key

Confirms the key and hits a real, billed-nothing endpoint first:

```bash
python3 -c "
import env_config
from scraper_api_client import TwoCaptchaClient
args = type('A', (), {'twocaptcha_key': None, 'proxy': None, 'cdp_endpoint': None, 'url': None})()
env_config.apply_env(args)
c = TwoCaptchaClient(args.twocaptcha_key)
print('balance: \$%.2f' % c.get_balance())
"
```

## 6. The Scraping Browser API (`--cdp-endpoint`), for real

`SHEIN_CDP_ENDPOINT` in `.env` is picked up automatically:

```bash
python3 playwright_scraper.py --query "summer dress" --out /tmp/shein_cdp.json
```

An unverified Browser API profile did receive `/risk/challenge?captcha_type=909`
in a live run. The user passed its "I am human" step in Browser API Live;
the same profile then returned 10 real products through the Playwright CLI.
Reusing the profile's default context is required to retain those cookies.

**Gotcha**, same as the rest of the family: if `.env` has BOTH
`SHEIN_CDP_ENDPOINT` and `SHEIN_PROXY` set, the code ignores `SHEIN_PROXY`
and warns — a CDP session already carries its own exit IP, stacking a
second one on top is a contradiction, not better cover (same for a
fingerprint over `--cdp-endpoint`). Comment out whichever you're not
testing if you want to test them in isolation.

### 6a. The automated `/risk/challenge` pass

Needs a profile SHEIN has NOT verified yet. A verified one skips the
challenge entirely, so there is nothing to test. Keep `--dump-html` on so
every round's grid, icon and after-click screenshot lands in
`/tmp/shein_ch_challenge/`:

```bash
python3 playwright_scraper.py --query "summer dress" --max-results 10 \
  --cdp-endpoint "$FRESH_PROFILE_WS" --block-retries 0 --risk-challenge-rounds 4 \
  --dump-html --out /tmp/shein_ch.json
```

Look for `Passed SHEIN's risk challenge` in the log and `status=complete`
in the sidecar. If rounds keep failing, open `roundN_icon.png`: a missing
icon file means the widget markup changed (see `STATE_JS`). Every
round logging `validation/check: code=9001`, including correct answers,
means SHEIN has written this profile off: switch profiles. Confirmed
live for Playwright on 2026-09-30 (2 of 3 fresh profiles passed, each on
round 3); Selenium and Puppeteer still need this
run.

## 7. The residential proxy (`--proxy` / `SHEIN_PROXY`), for real

```bash
python3 playwright_scraper.py --query "summer dress" --out /tmp/shein_proxy.json
```

## 8. The Scraper API (`--scraper-api` / `--scraper-api-cdp`), for real

```bash
python3 playwright_scraper.py --query "summer dress" --scraper-api --out /tmp/shein_scraper_api.json
python3 playwright_scraper.py --query "summer dress" --scraper-api --scraper-api-cdp \
    --scraper-api-country us --scraper-api-account-id YOUR_EXISTING_US_ACCOUNT_ID \
    --out /tmp/shein_scraper_api_cdp.json
```

Create/configure a US Browser API account before this run. The country
flag checks the account's saved country; it does not change its proxy.
`smoke_test.py` verifies Browser API account selection and retrieval of a
ready-made connection URL with fake responses. A successful end-to-end
SHEIN extraction over this route remains unverified.

**The automatic fallback** (added 2026-09-28: if the Scraping Browser
session itself fails, one automatic retry without `cdp_url`) is covered
by `smoke_test.py` against a fake client that fails only when `cdp_url`
is set, but has never been triggered by a REAL Scraping Browser failure.
To force it live, pass an obviously-invalid `--scraper-api-profile-id`
(or run with an account that has no Scraping Browser profiles left) and
confirm in the log: the "Scraping Browser session failed — falling back"
warning fires, the run still finishes (exit 0, not 5) using the plain
pool, and a second such run without `--scraper-api-cdp` at all is
unaffected.

## 9. Push to GitHub and let CI do the rest

```bash
git remote add origin git@github.com:2scraper/shein-scraper.git
git push -u origin main
git push --tags
```

Then, in the GitHub repo's Settings:

- **Secrets and variables → Actions**: add `TWOCAPTCHA_KEY`,
  `SHEIN_CDP_ENDPOINT` / `SHEIN_PROXY` (only if you want the `canary-cdp`
  job using them — `canary-local` needs no secrets at all), and
  `CLAUDE_CODE_OAUTH_TOKEN` (for `claude.yml` / `claude-code-review.yml` —
  both silently no-op without it, by design, rather than failing every
  PR check).
- **Actions → canary → Run workflow**: dispatch it manually at least once
  rather than waiting a day for the cron and trusting the badge blind —
  this is this repo's actual FIRST live engine test, so look at the run's
  log and uploaded artifact, not just the badge color.

## 10. What "done" looks like

- `tests.yml` green on both Python versions and all three `engine-smoke`
  matrix legs.
- At least one manually-dispatched `canary.yml` run, looked at — not just
  the badge — including whichever of the outcomes in step 2 above it
  landed on.
- If step 2 landed on anything other than a clean `complete` with
  plausible-looking rows: `shein_parser.py` updated to match what you
  actually saw, with a fixture under `tests/fixtures/` and a new
  `smoke_test.py` check, per `CONTRIBUTING.md`.
- The scroll-pagination question (see README "Pagination") answered one way
  or the other, with the mechanism documented here and in
  `shein_parser.py`'s module docstring.
- Whether `/risk/challenge` recurs, and under what conditions (proxy?
  fingerprint? plain local run?), documented one way or the other.
- Step 8's two open questions (`--scraper-api-cdp` locale selection and
  actual captcha auto-solve) answered one way or the other, with README/
  CHANGELOG updated from "wired, not yet exercised live" to whatever was
  actually observed.
- Step 8's automatic-fallback question (does the "Scraping Browser session
  failed — falling back" path actually fire and recover on a real,
  forced Scraping Browser failure, not just the faked one in
  `smoke_test.py`) answered the same way.
- **Added 2026-09-28**: `smoke_test.py` now drives each engine's real
  `main()` (forcing that engine's driver symbol to `None`), not just
  `run()` directly — see `CHANGELOG.md`'s "Fixed" entry for the same
  date. This caught a real `asyncio.get_event_loop()` crash in
  `puppeteer_scraper.py`'s `main()` that every prior check missed, since
  none of them called `main()` at all. Confirmed on Roman's own machine,
  not just the sandbox: `smoke_test.py` passes 68/68 over 5 consecutive
  runs there (same as the sandbox), the credential scanner
  (`.github/ci_checks.py`) passes, and the working tree is clean with no
  stray files. Two direct, real (not mocked) CLI runs on that machine the
  same day: `selenium_scraper.py --query "test"` with selenium genuinely
  not installed there correctly reports "selenium is not installed"
  (exit 1) rather than short-circuiting — run twice, once before this
  sync and once after, same result both times; and `puppeteer_scraper.py
  --query "test"`, with pyppeteer actually installed and a real
  `SHEIN_CDP_ENDPOINT` from `.env`, got past the asyncio setup this fix
  touches and into an actual CDP connection attempt (which then hung on
  the network-egress block this file's intro already documents, not on
  the asyncio bug — confirmed by the absence of the
  `RuntimeError: There is no current event loop` traceback that occurred
  reliably before the fix). Playwright and puppeteer's own "driver
  absent" path still could not be live-tested the same way as selenium's,
  because both are actually installed on Roman's machine — the new
  `smoke_test.py` check is what covers those two engines instead, by
  forcing the driver symbol to `None` in-process rather than needing it
  genuinely uninstalled.
