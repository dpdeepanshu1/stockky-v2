# E196 - two CI test failures fixed (wall-clock test bugs) and a Dhan intraday-cutoff rejection now pauses new entries for the day

Includes E195 (and E194 - E184).

## 1. CI failures (position-stocks `tests/test_eod_squareoff.py`, Python 3.11 and 3.12) - test bugs, no product change
- `TestRunEodFailureHandling::test_position_in_cooldown_is_skipped_quietly`: since E183 a position still in its exit backoff is retried inside the EOD retry window and, once the window has passed
  (EOD time + 12 min, judged on the real clock), the sweep raises the "EOD SQUAREOFF GAVE UP" alert. The test did not open the window, so it only passed in the first minutes after the EOD time and failed at any other
  time of day (it failed here too, at 13:08 IST). It now calls `open_retry_window(monkeypatch)` like the other retry-window tests.
- `TestEodRetryWindow::test_a_second_sweep_while_one_runs_does_nothing`: a sweep refused because another holds the lock returns before it reads the gate state, so the `ScalpGateState` row may not exist yet and the
  `fired_date()` helper crashed on `None.eod_squareoff_fired_date`. The helper now returns None when there is no row (= not fired), which is what the test means.
Both reproduced here and pass now (file: 181 passed). Position-stocks suite here: 4,386 passed, 2 skipped, 1 failed (`test_dhan_client.py::TestGetSdkClient::test_valid_creds_returns_client`: `dhanhq` is not installed in this sandbox; it passes in CI).

## 2. attempt_entry read (position-stocks `orders/entry.py`, lines 456-1170) - one finding, fixed
What I read: every gate in order, the symbol-claim release on each early return, capital reserve / release and the size-down steps (book cap, VIX, calendar, opening), minimum quantity and cost gate, the
shortfall top-up, shared order budget, live re-anchor, the Super Order / plain order call, the four rejection branches and the unknown-status branch (E177), the position write, alerts.
Finding: `dhan_client.is_intraday_cutoff_error` says "caller should stop hammering Dhan until the next trading day", and the BUY branch logged "Will retry tomorrow", but nothing stopped the retries: the next
~15 s cycle sent another doomed BUY (each spending the shared order budget). The funds branch already pauses all entries (group209); the cutoff branch did not.
Change: `entry_pause.minutes_left_in_ist_day()` (new, never raises) and `entry_pause.pause_all("INTRADAY_CUTOFF", ...)` until IST midnight in `attempt_entry` and in the live short's `_handle_failure`
(`orders/short_live.py`). The pause is process-local (a restart clears it; one extra attempt at worst). With the default `SCALP_ENTRY_CUTOFF_IST=14:00` the window is rarely reached, so this matters only when that cut-off is moved
late or its clock check fails open. Manual BUY is a person's click and is unchanged. No new environment variable.
Read and fine: lock release on every early return (a leak from an exception is cleaned by `shared_symbol_lock.sweep_stale`), size-down helpers never return 0 (minimum 1), capital release amounts.

## 3. latest_results.zip (reports from the VM) - read, nothing to fix in code
Services health: all OK (TimesFM profile off, optional). Monday check: 0 fail / 0 warn. Weekly calibration: structure gate G2 / G3 FAIL (A/B `no_edge` in 2 of 3 seeds, policy keeps 9.5 % of signals vs 30 % needed, `room` flips sign
between seeds), G4 and board parity NOT_ENOUGH_DATA, G1 latency p95 57.6 ms vs a 15 ms budget. The pack's own text says a FAIL is a reason to keep the kill switches ready, not a tuning instruction, so I changed no setting.
Decision for you: keep `STRUCTURE_GATE_MODE` / `STRUCTURE_POLICY_MODE` as they are only if you accept a gate with no measured edge; the kill switches are listed in `weekly_calibration/output.txt`.

## Tests
- `tests/test_entry.py`: 2 new (cutoff pauses all entries and nothing more is sent; other rejections do not); the cutoff one fails on the unchanged E195 `entry.py`.
- `tests/test_group209_entry_pause.py`: 2 new (`minutes_left_in_ist_day`).
- `docs/ENV_REFERENCE.md` regenerated; `--check` passes.

    bash scripts/docker_test.sh position-stocks-service
    python3 scripts/gen_env_reference.py --check
