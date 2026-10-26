# E171 - build error, stale-file test failure, oversell sync read

Includes E170.

## 1. Vercel build error (fixed)
`SurpriseStocks.tsx` used `surpriseRowView` (added in E161) but only imported `surpriseKpis`, so `tsc` stopped the build
with TS2304. The import now has both names. `src/surpriseView.test.mjs` also checks that the import is there, because the
node tests strip types and never saw this.

## 2. `npm test` failure "TabButton.tsx deleted" (your repo, not the zip)
`src/appHelpers.test.mjs` asserts that `components/app/TabButton.tsx` and `BackendStatusDot.tsx` are gone (E70 removed them).
Neither file is in the zip, and the whole frontend test chain passes on the zip. In CI the files still exist, because unpacking
a zip over a git checkout adds and changes files but never deletes. Remove them in git:

    git rm -f frontend/src/components/app/TabButton.tsx frontend/src/components/app/BackendStatusDot.tsx
    git status --short | head      # check nothing else deleted by E70 is still tracked

## 3. Next fix: oversell sync in `_send_real_sell` (exit_engine/exit.py)
Found while reading the sell path line by line. When Dhan says "sell more than you hold", the code reads the broker quantity to
correct `qty_open`, or to close the position as a ghost when the broker holds 0.
- It read **holdings only**. Holdings list settled shares (T+1). A share bought today is in the day's **positions**, so a
  same-day position read as 0 and was force-closed while still held. Now: holdings + open long positions (`netQty > 0`).
- The symbol match used `startswith`, so ABB could read ABBOTINDIA's quantity (first prefix hit won). Now exact, after
  upper-casing, removing spaces and a trailing `-EQ`; all matching rows are added.
- If the positions read fails, the existing "Qty sync failed" alert runs and the position is left alone (no guess of 0).

Tests: `tests/test_exit_error_branches.py` (+5; fixture stubs `get_positions`).

## Run here
- frontend: `tsc --noEmit` clean, `npm test` all suites ok, `vite build` ok.
- real-trade-service: 5,376 passed, 2 skipped. 4 `test_oracle_compat.py` tests fail only because `oracledb` is not installed in
  this sandbox (ModuleNotFoundError); your VM image has it.
- Not run: other services' suites (no change), a browser.
