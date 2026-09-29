# Session 126 — analysis-intelligence-service: fundamental/indianapi_fallback.py coverage (2026-09-29)

Picked per the session-125 "Next" list: `fundamental/indianapi_fallback.py` was the lowest file at 93%
(missed 44-45, 146, 159-160, 199-201). Tests only — no production code touched.

## Added to `tests/test_indianapi_fallback.py`

| Class | Covers (lines) |
|---|---|
| `TestKvCacheImportFallback` | `import kv_cache` fails -> `_kv = None` (44-45). Re-executes the REAL source, compiled under its real filename so coverage counts it, with `sys.modules["kv_cache"] = None`; cache helpers then no-op |
| `TestEnforceRateLimitInProcessFallback` | rate_limiter missing -> in-process pacing really sleeps the remaining interval and stamps `_MEM_LAST_TS` (146); no sleep when interval already elapsed |
| `TestFetchTimeoutFallback` | `suggested_timeout()` raises -> default `REQUEST_TIMEOUT_SECONDS` kept (159-160); suggested value used when it works; params/headers asserted |
| `TestRedisClientGuard` | `_get_redis_client()` raises `RuntimeError` -> logged, returns None, IndianAPI never called (199-201); Yahoo success never reaches it; `_get_redis_client()` no-op |

## Verification status

pytest / pytest-cov / fastapi are NOT installed in this sandbox and there is no network. So:
- `py_compile` passes.
- All 35 tests in the file (26 existing + 9 new) were run for real through a minimal hand-written pytest stand-in
  (fixtures, monkeypatch, caplog) with a line tracer on the module: 35 passed, 0 failed, and all 8 target lines
  (44, 45, 146, 159, 160, 199, 200, 201) were executed.
- NOT run under real pytest / coverage. Please run on your VM (commands in the reply / below).

Check on the VM, from `services/analysis-intelligence-service`:

    python3 -m pytest tests/test_indianapi_fallback.py -q --cov=fundamental.indianapi_fallback --cov-report=term-missing
    ./run_tests.sh            # full table, per-file processes; expect fundamental/indianapi_fallback.py 100%
    ./run_tests.sh --single   # one process, uses tests/conftest.py

## Next

`event/event_depth.py` 97% (118-119, 167-168, 174-175), then `fundamental/peer_multi_quarter.py` 98% (173-175),
`fundamental/peers.py` 98% (74-75), `news/news_quality.py` 98% (158-159), then the `__main__` / import-fallback
tails (`fundamental/main.py` 8-9 & 659-661, `news/main.py` 11-12 & 657-659, `technical/main.py` 692-693 & 765-767,
`event/main.py` 430-431).
