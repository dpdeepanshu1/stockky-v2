# E179 - market-data-service CI: two group193 tests failed only when CI ran in the 09:05-09:15 IST pre-open

Includes E178.

## The failure
`tests/test_group193_bulk_stale_cache.py::TestBulkCore::test_fresh_rows_served_stale_rows_sent_for_refresh` and
`::test_stale_row_returned_when_every_source_fails` (2 failed, 1731 passed). The CI run was at 09:09 IST on a trading Monday.

## Cause (a test problem, not a service bug)
In the pre-open, `/quotes/bulk` answers a symbol that the live feeds and AngelOne could not price from the last close (group244,
`_quote_preopen()` -> `_closed_last_close_row`). The group193 harness has no live price for the stale symbol, so that stage served the stale
row as a "last close": it came back inside `quotes` (first test: `['FRESH', 'OLD']`), so the last-resort `stale_served` path never ran
(second test: no `stale_served` key). `tests/conftest.py` already pins `_quote_market_closed` to False (group233) so tests do not depend on the
wall clock, but it did not pin `_quote_preopen`, so those tests passed at any other time of day.

## Change
`tests/conftest.py`: new autouse fixture `_g244_preopen_off_by_default` pins `_quote_preopen` to False for every test, the same way group233 is
pinned. group244's own tests turn it on explicitly, and its flag tests call `monkeypatch.undo()` to reach the real function, so they are unaffected.
No service code changed.

## Not run
My sandbox has no pip access, so I could not run the suite here; the file compiles. CI at any time of day should now give the same result.
Run: `bash scripts/docker_test.sh market-data-service`
