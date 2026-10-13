# Group 346 - the items group 345 left open (log review of the 13 Oct post-344 log)

Verification status: every changed file passes `py_compile`, and the repo checks `check_shared_copies.py` and
`check_merge_conflicts.py` pass. pytest, sqlalchemy, httpx, fastapi and feedparser cannot be installed in the sandbox
(PyPI is blocked), so **the pytest suites were NOT run**. The logic of each change was exercised with stand-alone scripts
that stub the missing modules (kv write-behind, AngelOne async writer, dead-primary feed skip, NSE endpoint backoff); all
passed. Run the api-gateway, market-data, analysis-intelligence suites before deploying.

| # | Issue | Change |
|---|-------|--------|
| 13 | api-gateway event loop blocked 3.6-3.9 s in an Oracle commit | `api-gateway/kv_cache.py`: `kv_set`, `kv_delete` and `kv_set_many` are plain sync functions that many `async def` handlers call directly, so each durable key write did an Oracle round trip + commit on the loop thread. When the caller is the event-loop thread the Oracle part is now queued to one writer thread (`kv-write-behind`, latest value per key wins); memory is still written first and is what readers hit. Worker-thread callers still write inline. `main.py` flushes the queue at shutdown (`flush_pending`). `KV_WRITE_BEHIND=0` restores inline writes. **Reads are unchanged**: a durable-key memory miss read from a coroutine still does an Oracle query on the loop; if `loop_watchdog` still logs a stall, its stack line will name that caller. |
| 12 | AngelOne poll cycle 30-40 s against a 15 s target | `market-data/angelone_ws_feed.py`: a batch no longer waits for its `live_quotes` write (Oracle executemany of up to 50 rows) before the next batch starts. The in-memory `_LIVE` cache is already current at that point; the durable copy goes to one background writer, latest row per symbol, <= 50 rows per transaction. `feed_status()["db_async"]` shows queued / written / coalesced / pending. `ANGELONE_FEED_DB_ASYNC=0` restores the awaited write. Whether this is enough depends on how much of the 30 s was DB time versus AngelOne latency: check `last_cycle_s` in the next log. |
| 16 | NSE endpoints answering 404 / 503 asked again every recompute | `api-gateway/main.py`: the existing pause covered 401/403/429 only. One endpoint that answers 404 / 5xx or fails at transport level is now left alone for `NSE_ENDPOINT_FAIL_PAUSE_S` (900 s), doubling per further failure up to `NSE_ENDPOINT_FAIL_MAX_S` (3600 s); a 200 clears it; other endpoints are unaffected; 0 disables. Empty-row 200 answers were already cached for the board's TTL. |
| 17 | Retired / frozen news feeds re-downloaded every cache expiry | `analysis-intelligence/news/feed_fetch.py`: once a fallback has served for a primary that answered 404/410, was all-stale, or served HTML, the primary is not tried again for `NEWS_FEED_PRIMARY_DEAD_TTL_SEC` (1800 s; 0 disables) and the fallback is used straight away. Timeouts, 5xx and bot-gate statuses never mark a feed dead. |
| 19 | Internet scanners hit the frontend | `frontend/nginx.conf`: `/metrics`, `/actuator`, `/server-status`, `/phpinfo`, `/wp-*`, `/cgi-bin` and similar return 404, and any `.html` file other than `index.html` is a clean 404 instead of the SPA page. **Not validated with `nginx -t`** (no nginx in the sandbox): run `docker compose build frontend` and `curl -i localhost:8080/metrics` once. |

Shared-copy baseline: `kv_cache.py` now differs between api-gateway and the other services on purpose (only the gateway
calls it from coroutines). `scripts/shared_copies_baseline.json` was updated; the diff only adds `kv_cache::` entries.

Still not changed, and why:
- #2 RAMRAT exit fill: only Dhan knows the fill. `GET /reconcile/pending` (position-stocks) lists it; once you have the
  price from Dhan, `POST /reconcile/pending/resolve` closes the marker. The automatic sweep already gives up after
  `PENDING_RECONCILE_MAX_AGE_DAYS` (3).
- #7 Hot Picks warm-up: group 344 stamps snapshots, so the fallback works from the next restart.
- #14 `scan/universe` momentum movers not ready within 5 s at boot: by design the call returns without them and the
  computation warms the cache for the next call. No change.
- #15 BIRET / HEG / HDFIRE / UNIVAFOODS have no AngelOne token: the scrip-master lookup already falls back from -EQ to
  -BE and -BZ, so these names are simply not in AngelOne's file. Yahoo reports BIRET and HEG as possibly delisted. Check
  whether they are still listed (or renamed) on NSE, and remove or alias them in the universe.
- #18 GMRAIRPORT entry held back by the 10 s price-age limit: the guard re-reads once on the priority lane and then
  leaves the candidate queued. Working as designed.
- #5 the 20 s Surprise scan overrun: bulk prefetch time, unchanged.
