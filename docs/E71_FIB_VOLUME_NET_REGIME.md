# E71 - Fibonacci on the chart, breakout volume (log), paper net P&L, regime rules (log)

Items 3-6 of the open list (E70 hand-over). Nothing here changes a live entry: the only new rule that can read a candidate (breakout volume) ships `log`, and the regime rules have no enforce mode.

## 3. Fibonacci levels on the candle chart (E25b)
- `shared/market_structure/chart_context.py`: `fib_levels(block)` turns the engine's `fibonacci` block into drawable levels (0 / 23.6 / 38.2 / 50 / 61.8 / 78.6 / 100 % + 127.2 / 161.8 % extensions, golden zone 50-61.8 %). `chart_context(..., fib=)` carries it as `fib` (None when absent). Display only: no badge, no event, no score.
- `analysis-intelligence-service/technical/main.py`: daily and intraday chart-context pass `block["fibonacci"]` (intraday charts get the DAILY leg - a daily level is still a price).
- Frontend: `normalizeFib` in `chartContext.ts`; `CandleChart` draws the golden band + one thin line per level inside the visible price range (dimmed when the leg is invalidated); `StockCandleBlock` has a "Fib" toggle and a one-line description.
- Tests: 5 new engine tests, 1 service test, `chartContext.test.mjs` (+ `normalizeFib`).

## 4. Volume required on breakouts - LOG ONLY (D-vol)
- Engine: the BOS event now carries `vol_ratio` (breakout bar volume / its 20-bar mean) -> `bos.vol_ratio` -> summary key `ms_bos_vol_ratio`. `analyze_golden.json` regenerated: the ONLY diff is the new `vol_ratio` field (checked).
- Real Trade gate: `STRUCTURE_BREAKOUT_VOLUME_MODE` = `log` (default) | `enforce` | `off`; `STRUCTURE_BREAKOUT_MIN_VOL_RATIO` 1.2 (0 = off); `STRUCTURE_BREAKOUT_BARS` 5. A fresh, not-failed bullish BOS below the ratio is an *observation* in `verdict["observations"]` and on `structure_json` (counted as `thin_volume_breakouts` on `/structure/status`); only `enforce` turns it into a gate reason. A summary without the key (old engine copy) is no opinion. Both switches are on the calibration page.
- Position Stocks is NOT changed (its gate has only enforce / off, so there is no log-only path there yet).

## 5. Net-of-charges P&L on the paper Trades page
- `frontend/src/paperNet.ts` (same Dhan delivery rate card as `chargesRates.ts`: STT both legs, stamp on buy, exchange / SEBI / GST, DP fee unless sold the same day). Closed table has a "Net" column, open cards show "after charges (est.)", the summary line shows "after charges" for all closed trades. Real Trade, Position Stocks and the calibration pages already had it.

## 6. Rules that switch by market regime - LOG ONLY
- `real-trade-service/entry_engine/regime_rules.py`: regime = weak (score < adaptive gate) / neutral / strong (score >= `REGIME_RULES_STRONG_SCORE`, 60). Weak market: R:R floor 2.5, conviction floor 65, advisory risk scale 0.5; neutral / strong use today's floors. Each candidate gets a hypothetical verdict as a `REGIME_RULES` log line and on `GET /regime-rules/status` (admin). It NEVER blocks, resizes or changes a stop / target; `REGIME_RULES_MODE` is `log` | `off` (enforce is treated as log). Table override: `REGIME_RULES_TABLE_JSON`.
- Promote only after gate_check / the calibration pack show an edge out of sample - that is a code change on purpose.

## Not done (needs your decision or VM evidence)
1. Position Stocks structure gate stays `enforce` (no behaviour change). Only the stale `.env.example` comment was fixed (Real Trade defaults to `log`, Position Stocks to `enforce`).
2. No general R:R floor added to Real Trade: `STRUCTURE_MIN_RR` is still 0 (off) because its replay edge flipped sign between seeds (group424b). Say a value if you want one.
7-11 unchanged (D22-D24 wiring + downloaders, gateway router split, requirements merge, Docker `pip install ./shared`, C21 live).

## On the VM
`cd shared/market_structure && python3 tests/run_plain.py`; `python3 scripts/sync_market_structure.py --check`; `cd frontend && npm ci && npx tsc && npm test && npx vite build` (tsc / vite were NOT run here: the sandbox cannot reach the npm registry); real-trade + analysis-intelligence pytest suites (pytest is not installable here - the new / touched gate, regime, catalog and chart tests were run with a small stand-in runner and pass); rebuild technical (analysis-intelligence), api-gateway, real-trade, position-stocks, decision-prediction (engine copies) and frontend. Then open a stock chart (Fib button, daily and 15m) and Trades -> Closed (Net column); `curl /regime-rules/status` after a cycle; `/structure/status/REAL` shows `thin_volume_breakouts`.
