# E128: fixes from the second VM test run of 24 Oct (after E127)

E127 worked: runner suite 255 passed, api-gateway 9044 passed, frontend type-check and build green, `calibration.test.mjs` ok. What was still red:

| Red | Cause | Fix |
| --- | --- | --- |
| Frontend `appHelpers.test.mjs`: `TabButton.tsx deleted` | **Not a code bug.** `frontend/src/components/app/TabButton.tsx` and `BackendStatusDot.tsx` were deleted from the project in E70, but laying a zip over `~/stockky-v2` never deletes files, so the old copies still sit on your VM. Nothing imports them (`tsc` passes). The zip does not contain them. | On the VM: `rm -f frontend/src/components/app/TabButton.tsx frontend/src/components/app/BackendStatusDot.tsx` (and `git rm` them if they are tracked). |
| `pytest scripts`: one `F` (the 70th test, `test_the_repo_itself_is_clean`), then the SSH session dropped | The test scanned the whole VM folder, which holds git-ignored local files by design (`.env`, `oracle_wallet`, `data/`, downloaded run outputs such as the runner JWT output). | The test now scans the files git tracks (what a push would publish). Outside a git checkout (a zip) it scans the whole tree as before. `python3 scripts/check_secrets.py` still scans the whole folder: run it, and delete or rotate whatever it lists, before you zip or share the folder. |
| `scripts/test_regime_overlay.py::test_describe_shows_the_overlay...` (found here: it fails when the whole file runs, passes alone) | The mode-switch test left `MARKET_STATE_MODE` in `os.environ`, so the next test saw 1 pending restart | `_setup` now clears `MARKET_STATE_MODE` too. 19 passed, as pytest and as a plain script. |

Still open, not code: `monday_open` and `ab_report` in the results index are the old failures (14:47 and 16:49, before E125 / cut off by the restart); nobody has re-run them yet. `service_advice` and `calibration_full` show ATTENTION only (their verdict lines, not errors).

Tests: `scripts` suite run here without the one slow catalog test: 477 passed; check_secrets tests 10 passed (including a real `git ls-files` listing and the zip fallback).
