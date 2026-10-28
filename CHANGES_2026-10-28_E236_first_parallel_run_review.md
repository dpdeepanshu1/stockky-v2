# E236 - what the first parallel "Run all" (28 Oct 07:57 IST, Real Trade tab) showed, and two fixes

Read from `latest_results_rt (1).zip` (the E235 run). `latest_results.zip` and `latest_results_rt.zip` are the same older set (27 Oct 18:24, before E235: every run untagged = shared), so they say nothing new.

## E235 worked
- 31 tests started 07:57:04 and the last ended 07:58:33: about 90 s for the whole light plan. Independent tests overlapped (four finished within the same second at 07:57:04).
- Every new run id carries `-rt-` and its output says `tab Real Trade`. Older untagged runs still show as shared.
- Order was respected: calibration_snapshot -> calibration_compare, export_records -> records_report / hold_bucket / entry_slippage.
- Solo tests (shadow_shorts_check, tm_gate_check, shorts_flat_check, overlay_clear_check) ran after strategy_readout, one by one.

## Where the 90 s goes
`strategy_readout` took 81 s (it took 80 s on 27 Oct too, so parallelism did not cause it). Everything else was 0-3 s. It waits on one call: position-stocks `GET /board/shadow-report?days=30` (up to 60,000 rows), and the script gave that call 90 s. More workers cannot shorten this; only a faster endpoint can.

## Changed
- `scripts/strategy_readout.py`: that call may take 240 s (was 90 s, 81 s used); the job's own limit goes 240 s -> 420 s so it can use it. Was one slow day from failing.
- `jobs.py`: `wiring_check` is a SOLO job. It POSTs and DELETEs a stamp-only overlay on the live services, so nothing that reads live state may overlap it. It ran alongside strategy_readout in this run.
- Test: `test_wiring_check_runs_alone`.

## Not code (your VM)
- `tsfm_nightly` / `tsfm_probe` (19-20 Oct): "name resolution" failed for timesfm-forecast-service. It is behind the compose profile `timesfm`: `docker compose --profile timesfm up -d timesfm-forecast-service`. `tsfm_combine` says NO_INPUT until `tsfm_probe` has run with the model.
- `g1_check` (26 Oct, before E229) says Latency FAIL p95 57.7 ms. E229 changed that check to CPU time because of the 0.50-CPU cap, so the result is stale: run `g1_check` once.
- `service_advice`: real-trade TURN OFF (21 trades, net -242, profit factor 0.48, -349 without its best trade); position-stocks has too little data (12 trades over 2 days). A trading result, not a fault.
- NOT_ENOUGH_DATA is correct for: regime shadow report / learner (3 of 20 days), swing shadow (data), strategy_readout entry window (8 of 30 trades), tm_gate_check (0 checks, no live sessions yet).
- A/B: seed 11 passes, seeds 7 and 13 show no edge; calibration_pack says treat every line as a hypothesis.
