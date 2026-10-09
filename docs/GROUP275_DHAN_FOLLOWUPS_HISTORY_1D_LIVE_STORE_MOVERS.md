# Group 275 - Dhan follow-ups (2026-10-09)

Found by checking a live `/internal/dhan-status` output and the real-trade log against the Dhan data path.

| # | Where | Problem | Fix |
|---|---|---|---|
| 1 | market-data `main.py` (`_dhan_history_attempt`, `_angelone_history_candles`, `_nse_history_candles`) | `period=1d` had no entry in any period->days table: window fell to 180 days (Dhan then capped it to 60), so real-trade's 1d/60m call got ~7 weeks of hourly bars and its 1d return was dropped as implausible | `1d` = latest session: 5-day window, then `_history_trim_latest_session` keeps the last session's bars (daily/weekly intervals keep the last bar). Explicit `days=` windows are never trimmed. `_HISTORY_PERIOD_DAYS` deliberately unchanged (it drives cached-series slicing) |
| 2 | `dhan_data/live_store.py` | One 900-row MERGE could pass Oracle's 8 s call timeout (DPY-4024) and the whole batch was lost | Rows written in chunks (`DHAN_LIVE_WRITE_CHUNK`, 150), each its own transaction; a failed chunk is counted and the rest still written; queued backlog is coalesced (newest price per symbol). Status adds `failed_chunks`, `timeouts`, `coalesced_batches`; failures log a throttled warning |
| 3 | `dhan_data/quotes.py` | `empty_symbols` was a bare count | Status lists up to 25 recent symbols, split into `empty_no_scrip_*` and `empty_no_data_*` (+ distinct counts) |
| 4 | market-data `GET /angelone/movers` | Full 2584-symbol AngelOne sweep | Dhan first when it is the FIRST quote provider (`DHAN_MOVERS_VIA_DHAN`, default 1); only unpriced symbols go to AngelOne. Result adds `dhan_priced` / `angelone_fetched`; coverage uses the combined count |
| 5 | api-gateway `_get_nifty50_data` | Per-symbol yfinance 1m history | `_movers_rows_from_market_data` (chunked `/quotes/bulk`) first; yfinance only for the rest. `GATEWAY_MOVERS_VIA_MARKET_DATA` (1), `GATEWAY_MOVERS_QUOTE_MAX_AGE_S` (90) |
| 7 | market-data `main.py` | AngelOne poll (~500 symbols / 3 s) and Yahoo feed run next to Dhan | `ANGELONE_WS_FEED_ENABLED` / `YAHOO_WS_FEED_ENABLED`, default 1 (no behaviour change). Set to 0 only after a clean session with Dhan first |

## Not changed on purpose
- #6 position-stocks-service `feed/ws_client.py` (own AngelOne websocket): optional Phase 4, moving the live scalper feed needs a live session.
- #8 training helper (`TRAINING_DATA_VIA_MARKET_DATA`): stays off until Dhan daily candles are checked for split/bonus adjustment.

## Caveats
- #4 and #5 trust Dhan's previous close (plan risk R3); `prev_close_disagreements` was 0 but check movers against known movers after deploy.
- AngelOne/NSE `1d` edits share the tested trim helper; the provider calls themselves are not unit-tested.

## Tests
- New: `market-data-service/tests/test_group275_dhan_followups.py` (47), `api-gateway/tests/test_group275_movers_via_market_data.py` (6).
- market-data: 1469 passed, 8 failed; api-gateway: 8469 passed, 6 failed. The same failures occur on the group 274 zip in full-suite runs (they pass when run alone), so no regressions.
- Other services untouched, not re-run.
