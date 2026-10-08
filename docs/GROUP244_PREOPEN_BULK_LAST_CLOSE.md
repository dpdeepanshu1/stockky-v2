# Group 244 - pre-open /quotes/bulk serves the last close for symbols AngelOne could not price, instead of yfinance

From the 2026-10-08 open log:
- One bulk call: `AngelOne REST resolved 50/88 symbols, 38 left for yfinance`, then `yfinance call exceeded 18s hard
  timeout`, an ERROR traceback and a 502 reported to the rate-limit monitor. The 38 symbols ended up on the per-symbol path.
- Whether that call was before or after the 09:15 bell cannot be told from the log alone (the gateway logged
  `phase=preopen` earlier, real-trade logged 09:20 later). Both cases are covered below.

## market-data-service
- `market_hours.is_preopen_ist()`: trading day, from the start of the feed window (09:05 with the default slack)
  until 09:15 IST. Never true on weekends, NSE holidays, or with `MARKET_HOURS_FEED_ALWAYS_ON`.
- `main._quote_preopen()`: that check plus `QUOTE_CLOSED_SERVE_LAST_CLOSE` on and `QUOTE_PREOPEN_SERVE_LAST_CLOSE`
  not 0 (new switch, default on).
- `/quotes/bulk` (`_get_quotes_bulk_core`): after the live caches and the AngelOne REST pass, when pre-open, the
  symbols still unpriced are answered by the group 233 helper `_closed_last_close_row` (cache row of any age, 30-day
  fallback row, bhavcopy close) and skip yf.download. Rows keep their original `fetched_at`, so each caller's own
  freshness limit (real-trade, surprise scan, display) still sees the real age. A symbol with no close known anywhere
  (new listing) and index symbols (`^...`) still go to yfinance. One info line: `quotes/bulk: pre-open - N symbol(s)
  AngelOne could not price served from the last close, M left for yfinance`.
- Group 233's closed-market behaviour is unchanged (the 09:05-09:15 slack still counts as open there; AngelOne and the
  live caches are still asked first in pre-open).

## Not changed
- From 09:15 on, a bulk call with AngelOne misses still goes to yfinance when the bucket allows it. If the 50/88
  case was in fact after the open (AngelOne "lane budget shed"), this group does not cover it; that needs the
  lane-budget/rate-pressure items (candle 403, poll cycle 22 s) looked at first.
- Single `/quote/{symbol}` is unchanged in pre-open (it already tries AngelOne REST first).

## Tests
New `tests/test_group244_preopen_bulk_last_close.py` (11): cache/fallback/bhavcopy served with no yfinance call, a
symbol with no close still reaches yfinance, original fetched_at kept, not-pre-open keeps the old path, index symbols left
for yfinance, the two switches, window edges, weekend/holiday, always-on. Real pytest: those plus groups 193/233 pass;
full market-data-service suite 1184 passed.
