# group137 (2026-10-04) - an early `cached=true` scan call could delete the saved last result (api-gateway)

Cumulative on group136. Application code changed in api-gateway only: rebuild api-gateway.

## Problem
Group 132 made the closed-market boot warm read the saved surprise/scan result with the stale-aware loader first, because
kv_cache's plain read DELETES an expired row. But `SurpriseStockEngine.scan(cached=True)` still did its own cold-process
load with the PLAIN loader. Callers such as real-trade-service or the after-hours scan can hit `/surprise/scan?cached=true`
within seconds of an api-gateway restart, before the boot warm's delayed read (default 20 s). On a closed-market restart
that early call found the expired row, deleted it, and the warm then found nothing and ran the full ~1000-quote sweep.
Your last two reports (A3) show the row is still present only because no early caller happened to arrive.

## Change (`api-gateway/surprise_scanner.py`)
The cold-process load inside `scan(cached=True)` now uses `_load_last_result_stale_from_durable_cache`. Freshness is
unchanged: the age check right below it still serves a result only when it is <= `cached_max_age_sec` old, so a stale row is
loaded but never served as fresh (it falls through to the normal live scan exactly as before). The plain loader stays for
other callers.

## Tests (`tests/test_surprise_last_result_expiry_real_kv.py`, +2)
- scan(cached=True) on a cold engine calls the stale loader and never the plain one.
- Real kv_cache + SQL table: an early scan(cached=True) leaves the expired row in the table (both tests fail on the old line).
api-gateway full suite here: 8196 passed (was 8194).

## After deploying
    docker compose up -d --build api-gateway
No behaviour change you should see on a normal day; it only protects the saved row from early callers after a closed-market restart.
