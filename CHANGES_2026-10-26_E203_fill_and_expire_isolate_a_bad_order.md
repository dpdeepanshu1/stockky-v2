# E203 - one bad order can no longer abort check_pending_fills / expire_stale_orders (or the cycle)

**Where:** real-trade-service/entry_engine/entry.py, `check_pending_fills` (DEMO) and `expire_stale_orders` (both modes).

**Bug:** neither function guarded its per-order work, and cycle_runner calls both with no try/except between the entry stage and the exit stage. An exception for ONE order (a missing decision AND limit price giving `None * float`, a failed account / position write inside `try_fill_entry`, a lock-table or DB error while expiring) therefore:
- left every later order in that pass unprocessed (no fills, no expiries, no late-fill booking),
- propagated out of the stage so the rest of that cycle, including the exit stage, did not run,
- left a half-written state on the session (e.g. `order.status = "FILLED"` set by try_fill_entry before the position write failed), which the next successful commit anywhere on that session could then persist as an order FILLED with no position,
- and repeated every cycle, because the poison order stays PLACED.

**Fix:**
- `check_pending_fills`: each order runs in its own try/except; on failure the session is rolled back, the failure is logged with `logger.exception`, and the loop carries on. A failing quote fetch now returns 0 (orders stay PLACED) instead of raising.
- `expire_stale_orders`: the per-order body moved unchanged into an inner `_expire_one(order)` (the four `continue`s became `return False`, the counter bump became `return True`). The loop wraps it in try/except with rollback, and commits after each order that finished, so a later order's rollback cannot discard an earlier one. The final audit-log line is unchanged.
- No change to what a healthy order does.

**Retry semantics:** a failed order stays PLACED/PARTIAL and is retried next pass. For REAL, if the Dhan cancel had already gone through before the failure, the retry's cancel fails and the existing cancel-failed path leaves it for reconcile (which resolves it against Dhan's book).

**Tests:** tests/test_e203_fill_and_expire_isolate_a_bad_order.py (3). NOT RUN (no pytest/SQLAlchemy in the sandbox); py_compile only.
