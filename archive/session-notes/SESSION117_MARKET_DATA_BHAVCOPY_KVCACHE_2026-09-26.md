# Session 117 — market-data-service: bhavcopy + kv_cache (2026-09-26)

## Files added

### `tests/test_bhavcopy.py` — 70 tests, 70/70 passed

Covers `bhavcopy.py` (761 lines) comprehensively. No real NSE calls —
`_nse_client()` is monkeypatched in all network-dependent tests.

- `_nse_client`: cached within TTL, rebuilds on stale, `force_new=True`
  bypasses cache, bootstrap exception swallowed, old client closed on refresh.
- `_candidate_session_dates`: returns n weekdays, descending order.
- `_bhav_urls_for_date`: non-empty list, first URL is sec_bhavdata_full, date
  components (ddmmyyyy) present.
- `process_bhavcopy_rows`: EQ/BE kept, non-EQ dropped, price cap enforced,
  non-dict rows skipped, comma-formatted price, NA price, empty/None input.
- `_parse_bhav_csv_all`: EQ rows parsed, non-EQ skipped, delivery_pct
  extracted, close price, empty CSV, BOM stripped, delivery computed from
  qty, price cap, first EQ row wins over later BE dup.
- `_parse_bhav_csv` (single-symbol): find/miss/empty/non-EQ/price-cap/BOM/
  qty-fallback/close-only-row.
- `_fetch_bhav_day_parsed`: success, caching, all-404, empty content,
  interstitial HTML (200-status block page), JSON content-type, ZIP content,
  cache eviction at MAX_DATES.
- `delivery_from_quote`: delivery_pct field, qty computation, non-200,
  exception, .NS suffix stripped.
- `delivery_from_bhavcopy` / `eod_close_from_bhavcopy`: cache hit, symbol
  missing, exception, not-found log line.
- `delivery_from_nse_cm_series`: success, non-200, no rows, no delivery
  field, exception.
- `get_delivery`: waterfall (quote → bhavcopy → cm → neutral fallback),
  exception swallowed, .NS suffix normalised.
- `process_bhavcopy_dataframe`: list path, None passthrough, to_dict object.

**Side note:** `bhavcopy.py` uses `datetime.utcnow()` in `get_delivery`
(2 places). Same deprecation as `angelone_client.py`/`bhavcopy.py` — to fix
in a dedicated cleanup pass.

### `tests/test_kv_cache.py` — 73 tests, 73/73 passed

Covers `kv_cache.py` (1143 lines) end-to-end. All Neon/Postgres paths
tested against a real SQLite in-memory engine; Oracle dialect paths covered
via dialect-string stubs. No Redis/Upstash needed.

- `MemoryTTLCache`: get/set/delete, TTL expiry (epoch-injected), no-TTL
  persists, ttl() return values (-1/-2/positive), max_keys eviction (expired
  purge first, then LRU when no expired).
- `_normalize_db_url`: postgres→postgresql rewrite, channel_binding removed,
  sslmode=required→require, sslmode added when absent, not doubled.
- `_is_durable`: watchlist, notification_config, data_feed, feed prefix,
  random key, fundamentals prefix.
- `kv_get/kv_set/kv_delete` memory-only: set/get, missing, delete, TTL,
  module aliases (get/set/delete), cache_get/cache_set backcompat.
- `kv_get/kv_set/kv_delete` with SQLite Neon stub: durable key writes to DB,
  reads from DB on cold memory, non-durable key not persisted, delete removes
  from DB, expired row handled, non-JSON value returned as string.
- `kv_set_many/kv_get_many`: memory writes, empty noop, durable write to DB,
  memory hits, empty keys, durable from DB, fallback on bulk error, aliases.
- `kv_ttl`: no-TTL → -1, missing → -2.
- `status()`: memory-only, neon_connected True with SQLite, error captured.
- `hard_reset_stockky_kv` memory-only: clears store (including notification —
  memory-only path is _store.clear(), not the selective Neon-path wipe).
- `hard_reset_stockky_kv` with Neon: graceful handling (SQLite has no
  TRUNCATE), error path returns error status dict.
- `_settings_table_ok`: valid tables pass, invalid raises ValueError.
- `settings_get/set/delete` memory path: roundtrip, delete, missing.
- `settings_get/set/delete` with SQLite Neon: set/get from DB, delete from
  DB, no row → None, DB error → None/False.
- `notification_config_*`: get/set/delete roundtrip, legacy kv key migration,
  set mirrors to legacy kv key.
- `watchlist_*`: get/set/delete roundtrip, legacy migration, mirrors to kv.
- `_neon_url`: no env → None, CACHE_DATABASE_URL preferred, KV_DATABASE_URL
  second.
- `_get_redis`: None by default, cached after first call.
- `_dialect`: postgresql by default.

## Totals this session

143 new tests, all passing. 322 total across all market-data-service test
files in this session stack (sessions 115–117).

## Next by priority

1. `surprise_premarket.py` (991 lines)
2. `main.py` (3315 lines) — FastAPI routes, the largest remaining gap
