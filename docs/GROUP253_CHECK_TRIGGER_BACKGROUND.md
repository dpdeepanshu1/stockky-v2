# Group 253 - the dynamic universe's /check warm-up no longer holds the cycle (2026-10-08 open-market log)

## What the log showed
`dynamic_universe: added 60 symbols: [...]` and then, about 90 s later, `dynamic_universe: /check trigger failed (ReadTimeout) - Tier 2 cache
may be stale`. Meanwhile analysis-intelligence was fetching events symbol by symbol (`=== Fetching fresh events for ...` for AAVAS, ABB, ABREL,
ACC ... one after another).

## Why
`GET /check` on the event tracker is a synchronous walk over EVERY subscription with a 1 s sleep between symbols plus a fresh news/event fetch
per uncached symbol. With a 60-symbol auto universe (plus user subscriptions) that takes minutes, so the 90 s client timeout expired on every
sync. Two costs:
- the dynamic-universe -> watchlist stage of the cycle (`_dynamic_universe_and_watchlist` in cycle_runner.py) sat on that wait for the full
  90 s, once every 20 minutes, for a warm-up whose answer nothing reads;
- the log said "failed" although the event service kept working through the list after the client had hung up.

## real-trade-service `watchlist_engine/dynamic_universe.py`
- After the subscribe/unsubscribe calls, `/check` is started on its own **daemon thread** and `refresh_dynamic_universe` returns at once.
  A thread rather than an asyncio task because cycles run inside throw-away event loops on worker threads (`asyncio.run` in
  `main.py` / `auto_pilot._run_coro_in_new_loop`), which would cancel a background task as soon as the cycle ended.
- One at a time: a sync that finds the previous `/check` still running logs `/check from an earlier sync is still running - not starting
  another` and starts nothing.
- The thread's timeout is `DYNAMIC_UNIVERSE_CHECK_TIMEOUT_S` (default 600, blank/invalid/<=0 = 600). Its real outcome is logged:
  `/check finished in 142s - event cache warmed` or `/check trigger failed (ReadTimeout) after 600s - Tier 2 cache may be stale`.
- `DYNAMIC_UNIVERSE_CHECK_BACKGROUND=0` restores the old wait-for-it behaviour (90 s, same log text as before).
- `check_status()` returns `{running, started, finished, ok, elapsed_s, skipped_busy}`.

## Limits
- Tier 2 now sees newly subscribed symbols' events when the thread finishes, not at the end of the same cycle. In practice that is no
  change: the 90 s wait timed out on every sync in the log, so those symbols were never warm at the end of the cycle either.
- The event service itself is unchanged: `/check` is still serial with a 1 s stagger, so it still takes minutes for a big universe.
  (Possible later step, not done: skip the 1 s sleep after a cache hit.)
- Not confirmed live. After the next open: no `/check trigger failed ... after 90s` lines; one `/check started in the background` per sync,
  followed later by `/check finished in Ns`.

## Tests
New `tests/test_group253_check_background.py` (24): env parsing; refresh returns without waiting for a gated slow check; URL and timeout used;
second sync while one runs starts nothing; a new one can start after the last finished; failure logged with type + elapsed and the flag cleared;
success logged; a failed check does not block the next; a thread that cannot start clears the flag and the sync still returns its result; flag
off keeps the old async path. `tests/test_watchlist_dynamic_universe.py` gets an autouse fixture pinning the old inline path for its existing
tests. Real pytest, real-trade-service: 3724 passed; the one `test_group172` teardown error is also on the unmodified upload.
