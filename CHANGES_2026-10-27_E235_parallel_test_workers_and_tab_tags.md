# E235 - the Calibration test suite runs several tests at once; Real Trade and Position Stocks tests are kept apart (2026-10-27)

Your ask: the tests take too long, run them faster with more workers / instances, and make sure a test started on the Real Trade tab never shows up as a Position Stocks result (and the other way round). The earlier attempt (a work log with patches) never reached the zip, so it is rebuilt here against the E234 code and tested.

## Why it was slow
1. `calibration-runner-service` ran ONE job at a time (one thread, one cancel flag, one process).
2. "Run all test cases" submitted the tests one by one and waited for each to finish.
3. A heavy replay used only 2 processes inside itself, and the container was capped at 0.50 CPU.

## Changed
- **Worker pool** (`runner.py`, `RUNNER_WORKERS`, default 4, 1-8): several runs are active together. `RUNNER_WORKERS=1` is the old one-at-a-time behaviour.
- **No clashes** (`jobs.derive_locks`): lock keys come from each job's own steps (files it reads / writes under the calibration and exports folders, POSTs to a service). Two runs never overlap when one WRITES what the other reads or writes; tests that only read run side by side. A later run never jumps in front of an older waiting run it clashes with, so a waiting writer is not starved.
- **Solo jobs run alone**: the live timing checks (Monday 09:30 / 10:15, shadow shorts, shorts flat, overlay clear, Time Machine gate), the millisecond benchmarks (latency save / compare, G1) and the two real 1-share Dhan probes.
- **Heavy cap** (`RUNNER_MAX_HEAVY`, default 2) and **memory guard** (`RUNNER_MIN_FREE_MB`, default 1500): a second heavy replay waits while memory is short (E229: one replay was OOM-killed at 2200m).
- **Inside one replay** (`RUNNER_POOL_JOBS`, default 3, was 2; the page option now goes up to 8).
- **Per-run cancel**: Cancel stops only that run; shutdown cancels all.
- **Tab tag**: every run carries `service` = `rt` (Real Trade), `ps` (Position Stocks) or empty (shared: the timetable, automatic runs). The run id contains it (`...-target-rt-a1b2`), the output txt says `tab Real Trade`, and a tab sees its own runs plus the shared ones, never the other tab's. Applied to Runs list, the per-test "last result" line, the queue, "Download all latest results" (`latest_results_rt.zip` / `_ps.zip`), duplicate-in-queue check and keep-latest-only clean-up (one kept result PER TAB).
- **Queue** 20 -> 60; a waiting run says why it waits (`waiting_for` in the state).
- **docker-compose**: calibration-runner `cpus` 0.50 -> 1.50, `mem_limit` 2200m -> 6000m, four `RUNNER_*` variables. The trading containers keep their own caps.
- **Frontend**: the tab (Real Trade / Position Stocks) is passed to every runner call; "Run all test cases" starts several tests at a time (as many lanes as the runner has workers, max 4) and Stop cancels every test that is going; the running dock, banner and Tests cards show every active run, "N of M slots busy", and each run in Runs & downloads has a tab badge.

## Tests
- New: `tests/test_e235_parallel_workers.py` (15) and `tests/test_e235_routes.py` (2). Runner suite under real pytest: 273 passed before these two files, 17 new passed.
- Frontend: `npx tsc --noEmit` clean, `npm test` chain green (`calibration.test.mjs` assertions updated for the lanes and the tab argument).
- Not run here: `docker compose build`, a real parallel run on your VM, a browser look.

## Deploy
```
cd ~/stockky-v2
docker compose build calibration-runner-service frontend
docker compose up -d
```
Back to one at a time without a code change: `echo 'RUNNER_WORKERS=1' >> .env && docker compose up -d calibration-runner-service`.
If the kernel log (`dmesg`) shows OOM kills of python3 again, lower `RUNNER_MAX_HEAVY` to 1 or `RUNNER_POOL_JOBS` to 2.

## Left as is
- Runs finished before this change have no tab; they show as "Shared" and both tabs see them.
- The worker count (1-8) can be changed with the + and - next to "N of M slots busy" on the page (saved on the runner, PUT /runner/workers); RUNNER_WORKERS is only the first default.
