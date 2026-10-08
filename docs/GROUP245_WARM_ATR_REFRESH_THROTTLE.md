# Group 245 - after a restart, DB-warm ATR refreshes are spread out instead of hitting AngelOne candles all at once

From the 2026-10-08 open log:
- 13 `GET /history/<SYM>?period=1mo&interval=1d` calls from real-trade (NESTLEIND, BELRISE, JSWINFRA, GVT&D, DABUR, DIXON,
  CYIENTDLM, ABB, GLAND, RKFORGE, GPIL, RENTOMOJO, CHOLAFIN) within about one second, then
  `AngelOne getCandleData returned HTTP 403 ... exceeding access rate`, a 30 s cooldown, a second trip a moment later
  (60 s cooldown) and two more `angelone_candle cooldown` lines.
- Correction to the earlier list: the 7 `/history` calls when SYRMA / PNB were opened are NOT the AngelOne problem.
  Group 231 already answers 5d/1mo/3mo/6mo daily from one cached 1y/1d fetch, and 60m / 1wk / 1mo intervals go to
  yfinance, so those 7 calls cost about one AngelOne candle call per symbol.
- The real source is real-trade's background ATR refresh (`_schedule_atr_refresh`). Its policy says a warm, recent ATR
  needs nothing, but "recent" is `_ATR_LAST_OK`, which is in memory only. After a restart the ATR cache is warm from the
  DB (1403 symbols in this log) and `_ATR_LAST_OK` is empty, so every symbol that a bulk/quote pass priced looked due
  and got a refresh (up to 8 in flight, continuously), each one an AngelOne 1y/1d candle call in market-data.

## real-trade-service (market_feed/feed.py)
- A symbol whose ATR came from the DB and which this process has not refreshed yet is refreshed at most
  `FEED_ATR_WARM_REFRESH_PER_MIN` times per minute process-wide (default 6; 0 = no limit, the old behaviour). Over a
  session that still refreshes ~360 symbols/hour, so the ATR cache stays current, without the burst.
- A symbol with no ATR at all is never limited (entries and exits need it). A symbol this process already refreshed keeps
  the 6 h TTL logic. A symbol turned away by the limit is not marked as tried, so the next pass can pick it up.
- No change to market-data-service, to the TTL, to `FEED_ATR_MAX_INFLIGHT`, or to the display path.

## Not changed / limits
- ATR values loaded from the DB can be days old (the snapshot has no timestamp); they are refreshed gradually at 6/min
  instead of at once, so the first hour after a restart uses a slightly older ATR for the not-yet-refreshed names.
- The other candle callers in market-data (volume-shock list, candidate checks, api-gateway, position-stocks) are not
  paced here. If candle 403s continue after this, the next step is pacing inside market-data's candle path.

## Tests
New `tests/test_group245_warm_atr_refresh_throttle.py` (6): DB-warm symbols limited to 6 per minute, no-ATR symbols
unlimited, window rolls after a minute, 0 = old behaviour, a symbol already refreshed by this process is not counted,
rejected symbols not marked as tried. Real pytest: full real-trade-service suite 3578 passed (the 4 `oracledb` tests
excluded here, they need that package; the one `test_group172` teardown error is also on the unmodified upload).
