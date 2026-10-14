# Group 351 (2026-10-13): daily market-structure gate in real-trade, board engine status in position-stocks, frontend

## real-trade-service
- `entry_engine/structure_gate.py` (new): pure `evaluate(summary, live_price)` -> `would_reject`, `reasons`, live chase / stretch / room / R:R;
  `structural_levels()` (structural stop next to the ATR stop, sane when its risk is 0.5x-1.6x the ATR risk); `snapshot()`; `get_summary()`
  (candidate payload first, else api-gateway `GET /stock/{symbol}/structure`, cached, fail-open).
- `entry_engine/entry.py`: Gate 4e after the VIX / VWAP / pre-open / calendar / liquidity gates, before the R:R floor. `shadow` (default) records only;
  `enforce` WAITs with `gate_tag="structure"`; `off` skips. `STRUCTURE_STOP_MODE=on` swaps in the structural stop only when sane. Any error in the gate = allow.
- No / stale / insufficient structure, an old last bar, or a live price >15% from the structure close = **no opinion** (never blocks, never bullish)
  unless `STRUCTURE_REQUIRE_DATA=true` (then WAIT, tag `structure_nodata`).
- Live adjustment: summary values are at the structure close, so chase / stretch / room are shifted by (live - ms_price)/ATR and R:R is recomputed at the live entry.
- `trade_decisions.structure_json` (new nullable column, auto-migrated for SQLite/Postgres and Oracle); `/candidates` rows carry `latest_decision.structure`.
- Config: 18 `STRUCTURE_*` vars (config.py, `.env.example`, `docs/ENV_REFERENCE.md`).

## position-stocks-service
- `screening/ms_engine_switch.py`: counters (`engine_calls`, `legacy_calls`, `fallbacks`, `last_error`) and `stats()`; `GET /board/status` gets `ms_engine`.
  Flag `BOARD_USE_MS_ENGINE` is still default 0.

## frontend
- `structureView.ts` (pure helpers + `structureView.test.mjs`), `components/StructurePanel.tsx` (collapsed by default, one-line mobile summary, ✓/✗ groups,
  invalidation, stop options, empty states), `realTradeApi.ts` types, `RealAutoTrade.tsx` (`[STRUCTURE GATE]` badge, shadow chip, panel, per-list tally with coverage %),
  `SessionHealthPanel.tsx` (board zone engine line).

## Tests
- real-trade: `test_group351_structure_gate.py` (17), `test_group351_structure_entry_wiring.py` (10); full suite 4483 pass. 7 failures are identical on the untouched zip
  (oracledb missing, wall-clock dependent).
- position-stocks: `test_group351_ms_switch_stats.py` (4); full suite 3638 pass, 4 failures identical on the untouched zip.
- frontend: `tsc` clean, `vite build` ok, all `*.test.mjs` pass.

## Not done here (still open from the plan)
Decision-service policy (WS-5) and its UI (DecisionCard panel, BuySniperModal levels, ScanPanel rejection summary), technical-service closed-bar / untrimmed frame (F7),
gateway sniper chase gate and PREPARE refresh reasons, observations / backtest / calibration (WS-6/7), chart overlays, Training views. Thresholds stay hypotheses until WS-6/7.
