# E240 - two heavy replays no longer start together and kill the calibration-runner (2026-10-28)

## What the results showed
`latest_results_ps.zip` / `latest_results_rt.zip`: every Real Trade test passed or gave a normal verdict. Eight Position Stocks runs ended `failed` in two groups, none with a result:
- 08:20:40: `g1_check`, `structure_backtest`, `gate_check` (no output at all, still queued).
- 08:26:22: `p8_retest`, `ml_v2` (both started 08:20:46, 3 pool workers each; `ml_v2` was at 10/200 symbols), `ab_report`, `intraday_gates`, `wiring_check`.
The runner marks a run `failed` with "the runner restarted while this run was active" only when the container went down. Two heavy replays at about 780 MB per process (parent + 3 workers = ~3.1 GB each) do not fit in the 6000 MB container.

## Cause
The E235 memory guard looked at the memory free AT THE MOMENT the second heavy run was about to start. A replay ramps its memory up over minutes, so two replays started in the same second both saw plenty of room. `wiring_check`, `ab_report` and the rest did not fail themselves: they were waiting or running next to the two replays when the container died.

## Changed (calibration-runner-service)
- `runner.py`: a second heavy run now starts only when `container limit - memory the running heavy runs will use - this run's memory >= RUNNER_MIN_FREE_MB`. A heavy run's memory = (its "Parallel workers" option, else RUNNER_POOL_JOBS, + 1) x `RUNNER_HEAVY_PROC_MB`. The limit is read from the cgroup (`memory.max`); if none is readable the old checks apply. The page shows why a run waits: "waiting for memory: ...".
- `config.py` / `docker-compose.yml`: new `RUNNER_HEAVY_PROC_MB` (default 800).
- With the shipped numbers (6000 MB, 3 workers) two default replays no longer overlap; a replay with "Parallel workers" = 1 can run next to one with 3 only if the limit allows (it does not at 6000 MB: 3200 + 1600 + 1500 > 6000). Raise `mem_limit` in `docker-compose.yml` (the VM had ~9 GB free) or lower `RUNNER_POOL_JOBS` to run two at once.
- Tests: `tests/test_e240_heavy_memory_reserve.py` (6) plus the E235 runner tests, 22 of 22 under a stand-in runner (no pytest in the sandbox); not run under real pytest.

## Not a fault, no code change
- `g1_check` on the Real Trade tab still shows the 26 Oct run (latency p95 57.7 ms against a 15 ms budget on the offline replay); a fresh run on the Position Stocks tab never completed because of the crash. Run it once after deploy.
- `service_advice`: real-trade-service TURN OFF (21 trades, net -242, profit factor 0.48), position-stocks NOT ENOUGH DATA. A trading result, not a test fault.
- Waiting for data: regime learner, swing shadow, TM gate (0/10 checks), short shadow (3 of 100), opening replay (< 30 trades), entry window (8 of 30).

## Deploy
```
cd ~/stockky-v2
docker compose build calibration-runner-service
docker compose up -d
```
Then re-run on the Position Stocks tab: `g1_check`, `structure_backtest`, `gate_check`, `p8_retest`, `ml_v2`, `ab_report`, `intraday_gates`, `wiring_check`. The second heavy one should show "waiting for memory" until the first finishes. If `dmesg` still shows OOM kills of python3: `echo 'RUNNER_MAX_HEAVY=1' >> .env`.
