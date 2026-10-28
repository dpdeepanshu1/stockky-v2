# E237 - GET /board/shadow-report is fast; strategy_readout no longer waits ~80 s (2026-10-28)

## Cause
`screening/board_report.py` ran a 2,000-resample bootstrap for every horizon of every rule, drawing each value with `randrange` in a generator. On 60,000 synthetic rows the report took about 174 s (E236: ~81 s on the VM), almost all of it in `bootstrap_diff`.

## Changed
- `bootstrap_diff` uses `random.choices` (about 4.5x faster per draw) and a draw budget: `boot_iters()` scales the resamples down for big groups (floor 400, full 2,000 when small). The 30m horizon, which decides the verdict, gets a 3x larger budget. Same method, same seed, same determinism. Synthetic 60,000 rows: 174 s -> 9 s, verdicts unchanged, CI ends moved by 0.001-0.007.
- `main.py`: the report is cached per `days` for `BOARD_REPORT_CACHE_S` (default 300 s, 0 = off). A stale entry is returned at once while a background thread refreshes it; `?fresh=true` recomputes now; the response carries `cache: {hit, age_s, stale}`. The cache is warmed once at startup for days=30 (what `strategy_readout` asks for).
- Tests: `tests/test_e237_shadow_report_speed.py` (7); `tests/test_e125_board_report_columns.py` now reads the query from `_board_report_compute`. Run under a stand-in runner here (no pytest/sqlalchemy/fastapi in the sandbox): all pass; not run under real pytest.
- Docs: `docs/ENV_REFERENCE.md` row for `BOARD_REPORT_CACHE_S`.

## Deploy and check
```
cd ~/stockky-v2
docker compose build position-stocks-service
docker compose up -d
time curl -s "http://localhost:<position-stocks-port>/board/shadow-report?fresh=true" | head -c 300
time curl -s "http://localhost:<position-stocks-port>/board/shadow-report" | head -c 300     # should be near-instant, cache.hit true
```
Send me the first timing if it is still slow: the real table is larger than the synthetic one.

## Cannot be done from the zip (your side)
- CI frontend failure: `TabButton.tsx` and `BackendStatusDot.tsx` are not in this zip but stay in your git (unzipping does not delete). `git rm -f frontend/src/components/app/TabButton.tsx frontend/src/components/app/BackendStatusDot.tsx`, push.
- Deploy the calibration-runner and frontend build; one Position Stocks tab test; TimesFM profile; `g1_check` run; parallel heavy "Run all" with `dmesg` watched (`RUNNER_MAX_HEAVY=1` if OOM); click the +/- worker control once.
