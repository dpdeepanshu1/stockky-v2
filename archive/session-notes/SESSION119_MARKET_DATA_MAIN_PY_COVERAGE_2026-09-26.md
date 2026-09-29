# Session 119 — market-data-service: main.py coverage (2026-09-26)

## Files added

### `tests/test_main_helpers.py` — 132 tests, 132/132 passed

Covers every pure-logic helper in `main.py` (3315 lines) without live
network. Heavy deps stubbed at module top (yfinance, upstash_redis,
requests, circuit_breaker) before import.

| Class/Group | Functions covered |
|---|---|
| `_normalize_de_ratio` | None, non-float, NaN, normal, >50 non-financial ÷100, financial <200 kept, >200 financial ÷100, zero |
| `_safe / _safe_int / _compute_growth` | NaN, Inf, string, None, negative, growth NaN/Inf |
| `_sanitize_for_json` | None, float NaN/Inf, dict recursed, list/tuple, non-string dict keys, datetime, date, numpy (float/int/bool/NaN/ndarray), pandas Timestamp/NaT/Series, string passthrough |
| `_redact_secrets` / `_SecretRedactingFilter` | apikey=, apiKey=, no key, non-string, exception fallback; filter redacts, returns True, leaves safe msg |
| `_install_httpx_secret_filter` | idempotent — second call doesn't duplicate filter |
| `_MemCache` | get/set/ttl, expired, no-ttl, -1/-2, eviction (expired first, then LRU) |
| `_in_cooldown / _set_cooldown` | not initially, set, expired, YF_COOLDOWN_UNTIL updated, non-yf name |
| `_cache_ttl / _should_soft_refresh / _cache_get / _cache_set` | miss, from mem, cooldown guard, TTL high/low, NaN sanitized; fallback_get/set |
| `is_known_delisted` | TATAMTRDVR/AAKASH true, RELIANCE false, .NS suffix, lowercase |
| `sanitize_symbol` | smart map, PB FINTECH, removes LIMITED, URL decode, strips .NS, empty |
| `normalize_symbol` | equity→.NS, caret passthrough, NIFTY50→^NSEI, NIFTY 50, BANKNIFTY, SENSEX, smart map rename, empty, .NS input |
| `_clean_quote_dict / _pad_quote_response` | drops Nones, empty, None input; minimal pad, with data, data price wins |
| `_yahoo_tickers_for` | empty, equity→[.NS,.BO], caret, NIFTY50, BANKNIFTY, smart map, .NS not doubled |
| `_is_rate_limit_error` | all 6 patterns, generic error, empty |
| `_waterfall_equity_base` | caret→"", equity, smart map, .NS stripped, empty |
| `_angelone_interval` | 1d, 1h, 1wk→None, 5m→None |
| `_history_flight_enter / _history_flight_exit` | creates entry, two enters share entry/ref-count, exit decrements+removes, held releases lock, not-held keeps lock, 5 concurrent threads |
| `get_cache_ttl` | 300 when market open, 21600 when closed |
| `is_market_open` | returns bool, Saturday weekday check |

### `tests/test_main_routes.py` — 30 tests, 30/30 passed

FastAPI TestClient covering all routes not requiring live upstream data.

| Route | Tests |
|---|---|
| `GET /` | 200, version in body |
| `GET /health` | ok status, timestamp |
| `GET /angelone/network-check` | 200 + all fields, static IP env used vs auto-detect |
| `GET /live-quote/{symbol}` | miss when no DB, .NS strip, ltp from DB, DB exception→miss |
| `GET /internal/yahoo-ws-status` | connected:False when module None, live status from module |
| `GET /bhavcopy/universe` | symbols returned, min_price filter, empty when no data |
| `GET /delivery/{symbol}` | delivery_pct, cache on second call, neutral fallback |
| `GET /delivery/{symbol}/refresh` | bypasses cache, force-fetches |
| `GET /surprise/premarket/status` | idle initially |
| `POST /surprise/premarket` | background accepted, already_running, background=false inline, symbols param parsed |
| `GET /surprise/premarket` | delegates to POST handler |
| `GET /surprise/static` | no_database_url error, DB failure, rows from real SQLite |
| `_report_rate_limit` | circuit_breaker branch, gateway branch (requests.post), no GW URL skips, exception swallowed |

**Three bugs found during authoring:**
1. `requests` stub at module top lacked `.post` — `m.requests.post` raised AttributeError; fixed by adding `_stub.post = lambda *a, **kw: None` plus a guard for pre-imported modules.
2. `/live-quote/{symbol}` does `from kv_cache import _get_neon` inside the function body, so patching `kv_cache._get_neon` correctly injects the engine.
3. `/surprise/static` calls `sqlalchemy.create_engine(url, ...)` internally — SQLite file-URL works; in-memory engine monkeypatched via `sqlalchemy.create_engine` doesn't survive the fresh import inside the route.

## Session totals

162 new tests, all passing.
**Grand total across all market-data-service test files: 546 passing.**

## Overall market-data-service coverage achieved

| File | Tests added | Status |
|---|---|---|
| `market_hours.py` | 14 | Session 115 |
| `circuit_breaker.py` | 43 | Session 115 |
| `rate_limiter.py` | 42 | Session 115 |
| `oracle_compat.py` | 37 | Session 116 |
| `angelone_client.py` | 43 | Session 116 |
| `bhavcopy.py` | 70 | Session 117 |
| `kv_cache.py` | 73 | Session 117 |
| `surprise_premarket.py` | 62 | Session 118 |
| `main.py` helpers | 132 | Session 119 |
| `main.py` routes | 30 | Session 119 |
| **Total** | **546** | |

## Next service

`analysis-intelligence-service` — no tests yet on any of its sub-modules
(news/, fundamental/, technical/, sentiment/, event/).
