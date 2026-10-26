# E202 - a stale REAL entry order with no Dhan id is looked up by its tag before the symbol is freed

**Where:** real-trade-service/entry_engine/entry.py, expire_stale_orders, top of the per-order loop (REAL only).

**Bug:** `if mode == "REAL" and order.dhan_order_id:` guarded the whole Dhan side. A stale REAL order with no dhan_order_id skipped it and was marked EXPIRED with the shared symbol lock released. But "no id recorded" is not "nothing sent": evaluate_mode commits the PLACED row (it claims the symbol first) before the Dhan call, so a crash, a lost reply or a failed commit before record_real_order_sent leaves exactly this shape while the BUY may be live at Dhan. It would then fill with no position, no cash debit, an unlocked symbol, and nothing for reconcile to follow. evaluate_mode already does the right thing after a placement ERROR (find_order_by_tag with `stk<order.id>`); expire did not.

**Fix:** for REAL, an order without an id is looked up with `dhan_client.find_order_by_tag(db, f"stk{order.id}")`.
- Live order found: record it (record_real_order_sent), alert, then fall into the normal cancel / late-fill path in the same pass (a cancel failure leaves it PLACED with its id for reconcile, as before).
- Order book unreadable (or an odd row with no id): leave the order open with the lock held, warning logged.
- Book readable and no such order: expire as before (no cancel).
DEMO never queries Dhan.

**Test rig:** TestExpireStaleOrders now patches find_order_by_tag to (None, True) (readable, absent); `self.tag_lookup` can override it.

**Tests:** tests/test_e202_expire_unrecorded_order_looked_up_by_tag.py (5). NOT RUN (no pytest/SQLAlchemy in the sandbox); py_compile only.

**Checked:** manual_engine.py (line 318) uses the same `stk<order.id>` tag, so manual BUY orders get the same protection.
