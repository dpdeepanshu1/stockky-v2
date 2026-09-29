# Session 123 — analysis-intelligence-service: root `main.py` (2026-09-29)

## File added / changed

### `services/analysis-intelligence-service/tests/test_service_main.py`

Covers the service entrypoint `main.py` (mount loop, `MOUNT_STATUS`, `_load_subapp`,
`_mount_summary`, `GET /`, `GET /health`, CORS). Most tests copy `main.py` next to five
tiny fake sub-app folders (ok / no `app` / import crash / syntax error / missing folder /
missing file). One smoke test runs the REAL `main.py` and asserts all five real sub-apps
mount; `Snapshot` restores `sys.modules` / `sys.path` / cwd after every test.

Coverage-report hygiene: the temp trees are created under `tmp_path/"__pycache__"` so the
`*/__pycache__/*` omit in `.coveragerc` keeps the throwaway `main.py` copies and fake
sub-apps out of the report (previously ~152 `/tmp/...` rows). Last measured run showed
`main.py` at 63 stmts / 0 missed / 100%.

Test fixed vs. the first draft: `_load_subapp` filters only the five sibling sub-app
folders, NOT `''` (cwd). The old test asserted `""` was hidden and would have failed.
Split into `test_sibling_subapp_folders_are_hidden_while_loading` and
`test_empty_sys_path_entry_is_left_alone_while_loading` (pins current behaviour).

## Verification status

NOT re-run after the final edits: this sandbox had no pytest/fastapi and no network. The
file compiles (`py_compile`) and was checked by hand against `main.py`. Re-run:

    python3 -m pytest tests/test_service_main.py -q -p no:cacheprovider --cov --cov-report=term-missing
    ./run_tests.sh

## Status of the session-122 roadmap

`news/main.py` -> `fundamental/main.py` -> `technical/main.py` -> `event/main.py` ->
`main.py`: every one now has a test file, so the planned list is finished.

## Next

Run `./run_tests.sh` for a real combined coverage table and work the remaining gaps from
that (a static name scan found no untested top-level functions; only nested helpers /
route handlers such as `rate_limit_report._kv_get/_kv_set`, `fundamental/main._val`,
`oracle_compat._set_call_timeout`, `technical/main._get_return` are not named in tests,
and may already be covered indirectly).
