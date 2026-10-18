# Group 441: P8 retest evidence, intraday structure, ML V2 walk-forward, legacy-board retirement

Status: built and unit-tested offline. Nothing is switched on, no live decision changes. None of the tools has run on real data yet.
(The build log called this "group437"; renamed because 437 is already the compose-duplicate-limits fix.)

## 1. P7X in the replay + P8 retest evidence
* `shared/market_structure/backtest.py`: `POLICY_THRESHOLDS["groups_strict_min"]` (default 0 = off) and rule `p7x_indep`, identical to the live `P7X_INDEP`
  (a missing `ms_groups_strict` trips it only when the rule is on). `ms_ab_report.py` reports it. `scripts/test_policy_replay.py` proves replay == live over a grid.
* `scripts/p8_retest_evidence.py HIST_DIR`: replays history with the retest branch off and on and sorts each qualifying day into clean / rescued / blocked / other.
  Edge = mean net(rescued) - mean net(blocked), cluster bootstrap by symbol x month. Verdicts: `no_data`, `insufficient_sample`, `retest_ok`, `retest_hurts`, `no_edge`.
  Entry = next session open + slippage, round-trip cost subtracted, a bar touching both stop and target counts as a stop.

## 2. Intraday structure (record / evidence only)
* `shared/market_structure/intraday.py` (vendored into 5 services as `stockky_ms/intraday.py`): session-aligned 15m / 5m bars from 1-minute bars, intraday params (own
  `params_hash`, so rows are never mixed with daily ones), `analyze_intraday`, `replay_intraday` (no lookahead). Summary keys are `i15_*` / `i5_*`.
* `services/position-stocks-service/feed/bar_recorder.py`: writes today's 1-minute bars to `BAR_RECORD_DIR/<IST date>/<symbol>.json` (atomic, merge-on-restart, one final flush
  after 15:30, retention `BAR_RECORD_KEEP_DAYS`). Called from the board runner; its failures are counted but never fail the runner. An unwritable directory turns it off and
  `/board/status` says why. **ON by default** (`BAR_RECORD_ENABLED=false` turns it off); compose mounts the named volume `bar-records` at `/data/bars`.
  Env: `BAR_RECORD_ENABLED`, `BAR_RECORD_DIR`, `BAR_RECORD_EVERY_S` (min 60), `BAR_RECORD_MAX_SYMBOLS`, `BAR_RECORD_MIN_BARS`, `BAR_RECORD_KEEP_DAYS`.
* `scripts/export_intraday.py BAR_DIR`: intraday rows with forward returns + `coverage.json/.md`.
* `scripts/intraday_gate_backtest.py BAR_DIR`: VWAP gate and opening gate on recorded minutes, entries sampled every N minutes, session-clustered bootstrap, only complete
  sessions. The replayed rules are tested against the live gates in `real-trade-service/entry_engine`. The stop-vs-ATR opening check is not replayed.

## 3. ML V2 walk-forward (offline)
`scripts/ml_v2_walkforward.py HIST_DIR`: FEATURE_COLUMNS_V2 vectors from replayed history, time-ordered folds with an embargo so no training label overlaps the test block,
stdlib ridge on the 0/1 net-return label, AUC vs `ms_tqs`, top-30 % picked edge with a cluster bootstrap, versioned report (`ml_v2_report.json/.md`, `ml_v2_model.json`).
Never touches `training/train.py` or the registry; the saved model is a record, not a deployable artifact.

## 4. Legacy board retirement
`scripts/retire_legacy_board.py (--file board_status.json | --url .../board/status) [--apply]`. Requires `board_parity_check.py` = GO. Replaces the bodies of `swings` and
`find_zones` in `screening/market_structure.py` with one-line calls to `stockky_ms.compat_board` (signatures and defaults unchanged, checked on the AST), drops `_is_displacement`
if unused, writes a `.pre_retire` backup. Dry run by default; idempotent; refuses on NO_GO / NOT_ENOUGH_DATA / NO_INPUT, an unexpected file, or a missing adapter.
It does not touch `ms_engine_switch.py`: afterwards the A/B compares the engine with itself and the fallback cannot protect the board, and `BOARD_USE_MS_ENGINE=0` no longer
means "legacy" (rollback = the backup or git). Remove those deliberately after a few clean sessions. **Not applied in this zip.**

## Tests
`shared/market_structure/tests/test_intraday.py` (15), `scripts/test_p8_retest_evidence.py` (8), `scripts/test_intraday_gate_backtest.py` (17), `scripts/test_ml_v2_walkforward.py` (10),
`scripts/test_retire_legacy_board.py` (9, on a temp copy), `services/position-stocks-service/tests/test_group441_bar_recorder.py` (14), extended `scripts/test_policy_replay.py`.
