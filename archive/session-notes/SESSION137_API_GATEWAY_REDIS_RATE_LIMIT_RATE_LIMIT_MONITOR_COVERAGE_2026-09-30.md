# Session 137 — api-gateway coverage pass 5: redis_rate_limit.py + rate_limit_monitor.py (2026-09-30)

- New: `services/api-gateway/tests/test_redis_rate_limit.py` (71 tests) and
  `tests/test_rate_limit_monitor.py` (121 tests). No production code changed.
- `redis_rate_limit.py` is a process-local token-bucket limiter (Redis is accepted by `set_redis` but never
  used). main.py's `_cb_get()` uses `limiter.allow()` / `wait_budget_sec()` to pace internal fan-out.
  Covered: `_cfg` env overrides (incl. invalid values), bucket acquire / allow / wait_budget / snapshot,
  the 2 s sleep cap, `max_wait` escape hatch, zero-rps polling, waiter accounting (also on exception),
  module-level fail-open wrappers, `suggested_timeout` scaling, `stats()`, and the `LocalMemoryRateLimiter`
  wrapper (regression for the old "no attribute 'allow'" crash). One real-thread test checks that 40
  concurrent `allow()` calls on a 10-token bucket grant exactly 10.
- `rate_limit_monitor.py`: `_init_redis` env matrix (DISABLE_REDIS / DISABLE_UPSTASH / USE_REDIS /
  missing credentials / ping or constructor failure / missing package), kv helpers, Neon hydration (list
  and dict forms, ordering, junk items), `_persist_neon` aggregation window, `record()` normalisation and
  truncation, background persistence (Redis failure never blocks the Neon write), `_all_events` (bytes /
  str / dict / junk items, Redis error and empty fallbacks), `snapshot()` upstream and overall thresholds,
  circuits, Neon merge, and a record -> restart -> hydrate round trip.
- Hermetic: fake clock, synchronous fake for the `_io_pool` thread pool, fake `kv_cache` and
  `upstash_redis` modules, no network, no database, no real sleeping (only the 40-thread test and one
  50 ms real-clock acquire use real time). `rate_limit_monitor` builds a singleton at import time that calls
  `kv_get()`, so the test module imports it once with a fake `kv_cache` and the DB/Redis env removed:
  collecting the file cannot reach a real database even if `DATABASE_URL` is set in the shell.
- Verification (real pytest 9.1 + pytest-cov in the build sandbox this time, not the stand-in runner):
  `redis_rate_limit.py` 100% (140/140), `rate_limit_monitor.py` 100% (196/196); the whole api-gateway suite
  is 1006 passed both with `bash run_tests.sh --single` and in the default per-file mode. 12 hand-made
  mutants (threshold off-by-ones, window boundary, ordering, ltrim bound, sleep cap, zero-rps branch, ...)
  were all caught by the new tests.
- Observations, left unchanged:
  * `Bucket.acquire(weight > capacity)` can never succeed: it sleeps until `max_wait` (60 s default) and then
    proceeds anyway. Nothing in the repo passes a weight above 1 to this limiter (`_cb_get` uses the default),
    so it is latent; pinned by `test_weight_larger_than_capacity_stalls_until_max_wait`.
  * `RateLimitMonitor.snapshot()` still does a blocking `kv_get()` (and, with Redis, a blocking `lrange`)
    inline. `GET /ops/rate-limits` is an `async def`, so each dashboard poll blocks the event loop for one DB
    round trip. `record()` was already moved to a thread pool for this reason; `snapshot()` was not.
  * In `snapshot()` the Neon aggregate can never change the count of a known upstream: the local counts are
    merged last and always overwrite it, so the Neon merge only surfaces sources outside `UPSTREAMS`. The
    `if not recent and neon_by: pass` block is dead code. After a restart the dashboard is still non-zero
    because the events themselves are re-hydrated from Neon.
  * `record()` raises `ValueError` for a non-numeric `status`; the only caller (`/ops/rate-limits/event`)
    already coerces with `int(...)` inside a try/except and returns 400.
- Next (pass 6): `batch_worker.py`.
