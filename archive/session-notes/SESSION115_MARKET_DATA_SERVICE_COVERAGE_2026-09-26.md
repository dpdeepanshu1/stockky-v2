# Session 115 — market-data-service: market_hours, circuit_breaker, rate_limiter (2026-09-26)

## Context

After session 113 (open-issues audit) and session 114 (feed.py remaining coverage),
all real-trade-service and position-stocks-service source gaps are closed.
Session 112 round 30 closed api-gateway/qstash_client.

Session 99 noted market-data-service has very thin coverage (3 pre-existing files +
session 98's 1). This session opens that work with three pure-logic modules that
carry zero external dependencies (no yfinance, no Redis, no SQLAlchemy).

## Files added

### `tests/test_market_hours.py` — 14 tests, 14/14 passed

Covers `market_hours.py` (64 lines) end-to-end:

- `is_feed_window_ist`: weekday in-hours ✓, Saturday ✓, Sunday ✓, before
  pre-open slack ✓, at boundary ✓, after post-close slack ✓, at boundary ✓,
  `_ALWAYS_ON` overrides weekend ✓, overrides outside hours ✓, `None` falls
  back to `datetime.now(utc)` ✓, UTC-aware datetime converted correctly ✓,
  Friday in-hours ✓.
- `seconds_until_next_window`: always returns 60.0 ✓, with explicit datetime ✓.

### `tests/test_circuit_breaker.py` — 43 tests, 43/43 passed

Covers `circuit_breaker.py` (403 lines) near-completely (Redis-backed
`_load_remote`/`_persist` paths untestable without Upstash):

- `_get_redis`: off by default, cached, `DISABLE_REDIS` wins, `DISABLE_UPSTASH`
  wins, `USE_REDIS=1` without URL returns None.
- `CircuitBreaker` state machine: starts closed, opens after threshold, the
  regression guard (`_opened_at` NOT re-stamped on subsequent failures after
  open), half-open after recovery_timeout, closes after `half_open_success`
  successes, half-open probe failure re-opens, success in closed resets count,
  `retry_after` zero/positive, `CircuitOpenError` fields, `call()` success /
  failure paths.
- `snapshot()`: all fields populated; `opened_at` is None when closed,
  non-None when open.
- Registry: `get_breaker` same-instance / different-name, `all_snapshots`.
- Helpers: `_looks_like_rate_limit` (429, rate limit, Too Many Requests, quota,
  throttled, generic error, empty string), `_provider_from_breaker` (all 8 cases).
- `record_rate_limit_hit`: writes events + stats via kv_cache stub, handles
  `kv_cache` import error, merges with prior stats, truncates to 500 events,
  swallows kv_set RuntimeError.
- Thread safety: 20 concurrent `record_failure` calls — `_opened_at` stamped
  exactly once (the 2026-09-01 regression guard confirmed).

**Bug confirmed caught by tests:** the `self._state != "open"` guard in
`record_failure` that prevents `_opened_at` being re-stamped by subsequent
failures. The `test_subsequent_failures_do_not_reset_opened_at` test fails
without that guard (removing it makes `_opened_at` keep advancing, which was
the original bug that caused circuits to stay open indefinitely).

### `tests/test_rate_limiter.py` — 42 tests, 42/42 passed

Covers `rate_limiter.py` (438 lines) near-completely (Redis coordination path
untestable without Upstash; `patch_yfinance`'s `Ticker.info` property-patch
path exercised indirectly):

- `_cfg`: defaults, RPS override, burst override, invalid env ignored, unknown provider.
- `_Bucket.acquire`: immediate return, token refill, throttle_events zero on no-wait,
  throttle_events incremented on real wait, `fail_open=True` proceeds after max_wait,
  `fail_open=False` returns -1.0, snapshot keys, waiters decremented.
- Public API: `acquire` float return, swallows bucket error; `try_acquire` True/False/
  swallows error; `would_block` False/True/swallows error; `stats` includes buckets.
- Cooldowns: not-in initially, in-cooldown after set, expired after TTL.
- `suggested_timeout`: no-waiters returns base, many-waiters widens, floor respected,
  swallows error.
- `_yf_call_with_hard_timeout`: success records, circuit-open raises RuntimeError,
  exception records failure and reraises, timeout raises TimeoutError and records
  failure, no-circuit-breaker-module still works.
- `_breaker_allows_call`: no-module True, closed True, open False + logs warning.
- `patch_yfinance`: yfinance not installed returns False, patches returns True,
  idempotent, patched download gates on open circuit breaker, patched history runs fn.

## No production code changed

Tests only. All 99 new tests pass in this sandbox against the real unmodified modules.

## Next by priority

Remaining market-data-service modules without tests:
1. `bhavcopy.py` (761 lines) — NSE bhavcopy fetch/parse
2. `kv_cache.py` (1143 lines) — Neon KV store
3. `surprise_premarket.py` (991 lines) — premarket surprise logic
4. `angelone_client.py` (450 lines) — AngelOne REST client
5. `oracle_compat.py` (283 lines) — Oracle/Neon compat helpers
6. `main.py` (3315 lines) — FastAPI routes (largest gap)
