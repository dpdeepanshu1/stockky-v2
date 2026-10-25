# E138 - swing lane: Oracle bug that stopped the scan, a day-5 exit backstop, an entry gate and a wiring check (2026-10-25)

Audit of `Stockky_Swing_Momentum_Plan.md` against the E137 tree: every plan item was traced from its code to the place that calls it. Nearly all of it is built (E130-E137); this round fixes what would have stopped it working live. Ships in **shadow**; nothing is bought until `SWING_MODE=live`.

## Found and fixed
1. **Oracle: the evening scan could never start (real bug).** `swing/repo.py` (`latest_run`, `has_run`) and `GET /swing/snapshots` filtered with `.is_(True)`, which Oracle renders as `IS 1` (ORA-00908). `scan_due` swallows errors and returns false, so on the Oracle VM no scan would run and no shadow evidence would be recorded. Now `== True` (`= 1`), the form group 150 already requires (`tests/test_oracle_boolean_filters.py` scans for it).
2. **A swing position could be held by its stop alone.** The exit engine deliberately has no target or time-stop for swing positions, so the 15:00 square-off was the only way out. It runs once a day, only with the gate's EOD square-off switch on, and a refused sale (CDSL TPIN, rejection, network) was never retried. New backstop (`exit_engine/exit.py` block 1b, `swing.live.overdue`, pure rule `lane.backstop_verdict`): the exit tick sells (reason `swing_max_hold`) when sessions > `SWING_MAX_HOLD_DAYS`; or on the last session once 10 minutes after `EOD_SQUAREOFF_TIME_IST` (never later than 15:25) has passed with the position still open; or when a position carried from an earlier day is open while `SWING_MODE` is not live. Any doubt (no entry day, no session count, an error) = no sale. The stop and the emergency check still run first; the pending-sell guard stops a second SELL; a refused REAL sale is retried on the next tick.
3. **Live entries need the EOD square-off switch.** `swing.live.entries` returns `eod_squareoff_off` (and does not use up the day's entry run) when the gate's EOD switch is off or unreadable.
4. **`GET /swing/status` has a `wiring` block**: schedule loop running, per mode gate armed / EOD switch / eDIS check / live entries allowed / open swing positions, broker-stop switches, newest scan date. `wiring.problems` is empty when nothing would stop the lane.
5. **`swing_shadow_report` was a "weekly" job in no pack**, so nothing ran it. It is now in the Friday review pack (read-only).
6. **`docs/ENV_REFERENCE.md` was already stale in E137** (five swing variables missing; CI step "ENV_REFERENCE.md is current" would fail). Regenerated.
7. `.env.example` documents every `SWING_*` setting (all commented).

## Checked here
Run for real (stdlib only): the backstop rules and `live.overdue` against the real NSE calendar (entry Wed 21 Oct 2026, holiday Tue 20 Oct; sessions 4 / 5 / 6 / not-live cases); 4 deliberate breakages (past-limit rule, last-session cutoff, not-live leftover, cutoff cap) each fail the new tests; existing `test_swing_lane.py`, `test_swing_momentum.py` and `scripts/test_swing_backtest.py` still pass as plain scripts; `py_compile` of every changed file; guards `check_control_catalog`, `check_shared_copies`, `check_table_name_collisions`, `check_secrets`, `check_merge_conflicts`, `check_requirements_drift`, `gen_env_reference --check` pass.

**NOT run here (no network in this sandbox, so no pytest / SQLAlchemy):** the database tests in `tests/test_swing_e138_wiring.py` (exit-engine backstop in DEMO and REAL, the entry gate, the wiring block, the Oracle filter), the full real-trade suite, the runner tests, Docker. Run on the VM:
`cd services/real-trade-service && python3 -m pytest -q tests/test_swing_e138_wiring.py tests/test_swing_lane.py tests/test_swing_momentum.py tests/test_oracle_boolean_filters.py tests/test_exit_evaluate_mode.py`

## Not changed (needs your decision or VM evidence)
`SWING_MODE` stays `shadow`; `SWING_LIVE_REAL_ENABLED` stays false; the broker-stop probe, the replay on your real history and the shadow evidence are still to be run (see the remaining list in the reply). No frontend page for the swing lane exists yet (`/swing/status` is API only).
