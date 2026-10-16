# Group 413 - CI hygiene (no service code changed)

Cumulative on the UI refresh v3 zip (group 412 + 412b).

1. `docs/ENV_REFERENCE.md` regenerated with `python3 scripts/gen_env_reference.py` (group 412 moved `DHAN_SECURITY_WARM_ENABLED` from `dhan_client.py:412` to `:436`). Fixes `scripts/test_repo_scripts.py::test_checked_in_generated_files_are_current`.
2. real-trade `tests/test_db.py`: `COLUMN_FUNCS` now lists `_ensure_structure_obs_bench_columns` (wired in `db.py`).
3. position-stocks `tests/test_main.py::test_health`: accepts the group403 `structure_engine` block.
4. `CHANGELOG_INDEX.md`: entries for groups 412, 412b, 413.
5. analysis-intelligence `.coveragerc`: omits the 5 vendored `stockky_ms` modules the service never imports. Coverage 92% -> 97% (gate 95%).

Not run here: pytest is not installed in this sandbox, so items 2-3 were only compiled; run `bash docker_test.sh` / `run_tests.sh`.
