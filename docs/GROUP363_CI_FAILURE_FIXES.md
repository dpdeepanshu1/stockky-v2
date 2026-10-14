# Group 363 - CI failure fixes (after the g362 merge)

CI on the merged branch showed 4 failing jobs. Each was reproduced locally with real pytest, then fixed. Cause in brackets.

## analysis-intelligence-service (2 tests)
- `test_group341_feed_warning_throttle::test_warns_again_after_the_interval` - `news/feed_fetch._warn_throttled` used `_warned_at.get(key, 0.0)`, so with a small clock value (the test patches `time.time` to 1000) the FIRST warning for a key was throttled. Now the first warning for a key always goes out (`last is None or ...`). [real, harmless production fix]
- `test_news_main::test_one_warning_carries_hf_message_and_model` - stale test: group341 made a 400 "not supported by any provider" back off 6 h (21600 s), the test still expected the 1800 s default. Test updated.

## market-data-service (3 tests)
- `test_group274_dhan_env_gaps` (x2) - `DHAN_POLLER_ALWAYS_SYMBOLS` (read in `dhan_data/live_poller.py`, g345) was missing from `.env.example` and `.env.oracle.recommended`. Documented in both.
- `test_group344_live_path::test_failed_rows_are_not_remembered_as_sent` - the simulated Oracle timeout starts the intended write back-off, so the retry wrote priority symbols only. The test now clears the back-off before the retry.

## real-trade-service / position-stocks-service (3 tests in real-trade, boolean filter also in position-stocks)
- `test_oracle_boolean_filters` - `structure_observation._pending` (g356, both services) filtered `is_complete.is_(False)`, which renders `IS 0` on Oracle (ORA-00908). Now `== False  # noqa: E712`, the repo's existing idiom (see `orders/breakeven.py`). [real production fix: the evaluator's pending query would have failed on Oracle]
- `test_group164 ... test_no_flapping_across_cycles` and `test_group344_log_review::test_in_band_row_is_never_parked` - stale tests: g345 added a 300 s post-queue park (`WATCHLIST_REQUEUE_COOLDOWN_S`), so a just-queued row is not re-evaluated on the next call. Both tests set the cooldown to 0 (they test other behaviour). Both also failed on the untouched g361 zip.

## api-gateway (7 tests)
- `test_main_ws_loops_startup::TestCollectJobSnapshots` - g360 added `structure_refresh` to `_collect_job_snapshots`; the test fixture did not know it. Fixture and expectations updated (7 sources, one failing source leaves 6).

## Verified locally with real pytest
api-gateway 8795 passed, real-trade-service 4542 passed / 1 skipped, position-stocks-service 3721 passed, analysis-intelligence-service 2512 passed; the changed market-data tests pass (the rest of that suite needs `pyotp` and other packages not installed here). This is also the first run of the g361/g362 tests under real pytest instead of the stand-in runner.
