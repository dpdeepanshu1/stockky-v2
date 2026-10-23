# E54 - fetch_history job pointed at the wrong service

Found in `latest_results.zip` (23 Oct): the `fetch_history` run printed `saved=0 kept=111 failed=289` (72%) for valid NSE names such as AARTIIND, ADANIGREEN, AMBUJACEM, and finished in 88 s (far too fast for 289 retried downloads).

Cause: `calibration-runner-service/jobs.py::_fetch` passed `--base <api-gateway>`. The script requests `<base>/history/<SYM>`; the gateway only has `/market/history/<SYM>`, so every symbol not already on disk got an instant 404. The 111 "kept" were files from an earlier run.

Fix:
- `jobs.py`: `--base` is now the market-data service (`ctx["md"]`); the symbol list still comes from the gateway `/api/universe`.
- `scripts/fetch_history_for_backtest.py`: the summary JSON now has `failed_reasons` (http_404 / http_400 / http_5xx / network / empty / short_history), and a run with nothing saved and only 404s prints a HINT naming the `--base` mistake.
- New test: `services/calibration-runner-service/tests/test_e54_fetch_history_base.py`.

On the VM: rebuild the calibration-runner, then run `fetch_history` (seed 7). `saved` should be about 289 and `failed_pct` low; then re-run `g1_check` so "Corporate actions" gets >= 10 cases.

Not changed: G1 latency (p95 57 ms vs 15 ms offline budget) is a speed finding, not a bug; the live check (`latency_compare`) passed at p95 5.0 ms.
