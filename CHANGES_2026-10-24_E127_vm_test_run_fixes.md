# E127: fixes from the VM test run of 24 Oct (after E125)

VM result: position-stocks 4321, real-trade 5309 + 1 skipped, market-data 1733, analysis 2643, decision-prediction 387, notification 214, frontend type-check + build all green. Four things were red, all in the test helpers, none in trading code:

| Red | Cause | Fix |
| --- | --- | --- |
| calibration-runner-service: `ModuleNotFoundError: httpx` at collection | `docker_test.sh` installs only the service's requirements; the runner does not list httpx, and `fastapi.testclient` needs it | `scripts/docker_test.sh` adds `httpx<0.28` when the service does not list httpx (the runner pins fastapi 0.104 / starlette 0.27, whose TestClient breaks on httpx 0.28) |
| api-gateway: 4 failures in `test_dockerignore.py` | the VM's test virtualenvs are called `.venv-test`; the test only knew `.venv` / `venv`, so 13,282 virtualenv files counted as "needed files dropped" | `_expendable` treats any `.venv*` / `venv` folder at any depth as expendable, and the walk prunes `.venv-test` |
| frontend: `calibration.test.mjs` ENOENT `/services/calibration-runner-service/jobs.py` | the test reads that file, but the Docker run mounted only `frontend/` | `docker_frontend_check.sh` mounts that one file read-only at the path the test uses |
| `python3 -m pytest scripts`: 4 collection errors | three standalone short-selling scripts (`test_plain_short`, `test_short_exit_paths`, `test_short_live`) must run in their own interpreter from `services/position-stocks-service`; `test_angelone_movers` needs httpx | new `scripts/conftest.py` leaves them out of direct collection (httpx one only when httpx is missing); new `scripts/test_standalone_behaviour_scripts.py` runs the three for real as subprocesses (61 + 75 + 67 cases) |

Not bugs (from `latest_results.zip` and the log):
- `ab_report` and `calibration_full` FAILED at 16:49 / 17:14: both were cut off by your `docker compose down` / `up --build` while they were running (their output ends mid-replay; `--resume` keeps the finished symbols). The runner already notes "the runner restarted while this run was active". Re-run them after the restart if you want the result.
- `monday_open` FAILED at 14:47 on Saturday: that run started before E125 reached the VM (E125 went in at about 17:00). At that time `services_health` still counted TimesFM as failed. The 17:00 `services_health` now reads step 9/9 `success (skipped (not running, optional))`, so that cause is gone. Its own output was not in the zip; run `runjob '{"kind":"pack","target":"monday_open","params":{}}'` once from `~/stockky-v2` to confirm it is green.
- `g1_check` / `calibration_full` G1 latency "FAIL 57 ms" is the offline replay; the live check (`latency_compare`) passes (p95 6.59 ms).
- `source scripts/vm_runjob.sh` said "No such file": the shell was in `~`, not `~/stockky-v2`. Run `cd ~/stockky-v2` first.

Tests: `scripts/test_docker_helper_scripts.py` (4), `scripts/test_standalone_behaviour_scripts.py` (3), api-gateway `test_dockerignore.py` re-run with fake `.venv-test` folders present (90 passed). Not run here: the Docker runs themselves.
