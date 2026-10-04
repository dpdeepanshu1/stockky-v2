# group130 (2026-10-04) - after-hours scan: no duplicate line when nothing was written

Cumulative on group129. Run `bash run_tests.sh` in real-trade-service on the VM.

## What the post-deploy boot log showed
Group 121's split line was printed immediately before group 85's explanation line, saying the same thing twice:
`... 10 symbol(s) scored -> 0 new/updated row(s), 10 already stored ... 0 failed`
`... 0 rows written - all 10 scored symbol(s) already stored for 2026-10-05 at an equal or higher score (nothing new to write)`

## Change (`real-trade-service/watchlist_engine/afterhours_scan.py`)
The split line (`N scored -> W new/updated, U already stored, F failed`) is now logged only when at least one row was written. When nothing was written, the existing `0 rows written - <why>` line (which already states the already-stored/failed counts) and the funnel line are the record. No change to scoring, writes or Telegram.

## Tests
`tests/test_afterhours_zero_row_explained.py`: the failure-count test now expects the `0 rows written - every one of 1 upsert(s) FAILED` line and no split line; new `test_no_duplicate_summary_when_everything_already_stored`. The real function was run against stubs here for both cases (write: split line present; all-stored: only the zero-row line); the test file compiles, not run under pytest (no pytest/sqlalchemy in the sandbox).
