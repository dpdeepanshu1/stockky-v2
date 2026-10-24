# E122: VM test run on a newer Python - the two remaining test-environment faults

## What the second VM run showed (stockky_test_report_20261024-095751)
- `wiring_check`: WIRING: PASS on both services (MARKET_STATE_MODE live, 5 and 4 dials overlayable, overlay accepted and cleared, SHORT_MODE shadow, /short/shadow answers).
- position-stocks-service: **4307 passed, 12 failed**. All 12 are in `tests/test_angelone_session.py` (the `TestLogin` and `TestEnsureSession` classes). Cause: the tests drive the async code with `asyncio.get_event_loop().run_until_complete(...)`. The VM's Python (python3-venv 3.14.x) no longer creates an event loop for that call and raises. The production code is not affected: every `get_event_loop()` call in the services sits inside an `async def` (a loop is running), and the Docker images are `python:3.11-slim`.
- real-trade-service: its requirements did not install: `psycopg2-binary` has no wheel for that Python and needs libpq to build, and pip aborts the whole install on one failure, so pytest itself was never installed in that environment either.
- Parts 2 and 3 of the script (DEMO scenarios, read-only diagnostics) failed to log in because the command was run with the placeholder `ADMIN_PASS='yourpassword'`.

## Changes (tests and the test script only; no production code touched)
- `position-stocks-service/tests/test_angelone_session.py`: both `_run` helpers use `asyncio.run(coro)`, which works on every Python version.
- `stockky_full_test.sh` `prep_pytest`: pytest / pytest-cov install first and on their own; if the requirements file does not install as a whole, it is retried line by line, what installs is kept, and the skipped packages are printed (details in `coverage/venv-<service>.log`). A suite that really needs a skipped package then fails with a clear `No module named ...` cause.

## Checked here
`bash -n`; the line-by-line retry dry-run with an unbuildable requirement (it keeps going and names it). NOT checked here: the Angel One tests themselves (the sandbox has no httpx / pyotp and runs Python 3.12, where the old call still works), so the 12 are confirmed fixed only by the next VM run. The real-trade suite has not run yet on the VM; its first result is still to come.
