# Session 124 — analysis-intelligence-service: rate_limit_report.py (2026-09-29)

## File changed

### `services/analysis-intelligence-service/tests/test_rate_limit_report.py`

Existing 19 tests always stubbed `kv_cache`, so only the happy path ran. Added 35 test functions (more once parametrized)
(appended; originals untouched) for the branches they could not reach:

| Class | Covers |
|---|---|
| `TestKvGetFallback` / `TestKvSetFallback` | first-attempt success; retry after inserting `fundamental/` on `sys.path` (and no duplicate insert when already there); both attempts failing -> `None` / silent + debug log; `kv_cache` un-importable (`sys.modules["kv_cache"] = None`); ttl forwarding |
| `TestRecordEdgeCases` | None/upper-case provider, path/symbol truncation, status int coercion, newest-first ordering, garbage / dict-without-list stored events, non-dict + stale + timestamp-less events kept but not counted, `unknown` source bucket, stats shape, prior-count coercion (`"7"`->7, bad/None skipped, fresh count wins), malformed prior stats, outer `except` -> warning log while gateway still notified |
| `TestGatewayPost` | URL (trailing `/` stripped), `timeout=2`, payload (detail cut to 200, path NOT cut — pinned), post failure swallowed, whitespace-only URL still posts (pinned) |
| `TestReportEdgeCases` | every message keyword -> 429, explicit status wins, 503 via `response.status_code`, falsy / non-numeric / raising / None `response`, detail cut to 200, str / empty / 0 inputs |

## Verification status

NOT run: sandbox has no pytest/requests and no network. Files compile (`py_compile`) and
each test was hand-traced against `rate_limit_report.py`. Confirm with:

    python3 -m pytest tests/test_rate_limit_report.py -q -p no:cacheprovider --cov=rate_limit_report --cov-report=term-missing

Expected: `rate_limit_report.py` at 100%.

## Next

Run `./run_tests.sh` for the real combined table and take the lowest-coverage file. Static
candidates with a name not referenced in any test: `fundamental/main._val`/`_empty_yahoo`,
`oracle_compat._int`/`_set_call_timeout`, `news_quality._rss_urls`/`_ts`,
`technical/main._get_return`, `event_depth._add`, `peer_multi_quarter._avg`, `peers._f`,
`indianapi_fallback._get_redis_client`.
