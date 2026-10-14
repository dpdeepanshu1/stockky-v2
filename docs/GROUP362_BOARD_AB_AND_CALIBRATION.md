# Group 362 - Board A/B and outcome calibration

Two jobs from the open-work list: (1) compare the legacy Market Board zone maths with the `stockky_ms` engine on the same bars, (2) turn the observed gate outcomes (g356 / g361) into advisory calibration. Both are observational: nothing served or enforced changes.

## 1. Board A/B (`position-stocks-service`)
`screening/ms_engine_switch.py`: while the engine serves the board (`BOARD_USE_MS_ENGINE`, default on), each fresh zone computation also runs through the legacy code on the same bars and the two are compared (zones matched on side + idx). What is served is the engine's output, unchanged.
- The comparison runs BEFORE the board's newest-6 cap, so `engine_added` (a zone only the engine has) is exact. When the engine drops one zone, an older one can slide into the newest 6; that visible difference is counted as `cap_knockon`, not as a contract break. `served_differs` = what the board user actually sees differing, any cause.
- Counters: `compared, identical, zones_differ, swings_differ, engine_dropped, engine_added, status_changed, bounds_changed, other_changed, served_differs, cap_knockon, errors`, per-day volume (14 days), the 50 newest differing samples.
- Read it in `GET /board/status` -> `ms_engine.ab` (no new route). `verdict`: `disabled | insufficient_sample (< 200 compares) | contract_break (engine_added, swings_differ or errors > 0) | parity_ok`.
- Config: `BOARD_AB_ENABLED` (1), `BOARD_AB_MAX_COMPARES` (100000 per process, 0 = no cap). Cost: one extra legacy + one uncapped engine pass per fresh computation (they are cached per symbol until bars change).
- Synthetic result (300 random-walk days, NOT real board data): 278 identical, `engine_added=0`, `swings_differ=0`, 15 status differences (`tested` where legacy says `fresh`), 8 dropped zones, 2 cap knock-ons, `parity_ok`. The real verdict needs a full session on live boards.
- A first version of the compare ran after the cap and reported a phantom "engine added a zone"; that was the knock-on above. Fixed by comparing before the cap.

## 2. Calibration (`stockky_ms.observation.calibrate`)
Shared pure function, vendored to all five services. Input: observation rows (with `summary_json`). Output (advisory, never applied):
- per gate rule (chase, stretch, room, rr, stop_wide, bearish): edge = passed mean - rejected mean at the chosen horizon, `advice` = `keep | review_loosen | no_evidence | collect_more`;
- for chase / stretch / room / rr: expanding-window walk-forward over `CAL_GRIDS`; `suggested` only when folds are stable, pooled out-of-sample edge > 0.25 and the best threshold differs from the shipped one (needs >= 44 finished rows);
- `ms_tqs` bucket table (does a higher score rank outcomes?);
- horizon = longest of T+5/10/20 with >= `min_n` finished rows, else T+5 (note `insufficient_sample`).
Each report passes ITS OWN gate thresholds: sniper (`STRUCTURE_*` env, real-trade defaults), real-trade (`config.STRUCTURE_*`), position-stocks (own looser defaults; no R:R rule, `rr=0`).
New key `calibration` on: `GET /api/scan/find-buys/edge`, `GET /structure/edge` (position-stocks), `GET /structure/edge/{mode}` (real-trade). A calibration failure returns `{advisory, error}` and never breaks the edge report. Position-stocks / real-trade `_row_dict` now carries `summary_json`.
Caveat: one row per symbol per day; neighbouring days overlap in time, so edges are indications, not proof. Expect `insufficient_sample` until ~20 trading days of finished rows.

## 3. UI
`StructureEdgeCard` shows a calibration section (rules that tripped, advice, "worth testing (not applied)", score ranking). New card "Structure gate - Buy Sniper" in the Training tab (`api.findBuysEdge`). `calibrationView()` in `structureView.ts`.

## Tests
- shared: `tests/test_observation_calibrate.py` (7); engine suite 97 pass.
- gateway `test_group362_sniper_calibration.py` (5), position-stocks `test_group362_structure_calibration.py` (4) + `test_group362_board_ab.py` (14), real-trade `test_group362_structure_calibration.py` (4).
- frontend node tests (`structureView.test.mjs`) pass; `structureView.ts` type-checks strict; TSX files only syntax-checked.
- Run with a stand-in runner (no pytest in the sandbox). `test_group354_structure_gate.py` failures under that runner (3 parametrized tests) are identical on the untouched g361 zip, so they come from the runner, not from this change.

## Not run
Real pytest, real fastapi app, `npm run build`, live Neon / market data, tests needing websockets / sqlalchemy / httpx.
