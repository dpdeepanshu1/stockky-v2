# E223 - ORB counter check reads the right endpoint; triage of latest_results.zip

## Fixed
- `scripts/intraday_state_check.py` (T5 shadow-shorts): warned "real-trade /health carries no orb.bars counters (E113 not running?)" on every run (seen in shadow_shorts_check 14:14 and live_check_open 10:19). real-trade's public `/health` never carried them; they are on the admin-only `GET /review-fixes/status` (`"orb": {..., "bars": ...}`). The check now falls back to that route (the script already sends the admin token) and only warns when neither has them, with a wording that names a missing/expired token.
- `scripts/test_intraday_state_check.py`: +1 test (counters read from `/review-fixes/status`; 401 there still only warns). 9/9 non-fixture tests pass under a stand-in runner; `test_main_exit_codes` needs pytest's capsys and was not run here.

## Results triage (latest_results.zip, no code change needed)
- tsfm_nightly (754 failed, name does not resolve): that run is 2026-10-20 00:03, before group 470's `service_not_running` skip; the current script skips cleanly. Re-run it to clear the ATTENTION.
- G1 latency FAIL (p95 ~57 ms vs 15 ms budget), G2/G3 FAIL (A/B no_edge, policy keeps 9.5% of signals), G4 / board parity / regime shadow / TM gate / short shadow NOT_ENOUGH_DATA: findings of the data, not code faults. Nothing to tune; keep switches as they are.
- ab_report 17:36 FAILED: log stops at 41/838 symbols after ~15 min, no traceback, and ab_7 was already complete (calibration_status: all seeds complete). Re-run is only needed if you want a fresh seed 7.
- service_advice: real-trade-service TURN OFF (20 trades, net -216, PF 0.51, 10-21 alone -217); position-stocks NOT ENOUGH DATA. It is a recommendation; pausing is your call.
- monday_check WARN depth20/order-WS "unverified" at 09:30/10:15: nothing parsed yet; the 14:11 re-run showed observed=0 (market closed at that time of the re-run).
- friday_review `ZETWERK: HTTP 404` is one upstream symbol, not ours.

## Reviewed, not changed (E222 "next unread")
- `short_gates.evaluate` walk (time, price, range, gap, vwap, circuit, depth, board, structure, quality): sign conventions match the long gate (`ms_stretch_atr` = (price-EMA20)/ATR, board side `bear`), config defaults agree with the long structure gate, a raising gate refuses (fail closed). No fault found.
- `reconcile.run_exit_reconciliation`: every P&L it books goes through `_side.pnl`, so a short books (entry - exit). No fault found. The 1,000-line body was searched for P&L, side and exit-leg handling, not read line by line.

## Run on your VM
`bash scripts/docker_test.sh position-stocks-service`; re-run tsfm_nightly and shadow_shorts_check (the WARN should be gone, or name the token).

## Next unread
`short_entry.plan_short` (stop/target/sizing for shorts), `short_exit` stagnation path.
