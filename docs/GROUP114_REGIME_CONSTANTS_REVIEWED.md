# group114 (2026-10-04) - item 25: regime constants reviewed, all six values kept, review dates moved

Cumulative on group113. Rebuild: `docker compose build real-trade-service notification-scheduler-service && docker compose up -d`.

## What the owner decided
Keep all six values exactly as they are and move their review dates to today (2026-10-04). This is the owner's review, recorded at his instruction; nothing in the code decided it.

| Constant | Value (unchanged) | Review date |
|---|---|---|
| `ENTRY_REGIME_MIN_SCORE` | 25 | 2026-10-04 (was 2026-09-03) |
| `ENTRY_MIN_REWARD_RISK` | 2.0 | 2026-10-04 (was 2026-09-03) |
| `CANDIDATE_MIN_CONVICTION` | 55 | 2026-10-04 (was 2026-09-03) |
| `CANDIDATE_MIN_BULLISH_TF` | 4 | 2026-10-04 (was 2026-09-03) |
| `CANDIDATE_DOWNTREND_6M_PCT` | -10.0 | 2026-10-04 (was 2026-09-03) |
| `CANDIDATE_OVEREXTENDED_52W_TOP_PCT` | 12.0 | 2026-10-04 (was 2026-09-03) |

## Changes
- `real-trade-service/adaptive_thresholds.py`: the six dates in `_REGIME_CONSTANTS`.
- `real-trade-service/config.py`: the five `LAST_REVIEWED` comments, plus a "RE-REVIEWED 2026-10-04" line in the regime-dependent block. Comments only; no value changed.
- `notification-scheduler-service/scheduler/governance_check.py` (5 constants) and `scheduler/weekend_hydrator.py` (4 constants): dates moved from 2026-08-28 to 2026-10-04 so the scheduler's warnings agree with real-trade-service.
- `real-trade-service/tests/test_adaptive_thresholds.py`: docstring mention of the date only.

## Behaviour to know
- No trading behaviour changes: no value, gate or threshold moved.
- The "Stale trading thresholds detected" Telegram notice, the weekly scheduler warning and the governance alert's stale section all stop until the dates reach 30 days old (about 2026-11-03). Dates are the only thing they read.
- `/adaptive/status` will show `age_days` 0 for all six on the day of the deploy.
- The next review is due by 2026-11-03. Config.py's own note still says to review on Nifty crossing 25,500 or monthly on the 1st.

## Tests
`notification-scheduler-service/tests/test_regime_constants_drift.py`: 2 new. The scheduler's governance and hydrator review dates must equal real-trade-service's `_REGIME_CONSTANTS` dates (read as source, no import of real-trade-service). They compare the copies with each other, never with a fixed day, so a later review that misses one copy fails the test instead of leaving a wrong warning. The module docstring is updated to say why.

## Run here
No pytest/sqlalchemy in the sandbox, so the suites were NOT run. All changed files compile, and the new date comparison was executed by hand against the real files: all 6 real-trade dates are 2026-10-04 and the 4 hydrator and 5 governance dates agree. I checked the existing real-trade staleness tests: they monkeypatch the threshold, so none depends on the real dates being stale. On the VM: `bash run_tests.sh` in real-trade-service and notification-scheduler-service.
