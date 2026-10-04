# group107 (2026-10-04) - scheduler's stale-constant warning showed an old value (ENTRY_REGIME_MIN_SCORE 38, live is 25)

Cumulative on group106. Rebuild: `docker compose build notification-scheduler-service && docker compose up -d`.

## Cause
notification-scheduler-service keeps its own hardcoded copies of the regime constants (`scheduler/governance_check.py` and the inline copy in `scheduler/weekend_hydrator.py`). real-trade-service lowered `ENTRY_REGIME_MIN_SCORE` from 38 to 25 on 2026-09-03; the scheduler copies still said 38, so the weekly stale-constant warning and the governance check named a value trading does not use.

## Change
- Both scheduler copies now show `ENTRY_REGIME_MIN_SCORE = 25` (the real-trade-service default).
- New `tests/test_regime_constants_drift.py` (3 tests): reads real-trade-service/config.py as source and fails if either scheduler copy's VALUE differs from it. Checked that it fails when the old 38 is put back.

## Deliberately not changed
- **Review dates.** All scheduler dates stay 2026-08-28 and real-trade-service's stay 2026-09-03. A date means "the owner reviewed this value"; nothing here is a review, so no date was moved. The six real-trade constants are 31 days old today and the warning is accurate (item 25 still needs your review).
- **CANDIDATE_OVEREXTENDED_52W_TOP_PCT** exists in real-trade-service but not in the scheduler copies. Adding it to governance_check needs a guardrail band, which would be an invented number, so it was left out. The hydrator copy also lacks CANDIDATE_MIN_BULLISH_TF (pre-existing).
- Env overrides (`ENTRY_REGIME_MIN_SCORE=...` in compose) are not read by the scheduler copies; the test compares defaults only.

## Run here
notification-scheduler-service: 181 passed (was 178 + 3 new).
