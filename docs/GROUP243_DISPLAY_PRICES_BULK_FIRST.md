# Group 243 - dashboard prices (Candidates / Positions / Orders tabs) come from one bulk call, not two calls per symbol

From the 2026-10-08 open log (about 09:20 IST):
- After real-trade's `POST /quotes/bulk` priced the 3 held symbols, market-data logged a run of `GET /live-quote/<SYM>`
  then `GET /quote/<SYM>` for about 50 symbols (FOCUS, BELRISE, AARTIPHARM ...), right after
  `GET /candidates/REAL?limit=40` from the frontend. Straight after, 20+ lines of
  `AngelOne-first did not price X (lane budget shed this call)` sent those symbols on to Yahoo.
- Cause: `get_display_prices()` (the shared price column for the three Real Trade tabs) still priced every cache miss
  with `get_quote()` per symbol (live-quote, then quote). Groups 152/171 gave the trading paths bulk-first;
  the display path was never changed.

## real-trade-service (market_feed/feed.py)
- `get_display_prices` now sends the whole miss list (8 symbols or more) through ONE chunked `POST /quotes/bulk`
  first. Freshness limit: `FEED_DISPLAY_BULK_MAX_AGE_S` (default 60 s) while the market is open; while it is
  closed the existing `FEED_PREVIEW_BULK_MAX_AGE_S` (a last close is fine for a display column).
- Symbols bulk could not price go through the per-symbol path, but without the `/live-quote` call (bulk had just read
  the same feeds), and at most `FEED_DISPLAY_LEFTOVER_MAX` (default 10) per poll. Symbols not attempted because of
  the cap are NOT cached as "no price": they are tried again on the next poll.
- If every bulk chunk failed or bulk raised, nothing is capped or skipped: the old full per-symbol path runs.
- `_bulk_ticks` gets `schedule_atr=True` (default, trading callers unchanged). The display path passes False so a
  price column never starts ATR `/history` refreshes (AngelOne candle limit).
- Batches under `FEED_DISPLAY_BULK_MIN_SYMBOLS` (default 8) keep the old path. `FEED_DISPLAY_BULK=0` turns it all off.
- Trading code (entry, exit, priority lane, `get_quotes`) is not touched. The display prices are still never used
  for sizing or order decisions.

## Not changed
- The 3 held symbols (MPHASIS, JSWCEMENT, UNITDSPR) still get `/live-quote` + `/quote` every ~8 s after a bulk
  timeout; that is the group 171/225 priority-lane fallback and is a separate item.
- Remaining items 2-7 from the 2026-10-08 list (pre-open bulk -> yfinance, candle 403 on SYRMA/PNB, Moneycontrol
  fallback staleness, surprise-scan timeout, AngelOne poll cycle 22 s, movers 12 s) are not part of this group.

## Tests
New `tests/test_group243_display_prices_bulk_first.py` (11): 40 symbols -> one bulk call and no per-symbol call, no ATR
scheduling, open/closed age limits, leftovers skip live-quote and are capped, uncapped leftovers retried next poll,
TTL hit makes no calls, small batch keeps old path, all-chunks-failed falls back uncapped, bulk exception swallowed,
switch-off, unpriceable symbol remembered briefly. Ran with real pytest: those 11 plus the existing
`test_feed_display_prices.py` pass (21). Full real-trade-service suite: 3571 passed; the 4 `test_oracle_compat`
failures are `oracledb` not installed in this sandbox, and 1 teardown error in
`test_group172_volume_shock_history_reasons.py` also occurs on the unmodified group 242 upload.
