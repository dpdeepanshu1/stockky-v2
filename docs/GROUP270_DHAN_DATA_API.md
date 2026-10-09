# Group 270 - Dhan Data API as the primary market-data source (Dhan -> AngelOne -> yfinance)

## What changed
Only market-data-service talks to Dhan for data. Everything else keeps calling market-data-service, so real-trade,
position-stocks, api-gateway and the analysis services inherit the new order with no change.

| Where | Change |
|---|---|
| `market-data-service/dhan_data/` (new) | config, creds (read-only), scrip_master, client (rate limit + errors), quotes (one-call-per-second batcher), history, live_poller, live_store, ws_feed (opt-in) |
| `market-data-service/main.py` | Dhan stage in `_get_quote_inner`, `/quotes/bulk`, `_get_history_impl`; startup hook; `GET /internal/dhan-status` |
| `market-data-service/kv_cache.py` | durable prefix `stockky:dhan_scrip` (scrip-master snapshot survives restarts) |
| `market-data-service/requirements.txt` | `cryptography>=42` (decrypt the stored Dhan token) |
| `analysis-intelligence-service/sentiment/main.py` | NIFTY/SENSEX and the NIFTY momentum/volatility bars come from market-data-service first, yfinance only for what it cannot give (`SENTIMENT_USE_MARKET_DATA=0` = old) |
| `decision-prediction-service/training/` and `prediction/` | optional `md_history.py`; `train.py` / `pred_train.py` use it only when `TRAINING_DATA_VIA_MARKET_DATA=1` (default 0) |
| `.env.example`, `.env.oracle.recommended`, `docker-compose.yml` | new Dhan variables; `DHAN_CREDENTIAL_ENC_KEY` passed to market-data-service |

Not changed on purpose: `sector_signal.py` (US sector ETFs, not on Dhan), `candidates.py` (already goes through
market-data-service), analysis `technical`, training `app.py`/`trades.py`/`evaluate.py` (already market-data-first),
`event`/`news`/`fundamental` (Dhan has no news or fundamentals).

## How it works
- **Provider order** is env: `QUOTE_PROVIDER_ORDER`, `HISTORY_PROVIDER_ORDER` (default `dhan,angelone,yfinance`).
  The Dhan stage sits first, after AngelOne, or after yfinance. Any Dhan failure returns nothing and the unchanged old code runs.
- **Quotes**: Dhan allows 1000 symbols per request but 1 request per second. All callers share one pending set; one worker
  sends one `/marketfeed/quote` call per interval. 60 concurrent single-symbol callers cost one upstream request.
  A row fresher than `DHAN_QUOTE_FRESH_S` (2 s) is served from memory.
- **Live quotes**: a poller (market hours only) queues the feed universe; the rows are upserted into `live_quotes` with
  `source='dhan'`, which is what real-trade-service reads as "Source 1". Written only when Dhan is the FIRST quote
  provider (`DHAN_LIVE_WRITE=auto`), so a shadow rollout never changes what trading reads.
- **History**: daily, weekly (built from daily) and hourly. A short daily period (5d/1mo/3mo/6mo) is cut from ONE cached
  1y Dhan fetch. Candle shape is identical to the AngelOne/yfinance paths. Indices use Dhan's `IDX_I` segment
  (ids come from the scrip master, never hard-coded).
- **Token**: read-only from the `trade_credentials` row real-trade-service owns. Needs the same `DHAN_CREDENTIAL_ENC_KEY`.
  Expired/missing/mismatched token pauses the Dhan stage (5 min when not configured, 60 s for auth errors), one WARNING per reason.
- **Failures**: auth/subscription -> pause; rate limit -> double the spacing for 30 s; no data -> fall through, no penalty;
  other errors -> circuit breaker (`DHAN_BREAKER_FAILS`, `DHAN_BREAKER_RECOVERY_S`).
- **Shadow comparison** (`DHAN_SHADOW_COMPARE=1`): Dhan vs AngelOne price for the same symbols, logged when >0.5% apart and
  summarised in `/internal/dhan-status` under `live_poller.shadow`.

## Rollout (recommended)
1. Dhan Data API subscription on; same `DHAN_CREDENTIAL_ENC_KEY`; a valid token saved in the app (or TOTP auto-refresh).
2. Deploy with `QUOTE_PROVIDER_ORDER=angelone,yfinance,dhan`, `HISTORY_PROVIDER_ORDER=angelone,yfinance,dhan`, `DHAN_SHADOW_COMPARE=1`
   (this is what `.env.example` ships). `docker compose up -d --force-recreate market-data-service` (a plain restart does not re-read `.env`).
3. Run one market session. Read `GET /internal/dhan-status`: `credentials.ok`, `scrip_master.equities` (about 2000+),
   `client.errors`, `quotes.prev_close_disagreements`, `live_poller.shadow.mean_diff_pct` / `over_half_pct`.
4. If prices agree: set both order lines to `dhan,angelone,yfinance` and recreate market-data-service.
5. Roll back at any time by editing those two lines back.

## Verify on the VM (NOT confirmed against a live Dhan account)
- Scrip master URL and column names (`SEM_EXM_EXCH_ID`, `SEM_SEGMENT`, `SEM_SMST_SECURITY_ID`, `SEM_INSTRUMENT_NAME`,
  `SEM_TRADING_SYMBOL`, `SEM_SERIES`). Header names are matched case-insensitively; a load that parses fewer than 100 NSE
  equities is rejected and the last good map (or the stored snapshot) stays.
- `ohlc.close` in `/marketfeed/quote` may not be yesterday's close during the session. Disagreements with `net_change`
  are counted in `quotes.prev_close_disagreements`; nothing is silently changed.
- Dhan error codes (805/806/807...), the intraday window per request (`DHAN_INTRADAY_CHUNK_DAYS`), the `last_trade_time` text format.
- **Adjusted prices**: yfinance candles are split/dividend adjusted; Dhan daily candles may not be. Compare a stock with a
  recent split/bonus before using Dhan candles for ML training, and keep a dataset on ONE source end to end.
  `MAX_HISTORY_PERIOD` (default 1y) and `MAX_HISTORY_ROWS` (260) also cap what training can get through market-data-service.
- Websocket (`DHAN_WS_ENABLED=1`, off by default): the binary layout is written from Dhan's docs without a live capture.
  Unknown packet codes are counted in `/internal/dhan-status` under `websocket.unknown_codes`.

## Tests
- `market-data-service/tests/test_group270_dhan_core.py`, `..._live_and_ws.py`, `..._main_integration.py` (147 tests, all offline).
- `analysis-intelligence-service/tests/test_sentiment_market_data.py`.
- `decision-prediction-service/training/tests/test_group270_md_history.py`.
- market-data-service conftest switches the Dhan stage off for older tests (`DHAN_DATA_ENABLED=0`); analysis conftest turns the
  sentiment market-data path off for older tests.
- In my sandbox the full market-data-service suite has 16 failures that also occur on the uploaded group 269 code; the full
  analysis suite has 1 (also on group 269). real-trade-service and position-stocks-service were not modified and not re-run.

## Completed in the group 274 merge
Checked against the original plan and fixed what was missing:
- `api-gateway/tests/test_kv_cache_drift.py` did not know the `stockky:dhan_scrip` durable prefix market-data's `kv_cache.py` gained here, so
  the drift guard failed once api-gateway's suite ran (the Dhan zip never ran it). The prefix is now a documented market-data-only extra, with a
  guard that no other service uses it.
- `DHAN_HOURLY_MAX_DAYS` was read with a bare `int(os.getenv(...))` in `main.py`; it now goes through `dhan_data.config.hourly_max_days()` (blank/garbage -> 60).
- `DHAN_QUOTE_ENDPOINT`, `DHAN_HOURLY_MAX_DAYS`, `DHAN_CREDS_CACHE_S`, `DHAN_WS_URL`, `DHAN_WS_MAX_INSTRUMENTS` were read by the code but missing from
  `.env.example` and `.env.oracle.recommended`; added (commented, defaults shown).
- New tests: `market-data-service/tests/test_group274_dhan_env_gaps.py` (blank-safe reads, every Dhan variable documented) and
  `api-gateway/tests/test_yfinance_callers_guard.py` (the plan's "no new direct yfinance callers" guard, with an allow-list of today's files).

