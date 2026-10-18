# Group 434 - decision structure policy defaults to record in docker-compose

## Change
`docker-compose.yml`: `STRUCTURE_POLICY_MODE` default `full` -> `record` for decision-prediction-service. While gate G2 is `no_edge`, untested structure
rules no longer downgrade BUY NOW or replace stop / target; they are still evaluated and logged (`features.would_be`, `structure.would_be_decision`).
The code default in `structure_policy.py` is unchanged (`full`), so only deployments using this compose file are affected.

## This LOOSENS live gating
BUY NOW calls that P1-P8 would have downgraded now pass through as the legacy engine made them (see group 422). Not covered, still live: real-trade
`structure_gate`, the sniper gate (`STRUCTURE_MIN_RR` is already 0 since 424b), position-stocks gate.

## Rollback
`STRUCTURE_POLICY_MODE=full` in the VM `.env` (a value there overrides the compose default). `off` stays the kill switch.

## Not done in this group (data not available)
Seed-13 `summary.txt`, `hist400` backtests and `structure.log` / `structure_out.json` were not in the zip, so the 424b `chase` / `stop_wide` check, the
robust backtest tables and the 52-week log-only switch are still open (REMAINING_TASKS.md, "Open after group 428").

## Rejection counters (second change in group 434)
`GET /structure/rejection-stats` on the decision service (counters since boot, per process, reset on restart):
`considered`, `structure_rejections`, `rr_rejections`, `chase_rejections` (rule fired AND applied, decision lowered) and
`structure_would_reject`, `rr_would_reject`, `chase_would_reject` (fired but record-only, decision unchanged). Counted once per decision.
`rr_*` is the whole `P4_RR` rule (ratio, no target, stop too far); `chase_*` is `P2_CHASE`; a P9 upgrade is not a rejection; `off` mode is not counted.
Under the new `record` default the `*_rejections` stay 0 and the `*_would_reject` counters show what live gating would have blocked.
Code: `structure_policy.py` (`apply_policy` is now a thin wrapper over `_apply_policy_impl`, same contract), `decision/main.py`.
Tests: `decision/tests/test_group434_rejection_counters.py` (4). No behaviour change to any decision.

## 52-week range log-only switch (third change in group 434)
`CANDIDATE_52W_LOG_ONLY=1` (real-trade-service, default `0`): the 52-week "top 12%" rule is still evaluated, but instead of rejecting the candidate it logs
`52W_LOG_ONLY would reject <SYMBOL>: <reason>` and bumps a counter (`candidate_engine.candidates.range_52w_log_only_stats()`, per process, since boot).
Default behaviour is unchanged. **Turning it on LOOSENS an entry rule** (near-52w-high stocks that are not fresh volume-confirmed highs are let through):
only use it if the robust `legacy_gate_backtest.py` 52-week row still says `hurts` on hist400 - that report was not available when this was built, so the
switch ships off and was not chosen on evidence. Other 52-week switches (`CANDIDATE_52W_RULE_ENABLED`, `_REGIME_GATE`, `_FRESH_HIGH_EXEMPT`) are unchanged.
Tests: `tests/test_group434_52w_log_only.py` (3) - written but NOT run here (no pytest / sqlalchemy in the build container); run them on the VM.

## `scripts/env_structure_check.py` (fourth change in group 434)
`python3 scripts/env_structure_check.py ~/stockky-v2/.env [--strict]` replaces the manual `grep STRUCTURE_ .env` before Monday. It lists every `STRUCTURE_*` /
`CANDIDATE_52W_*` line and flags each value that differs from the shipped default as `LOOSENS`, `TIGHTENS` or `OVERRIDE` (old `STRUCTURE_EXIT_MODE=exit`,
`STRUCTURE_STOP_MODE=on`, `STRUCTURE_PREPARE_UPGRADE=1`, `STRUCTURE_MAX_AGE_DAYS=7`, and now `STRUCTURE_POLICY_MODE=full`, which would silently undo the record
default). Read-only; exit 1 with `--strict` when anything is flagged. Defaults live in the `INTENDED` table at the top of the script: update it when a default changes.
Tests: `scripts/test_env_structure_check.py` (5, run OK with a plain runner).
