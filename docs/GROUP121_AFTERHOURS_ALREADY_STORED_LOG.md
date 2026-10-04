# group121 (2026-10-04) - after-hours "10 symbols scored -> 1 rows upserted"

Cumulative on group120. Run `bash run_tests.sh` in real-trade-service on the VM.

## What the log showed
`afterhours-scan [REAL 2026-10-05]: 10 symbol(s) scored -> 1 rows upserted`, right after a boot.

## Cause
Not lost symbols. The scan writes one row per (mode, symbol, market_date) and only writes a symbol that is new or has a higher score than the stored row. After a restart (or any repeat pass) the earlier pass's rows are still in the DB, so 9 of the 10 were "already stored at an equal or higher score" and 1 was new. The old summary line only printed the scored and written counts, so it looked as if 9 were dropped.

## What changed (`watchlist_engine/afterhours_scan.py::run_afterhours_scan`)
- Summary line now: `N symbol(s) scored -> W new/updated row(s), U already stored at an equal or higher score (unchanged), F failed`.
- Telegram header (only sent when something was written) adds `· U already stored` when U > 0.
- No change to what is written, scoring, or when Telegram is sent. The autopilot line `[afterhours] scan tick ... row(s) upserted` is unchanged and still accurate.

## Tests
`tests/test_afterhours_zero_row_explained.py::TestRunReportsAlreadyStoredSplit` (3 tests). Sandbox has no pytest/sqlalchemy: the real function was run against stubs and printed the expected line and Telegram header; the new test file compiles but is unrun here.
