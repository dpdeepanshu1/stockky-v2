# group301 (2026-10-10) - Position Stocks scalper can read its ticks from market-data

Cumulative on group300 (which includes 299 and 298).
Rebuild: `docker compose up -d --build position-stocks-service`. Default behaviour is unchanged.

## What
`POSITION_FEED_SOURCE=market_data` swaps the AngelOne SmartWebSocketV2 feed for a poller (`feed/md_poller.py`) that calls
market-data-service `POST /quotes/bulk` once per `POSITION_BULK_POLL_S` (1 s, Dhan's bulk limit). Each request = open positions
(`OPEN` / `EXIT_LEGS_REJECTED`) first, then the next `POSITION_BULK_CHUNK` (500) NSE-EQ symbols, round robin (~2,700 symbols
sweep in about 5-6 s). market-data picks the provider, so this service needs no AngelOne login for ticks.

`feed/ws_client.py`: the per-tick store (buffer append + time prune under the lock, day volume, day stats, depth, on-tick
callbacks) moved into `_ingest_tick()`; the WS loop calls it with identical arguments. `start()` picks the poller when selected;
`ws_status()` adds `source` and (for the poller) `md_poller` counters and keeps `connected` / `last_tick_at` / `reconnect_attempts`.

## Differences from the WebSocket (read before switching)
- A quote row is a snapshot, not a trade: stored only when `fetched_at` moved since the last stored row (no duplicate ticks), so a
  symbol's tick rate = how often market-data refreshes it. The tick-count volume proxy and 1m/5m windows see fewer ticks.
- Rows older than `POSITION_BULK_MAX_AGE_S` (30 s) are dropped; a future stamp is clamped to now.
- No bid/ask in rows: `get_best_bid_ask()` stays None, MAX_SPREAD_PCT stays fail-open. The entry depth gate reads market-data itself.
- No open price: day stats are `(None, high, low, prev_close)`.
- 3 failed polls in a row mark the feed `connected: false`; back-off 2/4/8/10 s, then recovers on the first good poll.
- Off-hours/holiday idling reuses `_offhours_idle()`.

## Env (all optional, commented in .env.example / .env.oracle.recommended)
`POSITION_FEED_SOURCE` (angelone_ws | market_data), `POSITION_BULK_POLL_S` (1.0, min 0.2), `POSITION_BULK_CHUNK` (500, 1-1000),
`POSITION_BULK_TIMEOUT_S` (8), `POSITION_BULK_MAX_AGE_S` (30). Blank/unknown source = angelone_ws.

## Tests
`tests/test_group301_md_poller.py` (64): config clamps, shared `_ingest_tick`, batching/round robin, row ingest (dedupe, stale, clock skew,
bad rows, volume, day stats, callbacks), fetch/poll_once, universe load, poll loop (success, failure back-off, recovery, idle, cancel),
`start()` routing, `ws_status()` shape.
Sandbox had no pytest/network: run via a stand-in runner, 64/64; 10 deliberate breakages of the poller were all caught. Existing
`test_ws_client*.py` give the same results as on the original code under that runner (caplog/delattr/sqlalchemy cases can't run
there). Run `bash run_tests.sh` on the VM.

## Turn on
Set `POSITION_FEED_SOURCE=market_data` in `.env`, rebuild position-stocks-service, then check `GET /ws-status`
(`source`, `connected`, `md_poller.ticks`, `stale_skipped`, `errors`). Wait for a clean Dhan market session first; revert by unsetting it.
