# A3 - write-behind queue for evidence rows (2026-10-22)

## Problem
`DPY-4024` (Oracle call timeout, 8 s) killed exit ticks on an INSERT into `trade_exit_decisions` (a HOLD row) while the DB was saturated. The entry cycle also wrote
one `trade_decisions` WAIT row per candidate inside the same transaction as real orders. Evidence rows were able to delay or fail protective and order work.

## What changed (real-trade only)
`write_behind.py`: one bounded in-memory queue and one daemon worker with its own DB session.

| Row | Before | Now |
|---|---|---|
| `TradeExitDecision` action HOLD (`exit_engine/exit.py`) | `db.add` in the exit session | queued (`exit_hold`) |
| `TradeDecision` action WAIT (`entry_engine/entry.py`, rows made through `_wait`) | `db.add`, flushed / committed with the cycle | removed from the session before the ENTER flush and the cycle commit, queued (`entry_wait`) |
| orders, fills, positions, cash, TRAIL_STOP / FULL_EXIT / EMERGENCY / PARTIAL exit decisions, ENTER and REJECTED decisions via `_mark_skipped_wait`, audit rows | synchronous | unchanged (synchronous) |

- **Back-off (group 347, whole cycle):** any DPY-4024 seen by the fast tick, the full tick (`auto_pilot._is_db_call_timeout`) or the worker stops queue writes for `WRITE_BEHIND_BACKOFF_S` (20 s). Rows wait in the queue.
- **Failure handling:** a timed-out batch is put back; any other error retries twice, then the rows are dropped and counted. Full queue (`WRITE_BEHIND_MAX_QUEUE`, 5000) drops the oldest row.
- **Shutdown:** the queue is drained (4 s, best effort). A crash loses at most the queued rows (seconds of evidence).
- **Switch:** `WRITE_BEHIND_ENABLED=0` writes every row inline as before. `GET /write-behind/status` shows queued / written / dropped / back-off.
- **Lag:** rows appear in the DB about 1 s after the cycle instead of with it. The only reader that looks at the latest WAIT per symbol (`candidates._recently_candidated_symbols`, gate-6 requeue) compares against a window of minutes; a missing newest row keeps the full cooldown (the fail-safe side).

## Live quotes
No change needed. real-trade never writes `live_quotes`; market-data's writer (groups 344 / 347 / 356) is already change-only, priority-first for held symbols, memory-first on reads and backed off after a timeout.

## Not done on purpose
- `log_action` (audit): it commits the caller's session, so many call sites rely on it as a commit. Moving it behind a queue would leave caller work uncommitted.
- `TradePosition.max_price_seen / min_price_seen` (excursion): written only on a new extreme, and in the same UPDATE as stop changes.

## Tests
`tests/test_a3_write_behind.py` (13, pure). `tests/conftest.py` turns the queue off for every pre-existing test (they read these rows straight after the call).
Not run here: the exit / entry integration (needs SQLAlchemy). Run the full real-trade suite before deploying.
