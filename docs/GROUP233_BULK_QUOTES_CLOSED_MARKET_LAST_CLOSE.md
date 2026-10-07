# Group 233 - bulk quotes instead of per-symbol calls, last close when the market is closed

Cause (evening boot log): hundreds of `GET /quote/{sym}` calls from the gateway (news / hot-picks / trending /
hot-picks repair) and real-trade-service (after-hours news-scan symbol validation), each one walking AngelOne-first
("lane budget shed this call") and then Yahoo, for prices that cannot change while the market is closed.

## market-data-service (main.py)
- New helpers `_quote_market_closed()` (outside the live-feed window: weekends, NSE holidays, before ~09:05, after
  ~15:35 IST), `_closed_last_close_row()`.
- `/quote/{symbol}` (when closed, not for index symbols): after the cache check it answers from the cached row of any
  age -> the 30-day last-good fallback row (if <= QUOTE_CLOSED_LAST_CLOSE_MAX_AGE_H, default 96 h) -> the NSE bhavcopy
  close -> an older fallback row; only a symbol with no close known anywhere continues into the normal waterfall.
  Source tags: `last_close_cache`, `last_close_fallback`, `bhavcopy_eod`. Rows keep their original `fetched_at`.
- `/quotes/bulk` (when closed): cached rows of any age are served (no group193 stale refresh), the rest answered the
  same way; AngelOne REST and Yahoo are only reached for symbols with no close known.
- Env: QUOTE_CLOSED_SERVE_LAST_CLOSE (default on, 0 = off), QUOTE_CLOSED_LAST_CLOSE_MAX_AGE_H (96).

## api-gateway
- `_fetch_prices_bulk_async(symbols, client, *, bulk_min=None, per_symbol_fallback=True)`: new keyword args.
  Hot Picks price pass 2 now calls it with `bulk_min=1, per_symbol_fallback=False` (one POST /quotes/bulk even for
  <15 missing symbols, never GET /quote per symbol).
- New `_gw_quotes_closed()` (session phase closed/holiday). When closed, `_fetch_prices_bulk_first` accepts last-close
  rows up to GATEWAY_BULK_QUOTE_CLOSED_MAX_AGE_S (7 d), reuses them GATEWAY_BULK_QUOTE_CLOSED_CACHE_S (600 s), and
  `_fetch_prices_bulk_async` skips the per-symbol leftover after a bulk pass. GATEWAY_BULK_QUOTE_CLOSED_AWARE=0 = off.
- `/market/trending`: one POST /quotes/bulk for its <=10 names instead of one GET /quote each (yfinance fallback kept).
- `hotpicks_store.hotpicks_repair_batch`: chunked POST /quotes/bulk (50) instead of GET /quote per target; a symbol bulk
  cannot price is skipped this run (stays "missing price", retried next repair).

## real-trade-service (market_feed/feed.py)
- `get_preview_quotes` (after-hours scan `_validate_symbols`, dashboard preview): chunked POST /quotes/bulk first (any
  age), then GET /last-close/{sym} (cache / bhavcopy only), then GET /quote/{sym} for at most
  FEED_PREVIEW_QUOTE_FALLBACK_MAX (5, 0 = never). New `_get_preview_last_close`. Env: FEED_PREVIEW_BULK_MAX_AGE_S (7 d).

## Tests
- New: market-data `test_group233_closed_market_last_close.py`, api-gateway `test_group233_closed_market_and_bulk_only.py`,
  real-trade `test_group233_preview_bulk_first.py`.
- Updated: market-data and api-gateway `conftest.py` pin "market open" by default (autouse), `test_main_hot_stocks.py`,
  `test_hotpicks_store.py` (fake client gets `.post`), `test_main_market_universe_routes.py` (trending uses bulk POST),
  real-trade `test_feed_fanout_controls.py` (fallback cap raised for the GET-only fake).

## What to look for after deploy (market closed)
`AngelOne-first did not price X` lines from 172.18.0.7 / gateway should drop to ~0 after the boot; the log line
`gateway prices: N symbol(s) left unpriced after bulk` is expected for names with no close anywhere.
Rebuild market-data-service, api-gateway, real-trade-service.
