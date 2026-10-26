# E183 - position-stocks: the EOD sweep gave up for the day after one failed pass (E181's "next sweep retries" was wrong)

Includes E182.

## What I did
Read `run_eod_squareoff` (all of it: overnight-carry selection, conversion and stops, eDIS pre-check, the per-position loop, the failure classification and the
once-per-day guard) and both callers in `main.py` (the scan loop and the fast-reconcile loop), plus `exit_retry.py`.

## Correction to E181
I wrote that after a blocked exit SELL "the next sweep or a manual close retries". For the 15:00 EOD sweep that was wrong: the sweep set
`eod_squareoff_fired_date = today` at the end of the pass even when positions were left OPEN, and both loops skip the sweep once it is set. Only a manual
close (or the stagnation / stop-watchdog exits) could retry. So E181's "block while Dhan's order book is unreadable" left the position OPEN, bracket already
cancelled, until you clicked, even though the order-book outage was usually over within a minute or two.

## Findings (fixed)
1. A failed pass ended the day. A transient failure (an unclassified error, E181's unverified-book block, an exit-placement backoff) is exactly what a retry a
   minute later clears. The code comment, the `/status` comment and an existing test all called "no auto-retry" intentional; I reversed that for transient failures
   only, because with E181's duplicate-SELL guards and exit_retry's backoff a retry cannot double-sell, and Dhan squares intraday positions off from about 15:15 anyway.
2. Two sweeps could run at once. The scan loop and the fast-reconcile loop both call `run_eod_squareoff` while the day is not marked done, and a pass takes
   seconds (cancel waits, retry delays). Nothing stopped both from selling the same position. Retry passes would have made this likelier.
3. A second pass would have sold today's carried positions. `run_eod_squareoff` squares off every OPEN position that is already `overnight_converted_to_cnc`
   (correct for a carry from an earlier day). Run again the same day (or after a restart mid-sweep) it would sell the holdings it had just carried, with
   their stops. This was already possible after a crash mid-sweep; the retry window would have widened it.

## Change (orders/eod_squareoff.py)
- `run_eod_squareoff` now takes a non-blocking lock: one sweep at a time (a second caller returns 0 at once). The old body is `_run_eod_squareoff_pass`.
- A pass collects the positions left OPEN by a possibly-transient failure (`ExitInCooldown`, or the unclassified-error branch). If any exist and the retry
  window is still open (EOD_SQUAREOFF_TIME_IST + 12 minutes, constant `_EOD_RETRY_WINDOW_MIN`), the day is NOT marked done; their ids are kept in process memory.
  Later calls run a retry pass: only those positions, no carry evaluation, no eDIS pre-check, quiet logging while in backoff.
- When the window has closed with positions still OPEN: one critical alert listing them ("EOD SQUAREOFF GAVE UP ... flatten by hand"), then the day is marked done.
- Permanent rejections (intraday cut-off, restricted, insufficient funds, circuit limit) end the day at once, as before. If the sweep first runs after
  the window, behaviour is exactly as before.
- `_carried_overnight_today()`: a position converted to CNC today (opened today, IST) with its stop in place (or with OVERNIGHT_STOP_LOSS_PCT=0, the explicit
  no-stop opt-out) is never squared off by the sweep, on any pass, including after a restart. A converted position with no stop while a stop is required (a
  failed carry) and a carry from an earlier day are still squared off.
- No new environment variable and no new column. `main.py`: one comment updated.

## Not changed / your call
- The retry state is process memory. After a restart inside the window the first call is a normal pass again (today's carry is protected by the rule above;
  a position that failed earlier is simply retried like any other OPEN one, and may now be evaluated for the overnight carry, which only happens if it
  passes all four quality checks and the conversion succeeds).
- The 12-minute window is a constant. Say if you want it in the config or in the Real Money Calibration tab; I judged a safety retry should not be tunable there.
- Not changed: `close_position_now` and `run_short_squareoff` (manual and 14:55 short cover) - read earlier, no retry window of their own.

## Tests
tests/test_eod_squareoff.py: the `env` fixture now pins the retry window as already closed, so every existing test keeps its old expectation. New
`TestEodRetryWindow` (retry then sell, quiet backoff, give-up alert, permanent rejection ends the day, retry touches only the failed position, no
eDIS/carry on retry, lock, lock released after an error) and `TestCarriedTodayIsNeverSold` (4 cases).
Not run with pytest (no pytest/SQLAlchemy in the sandbox). The real `eod_squareoff.py` (with the real `exit_retry`, `tz_utils`, `dhan_client`) ran 21 checks in a
throwaway stub harness with `_fire_flat_sell` scripted; all passed. The pytest tests themselves were not run.

## VM commands
    bash scripts/docker_test.sh position-stocks-service
    python3 scripts/gen_env_reference.py --check
