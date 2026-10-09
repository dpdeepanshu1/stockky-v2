# Group 272 - first sweep no longer throttled on a freshly booted host; two "known" test failures fixed

Two tests had been failing on every upload and were reported as "known" / "flaky". Both turned out to be real, deterministic,
and fixable.

## 1. position-stocks-service: `test_group210_symbol_lock_sweep::test_throttled_to_one_pass_per_interval` (a real bug)
`capital/shared_symbol_lock.py` kept `_last_sweep = [0.0]` ("never swept") and compared it with `time.monotonic()`. On Linux
`monotonic()` is the host's uptime, so `now - 0.0 < interval` was true whenever the host had been up for less than the
interval: the FIRST periodic stale-lock sweep was skipped. With the default 60 s interval that only matters in the first
minute after a host boot; the test uses 3600 s, so it failed on any machine up for under an hour (and passed on a long-running
one, which is why it looked flaky: "failed 3 times in one run").

Fix: the "never swept" value is now `float("-inf")` (also in `reset_sweep_throttle()`).

The same pattern was in `orders/reconcile.py` (`_last_stuck_sweep_ts = 0.0` compared with `time.monotonic()` against
`PENDING_RECONCILE_SWEEP_INTERVAL_S`, default 600 s): the first stuck-PENDING_RECONCILE sweep was skipped for the first 10 minutes
of host uptime. Same fix. Other module-level "last run" timestamps were checked: they either guard on truthiness
(`charges_ledger`, the auto-repair sweep, the cold poll) or are only used for log throttling, so they are not affected.

New tests: first sweep runs when `monotonic()` is 5 s (symbol-lock sweep, stuck-pending sweep), and the next call is still throttled.

## 2. real-trade-service: `test_group172_volume_shock_history_reasons::test_note_history_reason_never_raises_and_is_bounded` (test bug)
The test set `candidates._HIST_REASON` to `None` with a function-level `monkeypatch`. The module's autouse `_on` fixture also
uses `monkeypatch`, so that fixture is set up first and torn down LAST; its teardown calls `clear_history_state()` while
`_HIST_REASON` was still `None` and raised `AttributeError`. The source is fine (`_note_history_reason` swallows the error as
the test intends). Fix: the `None` patch now lives in `with monkeypatch.context()`, so it is undone before teardown.

## Result
position-stocks-service 2984 passed (was 2980 + 1 failure); real-trade-service 3848 passed, 1 skipped (was 3847 + 1 error).
No behaviour change apart from the two first-sweep fixes above.
