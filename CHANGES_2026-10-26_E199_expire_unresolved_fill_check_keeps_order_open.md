# E199 - a failed post-cancel fill check no longer hides the order from reconcile

**Where:** real-trade-service/entry_engine/entry.py, expire_stale_orders, the REAL post-cancel fill read (item 10 block).

**Bug:** after a successful Dhan cancel, the function reads Dhan's order book to book any shares that filled before the cancel landed. If that read raised, the booking raised, or Dhan showed filled shares with no average price yet, the code logged "reconcile will retry next cycle" and carried on to mark the order EXPIRED and release the shared symbol lock. reconcile_real_orders() only queries PLACED/PARTIAL orders, so it could never see the order again. Real shares could be bought with no position, no cash debit and an unlocked symbol. Same class as the item-10 gap, on the failure path.

**Fix:** the unresolved cases now leave the order PLACED/PARTIAL with the lock held (`continue`) and send a notification; reconcile resolves it against Dhan's book in the same cycle. A booking that fails part-way is rolled back first, because filled_qty_so_far is bumped before the position write and would otherwise make reconcile compute a zero delta. Pending changes are committed before the check so the rollback only discards this order's attempt.

**Not touched:** the happy path, the cancel-failed path, and the late-fill status/lock handling after a successful booking (E198).

**Tests:** tests/test_e199_expire_unresolved_fill_check_keeps_order_open.py (5). NOT RUN: the sandbox had no pytest/SQLAlchemy and no network. Only py_compile was run on the code and the test file.

**Merge note:** written against the E197 snapshot, which does not contain the E198 change. The edit is confined to the post-cancel try/except block, above the status/lock lines E198 changes.

## E199b - DEMO: an order past its validity window no longer fills

**Where:** real-trade-service/entry_engine/entry.py, check_pending_fills (DEMO only).

**Bug:** try_fill_entry's docstring says the caller checks valid_until, but check_pending_fills never did. cycle_runner and the main.py self-heal both call it BEFORE expire_stale_orders, so a stale DEMO limit (slow cycle, restart, outage) still filled the moment price dipped into the zone. That is a late chase the no-chase rule exists to stop, and REAL cannot do it (its order is cancelled at Dhan). It inflated DEMO results and made DEMO diverge from REAL.

**Fix:** check_pending_fills skips orders whose valid_until has passed (naive values read as UTC; NULL = no expiry, old behaviour). expire_stale_orders then retires them as before. Quotes are only requested for live orders.

**Tests:** tests/test_e199b_demo_stale_order_does_not_fill.py (5). NOT RUN (no pytest/SQLAlchemy in the sandbox); py_compile only. Existing TestCheckPendingFills orders have valid_until NULL, so they are unaffected.
