# group302 (2026-10-10) - gateway stall hardening, DB pill, audit timeouts, closed-market Surprise, Data Feed status, log flood

Cumulative on group301. Rebuild: `docker compose up -d --build api-gateway market-data-service frontend`
(api-gateway CPU limit changes, so `up -d` must recreate the container).

## 1. Gateway stall after `docker compose down` / `up` (root cause NOT reproduced)
Made less likely and diagnosable:
- `loop_watchdog.py`: a heartbeat task plus a daemon thread. When the event loop has not ticked for `GATEWAY_LOOP_STALL_WARN_S`
  (3 s) it logs the stack of the loop thread (the exact blocking line), at most once per `GATEWAY_LOOP_STALL_LOG_EVERY_S` (30 s).
  `GET /ops/loop-lag` shows current lag, worst stall, stall count, last stack. `GATEWAY_LOOP_WATCHDOG=0` turns it off.
- Default worker pool for `asyncio.to_thread` raised to `GATEWAY_THREAD_POOL` (default 32, 0 = leave the default).
- Boot warms run in sequence: momentum movers -> scan universe first (after `GATEWAY_BOOT_WARM_DELAY_S` 8 s, or
  `GATEWAY_BOOT_WARM_CLOSED_DELAY_S` 90 s when closed/holiday), then the Surprise warm (waits at most
  `GATEWAY_BOOT_WARM_SURPRISE_WAIT_S` 120 s). Blocking reads in the startup hooks and in `/surprise/scan/stream`
  (`load_static_cache`) now run in a worker thread.
- docker-compose: api-gateway `cpus` 0.45 -> 0.70 (total cap comment 2.55 -> 2.80). Revert if you do not want it.

## 2. DB pill
`/ops/wake-db-all`: gateway ping runs in a worker thread, each target has a `DB_WAKE_TIMEOUT_S` (20 s) budget and one retry.
`ok` = any target answered; also `all_ok`, `partial`. Browser waits 50 s and retries once after 6 s before showing red.

## 3. Audit timeouts
Four audits (Surprise, Hot Picks x2 endpoints, IPO, feed) make one attempt: 20 s (feed audit 30 s), then show the error and a
"Retry Audit" button. A failed Surprise audit no longer overwrites the last good numbers with "health 0".

## 4. Surprise while the market is closed
`GET /surprise/scan` returns the last saved result (flags `market_closed`, `from_cache`, `cache_age_sec`) when the phase is
closed/holiday. `refresh=true`, `force_reload=true` or explicit `symbols` still run a real scan; nothing saved -> live scan.
`SURPRISE_CLOSED_SERVE_LAST=0` restores the old behaviour. The tab asks `/market/session` first and shows a banner;
"Refresh Scan" still runs a real scan.

## 5/6. Data Feed status
Boot heal no longer stamps "Last success"; elapsed/ETA are 0 unless a job is running; `stock_count` and `last_count` follow the
feed count; `partial` clears when the run finished; a boot-heal "Last success" is repaired once from the job's finish time.
`DATA_FEED_STATUS_NORMALIZE=0` returns the raw job/meta.

## 7. market-data log flood
Per-symbol "Bhavcopy EOD waterfall hit" lines are DEBUG; one INFO summary per `BHAVCOPY_HIT_SUMMARY_EVERY_S` (60 s).
`BHAVCOPY_HIT_LOG_PER_SYMBOL=1` restores group235 behaviour.

## Tests
New: api-gateway `tests/test_group302_loop_watchdog.py` (8, run), `tests/test_group302_gateway_closed_serve_feed_status.py`;
market-data `tests/test_group302_bhavcopy_hit_summary.py`. Updated: `test_main_ops_routes.py` (wake-db-all),
market-data `test_group235_closed_quote_day_change.py` (two per-symbol log tests).
