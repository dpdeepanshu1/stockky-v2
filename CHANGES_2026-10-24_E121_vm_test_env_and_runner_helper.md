# E121: the VM test run installs what the suites need; a stuck runner job now says why

## What the first VM run showed (24 Oct 2026, stockky_test_report_20261024-084904)
- PART 0 (static guards, short-live and short-exit behaviour tests) and the calibration-runner suite passed (239 passed, 1 skipped).
- real-trade-service: 163 errors **during collection**, position-stocks-service: 90 errors during collection. No test ran. Cause: `stockky_full_test.sh` installed only `pytest` / `pytest-cov` into the VM's system Python; these two suites import fastapi, sqlalchemy, dhanhq, oracledb ... (the services run in Docker, so the host never had them). The runner suite is stdlib-only, which is why it passed. This was a test-environment gap, NOT evidence of a code bug.
- The calibration runner took the T1 / T2 requests but they stayed `queued` for 10+ minutes with an empty tail. The runner has one worker, so a job waits while another run executes. The old helper could not say what was blocking.

## Changes (no production code touched)
- `stockky_full_test.sh`: new `prep_pytest` builds `.venv-test/<service>` per service, installs that service's `requirements.txt` plus pytest / pytest-cov, and runs the suite with that interpreter (no `venv` module -> the system Python as before, with the requirements). New `why_failed` prints the distinct import / collection causes when a suite fails, so a wall of ERROR lines is readable. Install output goes to `coverage/venv-<service>.log`.
- `scripts/vm_runjob.sh` (new, `source` it): `runlogin`, `runjob '<json>'`, `runstate`, `runcancel <id>`. `runjob` polls the run's real `status` field, after 30 s still queued it prints what the runner is busy with, gives up after `RUNJOB_MAX_S` (900 s) without cancelling anything, and prints the tail.
- `.gitignore` / `.dockerignore`: `.venv-test/`.

## Checked here
`bash -n` on both scripts; the venv helper and `why_failed` dry-run with an empty requirements file; `runjob` tested against a fake runner that stays queued, then succeeds (waits, prints the busy-runner state at 30 s, prints the tail). NOT checked here: a real install of the two services' requirements (the sandbox has no network) and the real runner. First VM run will show it.
