# Group 278 - Phase A: data layer (Dhan -> AngelOne -> yfinance through market-data-service)

Order of use stays what market-data-service already does: Dhan first; if Dhan is paused, not configured or returns nothing, AngelOne; then yfinance and the NSE / last-close answers. Nothing is switched off (ANGELONE_WS_FEED_ENABLED and YAHOO_WS_FEED_ENABLED stay on).

## Done
1. **api-gateway `/market/indices`** read ^NSEI and ^BSESN from yfinance directly. It now reads market-data `/history/{index}` first (`_IndexTicker`, `_md_index_frame`; `INDICES_VIA_MARKET_DATA`, default 1); yfinance is only asked when market-data has nothing. Open-based change, the regime score and the group 221 previous-close block still come from the same frames and are unchanged.
2. **api-gateway** last-resort direct yfinance in `/market/trending`, `/api/quote` proxy and the websocket quote helper is behind `GATEWAY_DIRECT_YFINANCE_FALLBACK` (default 1 = unchanged, 0 = never).
3. **analysis-intelligence `technical/main.py`** already asked market-data first; its direct yfinance step is behind `ANALYSIS_DIRECT_YFINANCE_FALLBACK` (default 1, 0 = never).
4. **market-data `GET /internal/data-sources`** (`data_sources.py`): order, state and last error of Dhan / AngelOne / yfinance for quotes and for history, and which one is serving now.
5. **real-trade entry guard** refuses a tick older than `WATCHLIST_MAX_TICK_AGE_S` (default 30 s, 0 = off); the message names the source. A last-good copy keeps its original `as_of`, so it is caught.

Tests: market-data `tests/test_group278_data_sources.py`, api-gateway `tests/test_group278_indices_via_market_data.py` (+ conftest guard so older index tests keep their yfinance stub), analysis `tests/test_group278_direct_yf_switch.py`, real-trade `tests/test_group277_depth_guard.py` (age cases).

## Not done
- api-gateway `main.py`: movers per-symbol leftovers, hot-picks seed, `/api/surprise` and the surprise / IPO / premarket scanners still call yfinance for what market-data bulk could not price.
- decision-prediction training stays on yfinance until the split / bonus adjusted-price check is done (`TRAINING_DATA_VIA_MARKET_DATA=0`).
- `source` / `as_of` refusal is only in the real-trade watchlist entry; the position-stocks scalper uses its own AngelOne feed.
