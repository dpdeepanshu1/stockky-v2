# Group 249 - bulk leftovers reuse their last good tick instead of 120 per-symbol calls (2026-10-08 11:03 IST log)

## What the log showed
With group 248 live, the 706-symbol watchlist poll lost 3 of 8 bulk chunks to ReadTimeout. The retry priced 168 of the 300 lost
symbols; the remaining 138 were "left for per-symbol lookups", capped at 120 (`FEED_LEFTOVER_MAX`). market-data's AngelOne
quote lane was in cooldown (trip #7, "angelone_quote cooldown") and the yfinance bucket was saturated, so those `GET /quote`
calls went to Yahoo and about 100 ended in `get_quote(...): source-2 ... failed: ReadTimeout`. Group 248 fixed the common case;
this is what is left when market-data is genuinely overloaded.

## real-trade-service
- `market_feed/feed.py`
  - New `_WL_LAST_GOOD` (bounded 3000, memory only): the last tick the non-priority batch path priced for each symbol, from bulk
    and from per-symbol answers. Stale-tagged ticks are never stored back, so a served tick can never look younger.
  - `get_quotes(..., allow_stale=False)` / `_get_quotes_unique(..., allow_stale=False)`: with `allow_stale=True`, symbols still
    unpriced after the bulk pass (and the group 248 retry) take their last good tick if it is at most `FEED_LEFTOVER_STALE_S`
    old by its own `as_of` (default 120 s; `FEED_LEFTOVER_STALE_S=0` switches it off). Source is `stale_last_good(<orig>)`.
    Only what is still missing goes to the per-symbol path, with the existing distress back-off and `FEED_LEFTOVER_MAX` cap.
  - Log: "N unpriced symbol(s) served from their last good tick (<= 120s old, watchlist trigger only); M left for per-symbol lookups".
  - `allow_stale` is passed to `_get_quotes_unique` only when true, so existing callers and test doubles are untouched.
- `entry_engine/entry.py`: the watchlist trigger (`get_quotes(symbols, allow_stale=True)`) is the only caller that opts in.
  It queues a candidate; the entry engine re-prices before any order. The ENTER path, `try_fill_entry`, exit evaluation,
  auto-pilot, portfolio, manual engine and the display price cache never see stale ticks (a test pins this).

## Limits
- A watchlist trigger decision can use a price up to 2 minutes old for a symbol bulk could not answer. The band/catalyst checks
  are percentages of 1-6 %, so this is small, but it is a deliberate trade for fewer timeouts.
- Right after a restart nothing is remembered, so the first poll behaves as before.
- Does not fix the cause (market-data saturated, AngelOne quote lane in cooldown, Yahoo blocked); it stops the watchlist
  poll adding to it.
- Not confirmed live. After the next open: the "served from their last good tick" line should appear when chunks fail, and
  `source-2 failed: ReadTimeout` lines should drop to a handful.

## Tests
New `tests/test_group249_leftover_last_good_tick.py` (8): opt-in caller served from last good with no per-symbol calls; default
caller unchanged; tick older than the limit not used; never-priced symbol goes per-symbol; switch-off; served tick keeps its
real age and is never re-stored; per-symbol results are remembered; only the watchlist trigger opts in. The 10 fakes of
`get_quotes` in `tests/test_watchlist_trigger.py` now accept `**_kw`.
Real pytest, real-trade-service: 3673 passed, 2 skipped; the one `test_group172` teardown error is also on the unmodified upload.
