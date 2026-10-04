# group134 (2026-10-04) - repo-wide source-sweep tests failed on the VM because they walked .venv

Cumulative on group133. Tests only; no application code changed, nothing to rebuild.

## What the VM run showed
`services/api-gateway` suite on the VM: 8192 passed, 2 failed
- `test_env_flag_sweep.py::test_no_default_on_env_flag_reads_a_blank_value_as_off`
- `test_env_numeric_sweep.py::test_no_numeric_env_read_can_crash_on_a_blank_value`
Offenders listed were all under `real-trade-service/.venv/lib/python3.12/site-packages/...` (pandas, coverage).

## Cause
Both sweeps walk every service's source (`os.walk(services/)`) and skipped only `tests`, `__pycache__`, `node_modules`, `.git`. The VM has a virtualenv inside `services/real-trade-service/`, so third-party packages were scanned as if they were Stockky code. The sandbox has no in-tree venv, which is why they passed here. Not a bug in Stockky's own env reads.

## Change
Every tree-walking source guard that lacked it now skips `.venv`, `venv`, `site-packages`, `.tox`, `build`, `dist` on top of the old four: `test_env_flag_sweep.py`, `test_env_numeric_sweep.py`, `test_db_url_normalizer_drift.py`, `test_kv_cache_drift.py` (two walkers). The `blank`, `secret` and `oracle` sweeps already skipped venvs. The "scan covers > 200 files" guards are unchanged, so the sweeps still scan the real source.

## Verified
Reproduced here with a fake `real-trade-service/.venv/.../site-packages/pandas/cfg.py` containing an offending `os.environ.get(..., "1")` and `int(os.getenv(..., "5"))`: the flag sweep failed with the old skip set, and all four patched files pass (108 passed) with the fake venv present. No real source directory is named build/dist/.tox/site-packages, so nothing real is hidden.

## Running all four suites
Your last command chain ran only the first suite: the `cd services/api-gateway` left the shell inside that folder, so the next three relative `cd` calls failed. Use subshells:

    cd ~/stockky-v2/services
    for s in api-gateway real-trade-service market-data-service analysis-intelligence-service; do (cd $s && python3 -m pytest tests -q -p no:cacheprovider 2>&1 | tail -3); done

## Also from the group133 report (not changed here)
- B2: HFCL, MTARTECH and STLTECH exist in AngelOne's scrip master only as `-BE` (trade-to-trade) NSE rows, so the feed cannot resolve them and they fall through to Yahoo. Next item: decide whether the token lookup should accept `-BE`.
- A3: the saved row had expired by the time of the report (it was written before group 132's 7-day TTL); the next boot's save uses 7 days.
