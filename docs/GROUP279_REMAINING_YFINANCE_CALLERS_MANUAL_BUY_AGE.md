# Group 279 - Phase A leftovers: gateway yfinance callers, scalper manual-BUY age check

Order of use is unchanged: market-data-service decides Dhan -> AngelOne -> yfinance. This group moves the api-gateway's remaining direct yfinance callers behind it, and makes the direct call a switchable last resort everywhere in the gateway.

## Done (api-gateway)
`yf_policy.py` (new, imports nothing heavy) holds `direct_yf_ok()` (`GATEWAY_DIRECT_YFINANCE_FALLBACK`, default on) and `md_daily_frame()` (daily OHLCV from market-data `/history`). `main._direct_yf_ok` is the same switch.

| Caller | Before | Now |
|---|---|---|
| Surprise feed (`run_market_aware_surprise_feed`) | chunked `yf.download` first, market-data per symbol for misses | market-data `POST /quotes/bulk` first (`SURPRISE_FEED_VIA_MARKET_DATA`, rows older than `SURPRISE_FEED_MAX_QUOTE_AGE_SEC` skipped while the market is open); `yf.download` only for what it could not price and only if direct yfinance is allowed; then the per-symbol `/quote` as before |
| Premarket baselines | bhavcopy, then yfinance bulk, then per-symbol yfinance | bhavcopy, then market-data `/history` 1y (`PREMARKET_BASELINES_VIA_MARKET_DATA`, `PREMARKET_MD_WORKERS`, `PREMARKET_MD_BUDGET_S`), then yfinance bulk / per-symbol; both yfinance steps decline when direct yfinance is off. Result gains `source_market_data` |
| Movers leftovers (`_get_nifty50_data`) | per-symbol `yf.Ticker` for symbols the bulk quote could not price | same, but skipped when direct yfinance is off |
| IPO history (`ipo_scanner._fetch_history`) | market-data, then direct yfinance | direct yfinance step skipped when off |
| Repair RSI (`_patch_single_stock_feed`) | direct `yf.Ticker(...).history("1mo")` | market-data `/history` first (`REPAIR_RSI_VIA_MARKET_DATA`), yfinance only if allowed, then the technical service as before |

Found while doing this: the **hot-picks / momentum-movers seed** (`_get_momentum_movers` step 4) already goes only through market-data `/quotes/bulk` (`bulk_yahoo_download_prices`); it has no direct yfinance call. The 1m-history leftover and repair-RSI were the real direct callers in that area.

## Done (position-stocks)
The automatic entry already refuses a stale tick (`_price_guard_reject`, `ENTRY_MAX_TICK_AGE_S`, 45 s). The **manual BUY** (`POST /positions/manual/buy` -> `attempt_manual_entry`) took `ws_client.get_last_ltp()` with no age and had no such check. It now refuses with `STALE_TICK:...` before anything is claimed (`_stale_tick_reject`). Fails open on a missing buffer or error, like the other gates; 0 = off.

## Not done
- decision-prediction training stays on yfinance until the split / bonus adjusted-price check is done (`TRAINING_DATA_VIA_MARKET_DATA=0`). Needs market data to check.
- Scalper exits and the position-stocks feed: no age check on the exit side (phase C).

## Notes
- Premarket via market-data needs market-data `MAX_HISTORY_PERIOD` at `1y` (the default) for a true 52-week high.
- With `GATEWAY_DIRECT_YFINANCE_FALLBACK=0`, symbols market-data cannot price are left out of the movers and the surprise feed rather than fetched from Yahoo, and premarket symbols with no market-data history count as errors.
- Older gateway tests keep their yfinance stubs: `tests/conftest.py` turns the three new via-market-data reads off by default; the group279 tests turn them on.
- `test_group275_movers_via_market_data.py::test_get_nifty50_data_only_sends_the_unpriced_symbols_to_yfinance` depended on the wall clock (it failed after 15:30 IST in the original zip); it now pins the session phase.

Tests: api-gateway `tests/test_group279_remaining_yfinance_callers.py`, `tests/test_main_patch_single_stock_feed.py` (group279 cases), position-stocks `tests/test_entry.py::TestManualBuyStaleTick`.
