# GROUP339 baseline (written in group 369)

What was actually run (sandbox without pytest, fastapi, sqlalchemy, network):
- `shared/market_structure/tests/run_plain.py`: 97 passed, 0 failed.
- `scripts/gen_technical_baseline.py`: legacy /analyze fields equal golden (35 cases).
- `scripts/check_shared_copies.py`: OK, no new drift (147 known differences on record).
- `scripts/sync_market_structure.py --check`: was DRIFT (stale MANIFEST.sha256 in 5 copies), fixed by `--sync`; now ok.
- `scripts/gen_env_reference.py --check`: was out of date, regenerated; now ok.

NOT run (record real counts here after your CI run): position-stocks (3.11/3.12), real-trade (3.11/3.12), market-data, analysis-intelligence
(`./run_tests.sh`, coverage >= 95), api-gateway, notification-scheduler, decision-prediction, frontend `npm run build`.

| Suite | passed | failed | date |
|---|---|---|---|
| VM run, 7 of 8 suites, 2026-10-15 (before the group378 test-harness fix; the 2 failures were `test_group368_analyze_baseline.py`, fixed in group378) | 21909 | 2 (+1 skipped) | 2026-10-15 |
| still to record from your VM / CI: frontend `npm run build`, analysis-intelligence `./run_tests.sh` coverage gate (>= 95), Python 3.12 for position-stocks and real-trade | | | |
