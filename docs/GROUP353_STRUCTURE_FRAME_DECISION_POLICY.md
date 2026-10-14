# Group 353 - F7 structure frame + decision structure policy

## Technical service (`analysis-intelligence-service/technical/main.py`)
- Structure runs on the UNTRIMMED daily frame (kept from the fetch in a 64-entry / 15 min store, used only when its last bar matches the analysis frame) and on CLOSED bars only: today's bar is dropped until 15:30 IST + `STRUCTURE_BAR_CLOSE_BUFFER_MIN` (default 20). `ctx.ema20` is recomputed on that frame.
- `market_structure.frame` = {bars, full_history, forming_bar_dropped, source, last_bar}. `ms_price` is therefore the last CLOSED close.
- `STRUCTURE_PARAMS_JSON` overrides engine params. `/health` -> `structure` {enabled, engine_version, counters}.
- Tests: `tests/test_group352_structure_frame.py`; `test_technical_main.py` health / key-shape updated for the additive keys.

## Decision service (`decision-prediction-service/decision/structure_policy.py`)
- `STRUCTURE_POLICY_MODE`: off | shadow | caps | full (default since group354). Only ever tightens a BUY NOW (-> PREPARE TO BUY / WAIT). Unknown quality / stale / drifted price = no opinion.
- Rules: P2 chase, P3 stretch, P4 structure R:R / no target / wide stop, P5 bearish trend (unless recent bullish CHOCH), P6 room to resistance, P7 thin confirmation, P8 failed BOS. P9 = PREPARE re-check triggers (informational).
- Runs as the last step (after data-quality gate and multi-horizon). Response gains `structure` {mode, applied, original_decision, policy_decision, rules, no_opinion, legacy_levels, structural_levels, wait_for_entry, prepare_triggers, features, summary_line}. In `full`, structural stop/target replace the legacy ones only when the stop distance is 0.5x-1.6x the legacy distance.
- Thresholds are HYPOTHESES (override with `STRUCTURE_POLICY_PARAMS_JSON`) until WS-6/7 calibrate them. Keep shadow until then.
- Tests: `tests/test_group353_structure_policy.py` (16), `tests/test_group353_decide_structure_wiring.py` (4).

## Frontend
- `Decision.structure` type; `DecisionCard` policy line / wait range / PREPARE re-check / mobile one-liner / collapsible panel; `ScanPanel` "why nothing qualified" structure summary. Helpers + tests in `structureView.ts` / `structureView.test.mjs`.

## Still open
Gateway sniper chase gate + PREPARE refresh + `BuySniperModal`, daily post-close refresh job, WS-0 golden baselines, WS-6 observations + Training view, WS-7 backtest/calibration, chart overlays, coverage % indicator. Run position-stocks / real-trade suites (not re-run this group).
