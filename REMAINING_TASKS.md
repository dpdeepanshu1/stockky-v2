# Remaining tasks: market-structure engine (after group 359)

## Done so far
- Engine package `shared/market_structure/` (incl. `replay`, `backtest`, `observation`, `candles`, `compat_board`), `analyze()` end to end, vendored into 5 services as `stockky_ms/` (`sync --check` clean). Golden baseline `tests/test_engine_golden.py`.
- Technical service: `market_structure` + `structure_summary` on `/analyze`, `GET /structure/{symbol}`, `lite=1`, structure cache, size guard, closed-bar frame (g353).
- api-gateway: `ms_passthrough`, `/stock/{symbol}/structure`, `ms_*` on feed rows, `structure_summary` in the decide payload, `structure_coverage` on `/api/feed/audit-missing` (g356).
- decision-prediction: `structure_policy` (shadow by default, g353).
- real-trade / position-stocks: daily-structure entry gate (live, enforce), structural stop, status endpoints (g351-355), observation tables + T+1/5/10/20 evaluator + `GET /structure/edge...` (g356).
- Frontend: `StructurePanel`, `DecisionCard` policy line, `StructureGateCard`, `StructureEdgeCard`, chart overlay, feed-health "Structure read %" (g351-356).
- **Buy Sniper gate (g357, WS-4):** `buy_sniper.py` gate (also on the HOLD early-breakout path), route enrichment in `/api/scan/find-buys`, modal structure panel + "turned away" note, `api.ts` forwards `structure_summary`. See `docs/GROUP357_SNIPER_STRUCTURE_GATE.md`.
- **PREPARE refresh reasons (g358, log-only):** `/data-feed/refresh-prepare-to-buy` returns `structure_notes` / `structure_counts` and logs them; `ScanPanel` lists them. `SNIPER_PREPARE_STRUCTURE_MODE` off|log. See `docs/GROUP358_PREPARE_REFRESH_STRUCTURE_NOTES.md`.
- **Structure refresh job (g360):** `POST /data-feed/structure-refresh` refreshes the feed rows' `ms_*` keys after the close; see `docs/GROUP360_STRUCTURE_REFRESH_JOB_GATEWAY_COUNTERS.md`.
- **Trades tab entry structure (g359):** the structure record is stored once at paper-trade entry (`feature_snapshot.structure_entry`, no schema change), returned by `GET /api/trades` as `structure`, and shown in the Trades tab. See `docs/GROUP359_TRADES_ENTRY_STRUCTURE_SNAPSHOT.md`.

- **Structure exit + edge floor + board golden (g365):** real-trade watches daily structure on open positions (`STRUCTURE_EXIT_MODE=tighten`, live), `STRUCTURE_EDGE_MIN_N=100`, `PS_*` overrides, legacy board golden. See `docs/GROUP365_STRUCTURE_EXIT_AND_EDGE_FLOOR.md`.

## Verify on your side (not run in the sandbox: no pypi / npm)
1. Real `pytest` for: api-gateway (`test_buy_sniper`, `test_group357_*`, `test_group358_*`, `test_group349_ms_passthrough`), real-trade, position-stocks (incl. the SQLite round-trip and `test_group356_structure_observation_wiring`), decision-prediction (incl. `training/tests/test_group359_structure_entry.py`), technical. Expected failures: only the known 4 `test_oracle_compat`.
2. `npm ci && npx tsc --noEmit && npm run build` in `frontend/` (only a syntax-level pass and the node tests were run).
3. Start the gateway + technical service and hit `POST /api/scan/find-buys` with a few rows (no `structure_summary`): the response should carry `structure_mode`, `structure_rejected`, and each card a `structure` record. Also `/stock/RELIANCE/structure`, `/technical/structure/RELIANCE`.
4. If the gateway shares an env file with position-stocks, set `STRUCTURE_MAX_CHASE_ATR` etc. explicitly for the gateway (position-stocks uses looser own defaults; the sniper gate is meant to match real-trade).

## Open work, in order
1. ~~Post-close `structure_only` refresh job and gateway counters~~ done in g360 (`docs/GROUP360_STRUCTURE_REFRESH_JOB_GATEWAY_COUNTERS.md`). Still to do on your side: schedule `POST /data-feed/structure-refresh` (or the QStash tick action) at ~16:00 IST on trading days. Small follow-up: a DataFeed tab button / status line and structure coverage in feed health.
2. ~~Sniper outcome tracking~~ done in g361 (`docs/GROUP361_SNIPER_OUTCOME_TRACKING.md`). Still to do on your side: schedule `{"action":"sniper-observe-eval"}` on the QStash tick at ~16:30 IST; read `GET /api/scan/find-buys/edge` after ~20 trading days. Edge card added in g362 (Training tab, with calibration).
3. ~~Board A/B~~ built in g362 (`docs/GROUP362_BOARD_AB_AND_CALIBRATION.md`). Still to do on your side: after one full live session read `GET /board/status` -> `ms_engine.ab`; `parity_ok` = keep the engine, `contract_break` = read `samples` (set `BOARD_USE_MS_ENGINE=0` if real). Retire the legacy fallback after a clean week.
4. **Rollout / calibration:** `observation.calibrate()` (g362) now produces advisory per-rule verdicts and out-of-sample threshold hints in every edge report; apply nothing before ~20 trading days of finished rows. the design document is lost, so `params.py` thresholds and every `STRUCTURE_*` limit are hypotheses. Observation edge cards need ~20 trading days of finished rows before they conclude anything. Do not retrain on mixed engine versions (`params_hash`, `engine_version` are stored).
5. **Housekeeping:** add `sync --check`, `check_shared_copies` and the golden test to CI.

## Known gaps
- `compat_board` is rebuilt from the legacy code, not recovered; `swings()` with left/right < 1 returns [].
- Detector and golden tests use synthetic bars, not real market data; the golden file freezes behaviour, it does not prove the thresholds right.
- The sniper gate is enforce-by-default (like real-trade); there is no proven edge yet. `SNIPER_STRUCTURE_MODE=shadow` is the safe way to watch first.
- Dockerfiles need no change (`COPY . .`).
