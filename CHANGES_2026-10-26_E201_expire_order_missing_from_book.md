# E201 - an order missing from Dhan's book after the cancel is "cannot tell", not "nothing filled"

**Where:** real-trade-service/entry_engine/entry.py, expire_stale_orders, the REAL post-cancel block (follows E199 / E200).

**Bug:** after a successful cancel the function reads Dhan's order book and, only `if broker_row is not None`, checks for late fills. When the order was not in that list the check was simply skipped and the order went on to be marked EXPIRED with the symbol lock released. The list can lack the order, or be empty, when the read is thin: `dhan_client.get_order_list` returns `[]` for a success envelope with no data (it only raises on an explicit failure status). Any share that filled in that window was then invisible to reconcile_real_orders, which only follows PLACED/PARTIAL orders. reconcile itself applies the opposite rule for the same situation ("not visible yet - check again next cycle, never assume").

**Fix:** a missing order after a successful cancel is treated as unresolved: the order stays PLACED/PARTIAL with the lock held, a warning is logged and no alert is sent (transient). Reconcile / the next expire pass resolves it. An order that is present in the book expires exactly as before.

**Test rig:** TestExpireStaleOrders' fake book (added in E200) now lists every order in the DB as CANCELLED with its already-booked qty, since "empty book" no longer means "resolved".

**Tests:** tests/test_e201_expire_order_missing_from_book.py (3 test functions, 5 cases). NOT RUN (no pytest/SQLAlchemy in the sandbox); py_compile only.

**Trade-off:** an order that never appears in Dhan's book stays open (and re-attempts the cancel each cycle) until reconcile or an operator deals with it, instead of being silently declared dead. That is the deliberate "never assume" side of the choice.
