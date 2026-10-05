# group153 (2026-10-05) - market-data overload: batched live_quotes writes, bulk-first gateway prices

Cumulative on group152. Rebuild: `docker compose build market-data-service api-gateway && docker compose up -d`.

Source: item 4 of the 2026-10-05 market-open VM log list ("market-data-service overloaded"). Group152 already removed most of real-trade-service's per-symbol load (bulk-first, priority lane). This group handles the two parts group152 left: the AngelOne feed and the api-gateway's per-symbol `/quote` fan-out.

## 1. AngelOne feed: live_quotes written one row per transaction (market-data-service, `angelone_ws_feed.py`)
**Cause.** `_poll_cycle` called `_on_tick_sync` for every row of every AngelOne batch, and `_on_tick_sync` ran `_upsert_tick_sync`, a separate `engine.begin()` + MERGE (Oracle) / INSERT .. ON CONFLICT (Postgres) per symbol. A 491-symbol universe is ~491 sequential DB round trips per cycle, on the polling thread. The AngelOne calls themselves were already paced (10 requests per cycle, sequential, 0.35 s apart, shared `angelone_quote` bucket 5 rps), so the poll rate was not the problem; the DB writes were. On a slow Oracle link they stretch a cycle past the 20 s freshness window real-trade-service applies to `live_quotes` rows (`LIVE_QUOTE_MAX_AGE_S`), so symbols polled early in the cycle looked stale and every position/candidate fell through to the slow per-symbol `/quote` route.
**Fix.**
- New `_upsert_ticks_batch_sync(rows)`: the rows of one AngelOne batch (<= 50) go in ONE transaction (`executemany`), same SQL and columns as before, run via `asyncio.to_thread` so the DB round trip does not block the poll loop. 491 writes per cycle become 10. Rows keep the same freshness (written every cycle), so real-trade-service reads are unaffected.
- `_on_tick_sync(tick, write_db=True)`: with `write_db=False` it only updates the in-memory cache and returns the row tuple. Default behaviour is unchanged for any other caller.
- `ANGELONE_FEED_DB_BATCH=0` restores the old per-row writes.
- New visibility: `feed_status()` now has `last_cycle_s`; a cycle longer than `ANGELONE_FEED_SLOW_CYCLE_WARN_S` (15 s) logs one WARNING (at most every 5 min) saying rows will go stale.
- Polling interval, batch size and `ANGELONE_BATCH_GAP_S` are NOT changed.

## 2. api-gateway: per-symbol `/quote` fan-out (`main.py`, `_fetch_prices_bulk_async`)
**Cause.** Hot-picks pass 2 and the scan chunks call `_fetch_prices_bulk_async`, which sent one `GET /quote/{sym}` per symbol (8 at a time), even though market-data serves AngelOne ticks in bulk; the same symbol could be in the list twice, and two overlapping callers asked for the same symbols again.
**Fix.**
- The list is de-duplicated by base symbol (`SYM`, `SYM.NS`, `sym` are one request).
- Lists of `GATEWAY_BULK_QUOTE_MIN` (15) or more are priced with chunked `POST /quotes/bulk` first (`GATEWAY_BULK_QUOTE_CHUNK` 50, 2 chunks in flight, `GATEWAY_BULK_QUOTE_TIMEOUT_S` 12). Only the symbols bulk could not price use the old per-symbol path; a failing bulk call falls back to the old path for everything.
- A bulk row is used only when its `fetched_at` parses and is <= `GATEWAY_BULK_QUOTE_MAX_AGE_S` (20 s) old; rows for symbols that were not requested are ignored.
- Bulk prices are kept `GATEWAY_BULK_QUOTE_CACHE_S` (8 s) so an overlapping caller reuses them (cache bounded at 5000 entries, expired ones purged).
- `GATEWAY_BULK_QUOTE_MIN=0` turns the bulk path off. All new env vars are optional.

## Tests
- `market-data-service/tests/test_angelone_feed_batch_writes.py` (9): one transaction for N rows (Postgres + Oracle SQL), rows without symbol/price skipped, no engine / DB failure do not raise, `write_db=False` returns the row and skips the DB, a 120-symbol cycle writes 3 batches (50+50+20) and no single rows, off switch restores per-row writes, slow-cycle warning is rate limited and `last_cycle_s` is recorded. 8 of 9 fail on the group152 code.
- `api-gateway/tests/test_group153_bulk_first_prices.py` (11): de-duplication, one bulk POST per 50 symbols and no `/quote` GETs, per-symbol only for the misses, stale / undated / unparsable rows ignored, bulk 500 / bad body / exception degrade to the old path, unrequested rows ignored, short cache reused by an overlapping caller, cache off / bulk off switches, bad env values, cache bound, unexpected helper error. All 11 error out on the group152 code.

## Not changed (judgement calls)
- The other gateway `/quote` callers are single-symbol by design (stock-detail 30 s refresh, the WS push of <= 12 watched symbols, alert checks) and were left alone; I could not see which callers produced the duplicate calls in the log, so this group fixes the one fan-out helper that can send the same symbols repeatedly.
- No change to AngelOne poll rate: it is already sequential and paced. If `last_cycle_s` / the slow-cycle warning still shows cycles > 15 s after this, the next knobs are `ANGELONE_BATCH_GAP_S` and narrowing the universe passed to the feed to the trade-critical set.

## Still open from the log list
5 (volume-shock ratio not time-of-day adjusted; history returns None), 8 (news sources returning 0), 9 (duplicate concurrent event fetches), 10 (`0/180` bulk log), 11 (EMBASSY.NS), 12-18.
