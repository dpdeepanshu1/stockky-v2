# E129: `pytest scripts` stuck at 14% on the VM (virtualenv scanned)

The third VM run of `python3 -m pytest scripts -q` stopped making progress after 87 tests (14%) and never finished. Test 88 is `test_control_kit.py::test_real_catalogs_are_valid_and_pass_the_static_check`, which runs `check_control_catalog.main()`.

| Cause | Fix |
| --- | --- |
| `scripts/check_control_catalog.py` listed service files with `svc.rglob("*.py")` and skipped only a folder named exactly `.venv`. On the VM a virtualenv with another name (for example `.venv-test`) was read file by file, with every installed package. `scripts/check_table_name_collisions.py` had the same plain `rglob` over all of `services/`. | Both now use the shared `_repo_walk.walk_py`, which prunes `.venv*`, `venv`, `site-packages`, caches and any folder holding `pyvenv.cfg`, the same rule as the other repo scanners. |

Checked here: `check_control_catalog.py` (2 catalogs ok), `check_table_name_collisions.py` (no clashes), `check_shared_copies.py` (no new drift), `test_control_kit.py` run as a plain script (all ok). Not run here: pytest itself (not installed). No trading code changed.

If the VM run still stalls: `python3 -m pytest scripts -q -o faulthandler_timeout=60` prints the stack of any test that hangs for 60 s; send that trace.

VM housekeeping done in the same session (not in the zip): stale `.env.bak_before52w` deleted (it lacked only the newer `CANDIDATE_52W_LOG_ONLY`), the two `stockky_test_report_*` folders deleted, the test fixture fingerprint `7da0722e80af` allowlisted in `scripts/secrets_allowlist.txt`. `check_secrets.py` now lists only `.env`, which is expected. Frontend check: all node tests ok.
