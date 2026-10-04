# group118 (2026-10-04) - AAKASH / ANNAPURNA 404s from paths group99 did not cover

Cumulative on group117. Run the api-gateway and market-data-service suites on the VM.

## What changed
- `api-gateway/surprise_scanner.py`: new `_is_dead_symbol()`; `load_static_cache()` (table rows) and `_seed_from_data_feed_kv()` skip confirmed-delisted symbols. If `symbol_aliases` cannot be imported nothing is skipped.
- `market-data-service/main.py`: `/quotes/bulk` drops delisted symbols before any lookup; `/history/{symbol}` returns the same fast 404 as `/quote`; `/fundamentals/{symbol}` returns the existing "unavailable" shape with `error: "delisted"`.
- Tests: 5 added to `api-gateway/tests/test_surprise_scanner.py`, new `market-data-service/tests/test_delisted_fast_paths.py` (10).

## Not changed
`/live-quote` (DB read only). The two KNOWN_DELISTED lists are still kept in sync by `test_known_delisted_drift.py`.
