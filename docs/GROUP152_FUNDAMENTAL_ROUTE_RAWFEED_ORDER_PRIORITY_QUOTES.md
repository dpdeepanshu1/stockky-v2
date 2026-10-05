# group152 (2026-10-05) - fundamentals route restored, raw-feed route order, priority quotes for open positions

Cumulative on group151. Rebuild: `docker compose build analysis-intelligence-service real-trade-service && docker compose up -d`.

Source: the 2026-10-05 market-open VM log (09:14-09:19 IST). Items below use the numbering of the issue list given for that log.

## Item 3 - `market_cap fetch failed ... ('str' object has no attribute 'get')` (analysis-intelligence-service)
**Cause.** group112 added the helper `_md_fundamentals_symbol()` directly under the `@app.get("/analyze/{symbol}")` decorator in `fundamental/main.py`. The decorator therefore bound to the helper, and `analyze()` lost its route. `GET /fundamental/analyze/LALPATHLAB` returned HTTP 200 with the plain JSON string `"LALPATHLAB.NS"`. Every caller that does `.get(...)` on the answer broke: real-trade-service's market-cap floor (14 symbols in the log), and anything else reading fundamentals from this route (decision-prediction included).
**Fix.** Decorator moved onto `analyze()`; the helper is a plain function again. A scan of all service modules found no other route decorator on an underscore helper.

## Items 6 + 7 - `RAW-FEED.NS` / empty Tier 2 (analysis-intelligence-service)
**Cause.** In `event/main.py`, `GET /events/{symbol}` was registered before `GET /events/raw-feed`, so `raw-feed` was handled as a symbol ("Fetching fresh events for RAW-FEED.NS") and returned a per-symbol event dict with no `items`. real-trade-service watchlist Tier 2 therefore always saw an empty feed ("Tier 2 (event-service) empty/unavailable") and fell to Tier 3.
**Fix.** `raw_feed` is registered above `/events/{symbol}`. A comment says why the order matters.

## Items 1 + 2 - open-position quotes timing out; 923-symbol batch giving up (real-trade-service, `market_feed/feed.py`)
**Cause.** Every 8 s exit cycle priced the 5 REAL positions with `/live-quote` (3 s timeout) then `/quote` (8 s timeout) over a client shared with the rest of the process, while a ~923-symbol batch sent one or two requests per symbol (32 in flight, 45 s deadline: 763 symbols never attempted). market-data-service answered 200, but later than the timeouts, so the position symbols got no tick and exit evaluation logged "No current price available this cycle".
**Fix.**
- `get_quotes(symbols, priority=True)`: own connection pool (never queued behind a batch), timeouts x`FEED_PRIORITY_TIMEOUT_SCALE` (default 2.0), then a `POST /quotes/bulk` fallback for any symbol the per-symbol path could not price. Used for exit evaluation (`exit_engine/exit.py`), the overnight-hold selection and the DEMO close-all path (`execution/auto_pilot.py`) and the manual DEMO close (`main.py`).
- Default batches above `FEED_BULK_MIN_SYMBOLS` (25) are priced with chunked `/quotes/bulk` first (`FEED_BULK_CHUNK_SIZE` 100, `FEED_BULK_CONCURRENCY` 3, `FEED_BULK_TIMEOUT_S` 12); only symbols bulk could not price go through the old per-symbol path. A failing bulk call falls back to the old path for everything.
- Staleness guard: a bulk quote is used only when its `fetched_at` is within `FEED_BULK_MAX_AGE_S` (20 s) and parseable; otherwise the symbol goes per-symbol. The `LIVE_QUOTE_MAX_AGE_S` rule for single-symbol ticks is unchanged.
- `get_quote()` gained `timeout_scale` (default 1.0, no change for existing callers).
- A WARNING line `priority quotes: per-symbol path failed for N/M symbol(s) ... /quotes/bulk recovered K` shows when the lane had to fall back.
All new env vars are optional; defaults are above.

## Tests
- `real-trade-service/tests/test_feed_priority_bulk.py` (12): bulk-first chunking, small batch unchanged, bulk misses fall back per symbol, stale bulk rows ignored, bulk 500 degrades, bulk row validation, priority lane happy path / timeout recovery through bulk / both down / symbol spelling kept, exit evaluation requests the priority lane.
- `analysis-intelligence-service/tests/test_group152_route_registration.py` (5, source-level): analyze route bound to `analyze`, helper not a route, no underscore helper is a route, raw-feed registered before the catch-all. All 5 fail on the group151 code.
- Existing fakes of `get_quotes` in 5 real-trade test files now accept `**_kw`, because exit, auto-pilot and the manual close pass `priority=True`.

## Not changed (still open from the log list)
4 (market-data-service overload: the bulk-first change removes most of the per-symbol load from real-trade, the api-gateway duplicate `/quote` calls and the 491-symbol AngelOne polling rate are untouched), 5 (volume-shock ratio not time-of-day adjusted; history returns None), 8 (news sources returning 0), 9 (duplicate concurrent event fetches), 10 (`0/180` bulk log), 11 (EMBASSY.NS), 12-18.
