#!/usr/bin/env python3
"""Judge one canary run: exit code plus the sidecar. Kept out of the
workflow so it can be run (and tested) locally — a workflow's inline
assertion block is otherwise code that nothing executes before CI.

    canary_assert.py OUT_PATH EXIT_CODE [--refusal-expected]

Passes only for exit 0/6 with a sidecar holding at least MIN_ROWS rows and
at least MIN_SHARE of them priced and titled. With --refusal-expected
(the datacentre job), exit 3 is a notice instead of a failure; nothing
else is ever excused.
"""
import json
import sys
from pathlib import Path

MIN_ROWS = 10
MIN_SHARE = 0.9


def judge(out_path: str, code: int, refusal_expected: bool = False) -> int:
    print(f"playwright_scraper.py exited {code}")
    if code == 3 and refusal_expected:
        print("::notice::exit 3 — SHEIN refused this datacentre runner (its /risk gateway). "
              "Expected here; canary-live is the job that tests real data.")
        return 0
    if code not in (0, 6):
        reasons = {1: "a crash", 2: "a usage error", 3: "blocked by SHEIN", 4: "zero products on a listing that always has them",
                   5: "content never obtained, or SHEIN stated products and none parsed"}
        print(f"::error::exit {code}: {reasons.get(code, 'unrecognised exit code')}. See the uploaded debug HTML.")
        return 1
    meta_path = Path(out_path + ".meta.json")
    if not meta_path.exists():
        print(f"::error::exit {code} but no sidecar at {meta_path}")
        return 1
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    rows = json.loads(Path(out_path).read_text(encoding="utf-8"))
    print(json.dumps(meta, indent=2))
    problems = []
    if len(rows) < MIN_ROWS or meta.get("product_count", 0) < MIN_ROWS:
        problems.append(f"{len(rows)} rows, want at least {MIN_ROWS}")
    if rows:
        priced = sum(1 for r in rows if r.get("price") is not None) / len(rows)
        titled = sum(1 for r in rows if r.get("title")) / len(rows)
        if priced < MIN_SHARE:
            problems.append(f"only {priced:.0%} of rows have a price")
        if titled < MIN_SHARE:
            problems.append(f"only {titled:.0%} of rows have a title")
    if problems:
        print("::error::" + "; ".join(problems))
        return 1
    print(f"canary OK: {len(rows)} rows, status={meta.get('status')}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(judge(sys.argv[1], int(sys.argv[2]), "--refusal-expected" in sys.argv[3:]))
