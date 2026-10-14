# group361 - Oracle latency probe + keep-warm, and the async-handler DB sweep (log item #2, and the open sweep from group359)

## 1. Sweep: sync DB calls made directly from async handlers (api-gateway/main.py)
An AST scan (async def -> sync function that reaches `eng.connect/begin/execute`, `_get_neon`, ... , calls inside `to_thread` /
`run_in_executor` / `Thread` arguments excluded) found 28 call sites. All now run through `asyncio.to_thread` (marked
`# group361: off the event loop`): `_prioritize_universe`, `get_all_stock_feeds`, `get_symbols_bulk`, `_load_watchlist`,
`_filter_symbols_under_max_price`, `list_feed_symbols_from_neon_under_max_price`, `_get_recent_ipos`, `run_ipo_scan`,
`add_manual_ipo_by_name`, `purge_non_equity_ipos`, `precalculate_surprise_baselines`, `add_price_alert` / `delete_price_alert`,
`find_prepare_to_buy_candidates`, 7x `store.delete_symbol`, `hotpicks_db_payload`. Re-scan: 0 left.
The scan is name-based and only sees calls it can resolve; it is a net, not a proof.
Test fix: `tests/test_main_ipo_premarket_routes.py` replaced `threading.Thread` with a stub that never runs, which also froze
asyncio's own worker/shutdown threads once a route used `to_thread`; the stub now keeps those real.

## 2. Oracle latency (`oracle_compat.py`, identical in all 8 copies)
No live latency numbers were available, so this adds measurement and one mitigation; it does not claim a fix.
- Probe: per engine, every cursor execute and every NEW physical connection is timed (count, avg, p50, p95, max, last slow
  statement). A throttled WARNING `oracle slow exec|connect N ms (engine=...)` fires at `ORACLE_SLOW_QUERY_MS` (1500; 0 = off).
  Statement text only (first 100 chars), never parameters.
- `GET /ops/db-latency` (api-gateway): the snapshot plus pool state (size / checked in / out / overflow) and keep-warm counters.
  Connect p95 high = handshake/auth cost (pool_recycle, idle drops). Exec p95 high = the DB or the network round trip.
- Keep-warm: one daemon thread, every `ORACLE_KEEPWARM_S` (60; 0 = off), checks out up to 5 connections that are ALREADY idle
  in each pool, one at a time, and runs `SELECT 1 FROM DUAL`. pool_pre_ping / pool_recycle therefore fire there, not in a request.
  It never opens a new session. A failed ping is counted (`keepwarm.failures`), not raised.
- `ORACLE_LATENCY_PROBE=0` disables both. Uses `event.listen` + weakrefs; a stub engine skips them silently.

## Switches (`.env.example`, `.env.oracle.recommended`)
`ORACLE_SLOW_QUERY_MS`, `ORACLE_KEEPWARM_S`, `ORACLE_LATENCY_PROBE`.

## Not changed
Sync DB calls in other services' handlers (real-trade-service commits in async defs are a separate open item).

## Tests
`tests/test_group361_oracle_latency_probe.py` (13, real sqlite engine). api-gateway suite run file by file: all pass except 3
files that already failed on the unmodified code in the sandbox: `test_group222_boot_warm_preopen` (3), `test_main_ws_loops_startup`
(10). `test_kv_cache::test_memory_only` was stale since group359 and is fixed. Other services' suites NOT run.
