# E172 - line-by-line read of `reconcile_real_orders`, two dead-SELL bugs fixed

Includes E171.

Read: `execution/reconcile.py` `reconcile_real_orders` (347 lines) plus `_book_fill_delta`, `_restore_orphaned_position`,
`_track_exit_failure_and_maybe_alert`, `_repair_orphaned_pending_exits`, and the position state left by `record_real_exit_fill`.
The fill booking itself (cumulative to delta, increment price, BUY stop/target fallbacks, partial status) is correct.

## Bug 1 - a SELL cancelled after a partial fill was counted as a zero-fill rejection
Cycle 1 books a partial SELL (order PARTIAL, position PARTIALLY_CLOSED). Cycle 2 the broker cancels the rest: the new fill is 0,
so the dead-order branch took the "zero fill" path: it bumped `consecutive_exit_failures` (which makes `_send_real_sell` wait a
doubling cooldown before selling the remaining shares), could raise the "Exit SELL stuck" alert and wrote "zero fill" in the
event log. Now that path runs only when `filled_qty_so_far` is 0. The position already is PARTIALLY_CLOSED and is re-evaluated
by the exit engine at once.

## Bug 2 - dead order with new filled shares but no price was treated as zero fill
If the row showed new filled shares but no average price (`delta > 0`, `fill_price is None`), the old code skipped the booking,
fell into the zero-fill path and marked the order dead, so those shares were never booked. Now the order is left PLACED/PARTIAL
with a CRITICAL log line and is booked on the next cycle once the price appears.

Tests: `tests/test_rt_reconcile.py` (+2, both fail without the fix).
Run here: `test_rt_reconcile.py` + `test_a13...` 135 passed; whole real-trade suite 5,326 passed, 2 skipped (oracle_compat file
left out: no `oracledb` in this sandbox).

## Noted, not changed
- An order that never shows in Dhan's order list is skipped every cycle ("not visible yet, never assume"). Dhan's list is
  today's orders, so a PLACED order from an earlier day would stay pending without any alert. Entry orders and LIMIT sells have
  their own expiry passes; I did not check every other order kind. Needs a design choice (alert after N days?).
- `fill_price` is the broker's cumulative average, used for the increment (`_increment_price` corrects it); already documented.
