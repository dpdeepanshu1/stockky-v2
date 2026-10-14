# group359 - bounded durable reads on the api-gateway event loop (log review item #3)

Source: the 14 Oct 10:38 IST restart log, item 3 (High): "Event loop blocked for 3.2s in api-gateway. A sync `oracledb` rollback,
closing a connection over SSL, runs on the async loop thread."

## What was found
group346 moved durable key WRITES issued from the loop thread to a write-behind writer. The READS were left: `kv_get`,
`kv_get_stale`, `kv_get_many` and `kv_cache.status()` are plain sync functions that `async def` handlers call directly. On a memory
miss they ran `eng.connect()` + SELECT + the pool's rollback-on-return (a network round trip over SSL) on the loop thread. A slow
Oracle, or a wait for a pool connection (`CACHE_DB_POOL_TIMEOUT`), froze every route (including `/health`) for as long as it took.

The full stack was not in the log (only the summary line), so the calling route is not identified. The guard fixes the whole class
(any read from any handler), and the watchdog now names the caller next time.

## Change (api-gateway only)
`kv_cache.py`:
- New `_guarded_read(token, fn, default)`. On the event-loop thread the durable read runs on a small worker pool (`kv-loop-read`,
  4 threads) and the loop waits at most `KV_LOOP_READ_TIMEOUT_S` (default 1.5 s). On timeout the caller gets the same "not found"
  answer a failed read already produced (`None` / `{}` / `(None, None)`); the worker keeps going and WARMS the memory cache when it
  finishes, so the next call is a memory hit. The rollback / connection return now happens on the worker.
- One in-flight read per key (later callers join it). Past `KV_LOOP_READ_MAX_INFLIGHT` (64) further reads are shed as misses.
- Wired into `kv_get`, `kv_get_stale` (DB part moved to `_stale_db_read`), `kv_get_many` (DB part moved to `_many_db_read`) and the
  `status()` probe. Callers on worker threads (`asyncio.to_thread`, background threads) are unchanged and read inline.
- `status()` gains a `loop_read_guard` block (enabled, inflight, guarded, joined, timeouts, shed).
`loop_watchdog.py`: the stall warning keeps the last 8 frames and adds `called from our code: file:line in function()` (innermost frame
that is not site-packages / stdlib / `kv_cache` / `oracle_compat`).

## Switches (`.env.example` and `.env.oracle.recommended`)
`KV_LOOP_READ_GUARD` (1; 0 = old inline read), `KV_LOOP_READ_TIMEOUT_S` (1.5, min 0.1), `KV_LOOP_READ_WORKERS` (4),
`KV_LOOP_READ_MAX_INFLIGHT` (64). Blank = default.

## Trade-off
While Oracle is slow, a cold key read from a handler now returns "not found" after 1.5 s instead of after 3-8 s. Callers already
treated a failed read that way (debug log, None), so the same fallbacks run (stale-serve, rebuild), sooner and without freezing the
gateway. The background read still completes and fills memory.

## Shared-copy baseline
`scripts/shared_copies_baseline.json` updated (147 known differences): api-gateway's `kv_cache.py` now differs from the other five
copies by the read guard, as it already did for write-behind. The other copies are unchanged.

## Tests
`tests/test_group359_kv_loop_read_guard.py` (14): inline off the loop, worker thread on the loop, slow read bounded + cache warmed,
join in-flight, shed cap, switch off, blank / bad / below-floor env, `kv_get_stale`, `kv_get_many`, `status()` probe, watchdog line.

## Not changed
- Sync Oracle calls made directly from handlers outside `kv_cache` (group356 / group303 already moved hot-picks, surprise, feed-store).
  An AST sweep for the rest is a separate item.
- Item 2 (Oracle latency itself). Next by priority: item 10 (stale-price gate on queued symbols).
