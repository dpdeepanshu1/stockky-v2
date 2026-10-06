# Group 210 - stale symbol locks are swept while the service runs (position-stocks-service)

Cumulative on group 209. Item 14 of the open list, first half (stale locks). Rebuild position-stocks-service only.

## What was wrong
`capital/shared_symbol_lock.cleanup_stale()` ran only at startup. A lock left behind by a row that ended in ERROR
(AVALON: filled trade stored as ERROR after a rejected duplicate SELL) blocked that symbol for this service, and
for real-trade-service's own claim on it, until the next restart.

## Changes
- `capital/shared_symbol_lock.py`: the sweep body is now `_sweep(db, min_age_s, keep_dead_sell)`.
  - `cleanup_stale(db)` (startup) behaves exactly as before.
  - New `sweep_stale(db)` (runtime): leaves a claim younger than `SYMBOL_LOCK_SWEEP_MIN_AGE_S` (600 s) alone,
    because `try_claim()` runs before the ScalpPosition row exists; keeps the lock of an ERROR row whose exit SELL
    died with zero fill (`*_SELL_DEAD`, the position may still be open and reconcile re-claimed it on purpose);
    never touches the peer's rows; throttled to one pass per `SYMBOL_LOCK_SWEEP_INTERVAL_S` (60 s, 0 = off);
    never raises.
- `main.py::_fast_reconcile_loop`: calls `sweep_stale` next to `publish_exposure`, market open or not, and logs a
  WARNING naming the symbols it freed.
- `config.py`: `SYMBOL_LOCK_SWEEP_INTERVAL_S`, `SYMBOL_LOCK_SWEEP_MIN_AGE_S`.

## Not changed (second half of item 14: COHANCE in the priority-quote list)
real-trade-service builds its priority lane from `open_positions()` (status OPEN / PARTIALLY_CLOSED) on every
cycle, so a sold symbol stays in the list only while its `trade_positions` row is still OPEN. The code cannot
tell why from here (a sale made outside the service, or a close that was never booked). To find it, send the row:
`SELECT id, symbol, mode, status, opened_at, closed_at FROM trade_positions WHERE symbol='COHANCE';`
No change made without that.

## Tests
`tests/test_group210_symbol_lock_sweep.py` (10 cases, SQLite). The sandbox has no pytest/sqlalchemy: the sweep
logic was run against stubbed models (AVALON freed; young claim, open position and dead-SELL lock kept; startup
sweep unchanged) and all changed files compile. Run `bash run_tests.sh` on the VM for the DB-backed cases and the
existing `tests/test_shared_symbol_lock.py`.
