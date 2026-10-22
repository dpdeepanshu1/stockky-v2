# C21 - pattern signals are logged, not scored (E35, 2026-10-23)

**Log-only.** No gate, score, sizing rule or model reads anything below. `technical_quality_score` is unchanged (tested).

| Piece | Where | What |
| --- | --- | --- |
| Pattern feature row | `shared/market_structure/ml_features.py` | `PATTERN_COLUMNS` (23 `pt_*` columns), `pattern_columns_hash()`, `build_pattern_features()`, `pattern_vector()` |
| Shadow evidence | `shared/market_structure/scoring.py` | `pattern_shadow()`: five separate groups (pattern, squeeze, trend, momentum, sr_flip), each +1 / -1 / 0 / None, with `log_only: true, in_tqs: false` |
| Wiring | `shared/market_structure/extras.py` | `analyze_extras()` now also returns `pattern_features`, `pattern_shadow`, `pattern_columns` (served by `/structure/{sym}?extras=1`) |

* `FEATURE_COLUMNS_V2` (32 columns) and its hash are NOT changed, so rows already stored stay comparable. Pattern rows use their own hash.
* A section that could not be computed (too few bars, detector error) stays `None`, never 0.
* A candle pattern and a confirmed chart pattern share one group, so one move is not counted twice. Forming chart patterns are not counted in the shadow.
* Copies: `python3 scripts/sync_market_structure.py` was run (5 service copies). Tests: `shared/market_structure/tests/test_pattern_logging.py` and its generated copy `services/analysis-intelligence-service/tests/test_stockky_ms_pattern_logging.py`.

**Not done here:** nothing stores these rows to the DB yet; they are returned by the extras endpoint only. A stored table and the join to outcomes come with the B-tools comparison (item 16/17). A pattern joins a decision only after it beats the MA-crossover baseline (`scripts/ma_baseline.py`).
