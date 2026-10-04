# group97 (2026-10-04) - item 24: stale DEMO open-positions snapshot (snap_as_of 2026-09-21) re-reported on every boot

Cumulative on group96. In `services/real-trade-service` run `python3 -m pytest tests -q` (there is no `run_tests.sh` in that folder; the group95 note named one by mistake), then `docker compose build real-trade-service && docker compose up -d`.

## Cause
`resilience/local_cache.py::snapshot_open_positions` is written only from `cycle_runner.run_cycle_core`. `auto_pilot._full_tick_body` returns before it when the gate is disarmed or auto-pilot is off, or the market is closed, so a mode that is switched off never refreshes its snapshot. DEMO was in that state: its snapshot stayed at 2026-09-21. Positions closed or changed since then (manual close, reconcile) differed from it, and `reconcile_on_startup` wrote the same `RECONCILE_MISMATCH` audit row and warning on every boot, even though the first one had already recorded the drift. This is the same class as the 2026-09-16 empty-list bug, reached through a switched-off mode instead.

## Fix (`resilience/local_cache.py`)
After `reconcile_on_startup` logs and audits a mismatch for a mode, it re-saves that mode's snapshot from the live OPEN/PARTIALLY_CLOSED rows. The drift is reported once and the next boot compares against today. A matching mode is not touched. The DB position rows are never changed. If the re-save fails it is logged and the old snapshot stays (the previous behaviour); the other mode is still checked.

## Also fixed: manual after-hours scan with nothing scored (found by the first real pytest run of group95)
`TestScanTelegramGating::test_scan_that_finds_nothing_is_silent_unless_manual` failed. group95 said a manual scan always replies, but `afterhours_scan.run_afterhours_scan` returns early when nothing scored, before any message. `Run Now` therefore got no Telegram reply in that case. Now a manual pass with nothing scored sends one short message with the reason (the same text as the log line); scheduled passes stay silent as in group95. No other change to scan behaviour.

## Not changed / not verified
- **Other ways the snapshot can drift:** a manual close, reconcile or holdings sync while the mode is switched off still does not refresh the snapshot. The first boot after such a change reports once, then re-baselines.
- **The first mismatch after a long gap still appears once** (audit row and warning). That is the intended visibility.
- **Whether DEMO should be armed:** not a code question. A DEMO with open positions and the gate off already gets the existing "Auto-Pilot not evaluating exits" alert.
- **Live behaviour not seen:** I have not seen your VM's audit log, so I cannot confirm that the 2026-09-21 mismatch is exactly this drift.

## Tests
`tests/test_local_cache.py::TestReconcileReportsDriftOnce` (7): second boot clean, re-baseline matches live rows (including PARTIALLY_CLOSED and qty), zero live positions, a matching mode is untouched, only the drifted mode is re-baselined, a failed re-save is non-fatal and keeps the audit row, and the other mode is still checked. Run here: `test_local_cache.py` 39 passed; full `real-trade-service` suite 2948 passed, 1 skipped, which includes the group95 tests for the first time.
