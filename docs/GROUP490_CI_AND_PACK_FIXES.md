# group490 - CI failures and the calibration-pack crash

## 1. CI: `test_no_numeric_env_read_can_crash_on_a_blank_value`
`real-trade-service/resilience/circuit_breaker.py` (group 485) read `RT_BREAKER_PROBE_ALERT_REPEAT_S` with a bare `float(os.getenv(..., "1800"))`,
which raises on a blank value (the `.env` line `RT_BREAKER_PROBE_ALERT_REPEAT_S=`). It now uses the blank-safe form (blank or garbage -> 1800).
New test `test_probe_alert_repeat_setting_is_blank_and_garbage_safe`.

## 2. CI: `check_shared_copies.py` drift on `circuit_breaker`
Only the real-trade breaker sends Telegram alerts (the other four copies only log), so the group 485 throttle is a per-service
difference on purpose, not a fix missing from the twins. It was accepted with `check_shared_copies.py --update`; the baseline
changed in exactly two entries (`CircuitBreaker` for real-trade, new `_probe_alert_repeat_s`).

## 3. Runner log: `calibration_pack.py` JSONDecodeError traceback
`backtest.json` is the redirected output of the walk-forward job. A walk-forward that was killed or failed leaves an EMPTY file, the runner's
input check only tested that the file exists, and the pack script crashed with a traceback (job shown FAILED).
- `scripts/calibration_pack.py`: an unreadable, empty or non-JSON `--backtest` / `--ab` file now ends with one clear line naming the file and the job to re-run.
- `calibration-runner-service/runner.py`: an EMPTY `.json` input is now "input missing ... is empty", i.e. the amber "needs data first" state, not a script run.
- Tests: 4 in `scripts/test_calibration_pack.py`, 1 in `tests/test_group470_fixes.py`.
- To get a real result: run the **Structure rules walk-forward** job first (and let it finish), then the pack job.

## 4. Found while checking: `test_every_rule_points_at_a_real_switch_and_a_real_test` (calibration-runner)
Failing since group 481: `SIZE_LADDER_MAX_LEVEL` is correctly in `PROTECTED` but is an env setting read in real-trade's `config.py`, not a control-panel
`Flag`, and the test only accepted catalog flags. The test now also accepts a key the service config really reads via `getenv("KEY")`; a typo is still caught.
