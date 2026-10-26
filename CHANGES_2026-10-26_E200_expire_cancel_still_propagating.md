# E200 - a cancel Dhan has accepted is not yet a dead order; expire_stale_orders no longer frees the symbol on that assumption

**Where:** real-trade-service/entry_engine/entry.py, expire_stale_orders, the REAL post-cancel block (follows E199).

**Bug:** cancel_order returning without an error only means Dhan accepted the cancel REQUEST. The exchange can still fill the order for a moment. The post-cancel book read looked only at filledQty; if it showed 0 (or a partial) while the order was still PENDING / TRANSIT / PART_TRADED, the order was marked EXPIRED and the symbol lock released. Any share that filled after that read was invisible to reconcile_real_orders (it only sees PLACED / PARTIAL orders) and position-stocks could buy the symbol again. Also, shares seen in that read were booked with is_partial=False, which marks the order FILLED even though the remainder could still trade.

**Fix:** the broker's orderStatus is read with the fill. A status that is present and not terminal (REJECTED / CANCELLED / CANCELED / EXPIRED / TRADED) means unresolved: the order is left PLACED/PARTIAL with the lock held and nothing is alerted (this is the normal transient case; log only). Shares already filled are still booked, with is_partial=True while the broker shows the order live, so the order stays PARTIAL and visible. The next cycle (or reconcile) resolves it. A blank or missing status does not block expiry.

**E199 test-rig correction (my own miss):** the existing TestExpireStaleOrders tests never patched dhan_client.get_order_list. They passed only because the unpatched SDK call raised and the old code swallowed it. After E199 that makes the order stay open, so several of those tests would have failed. The rig (tests/test_rt_entry_helpers.py) now patches get_order_list to an empty book.

**Tests:** tests/test_e200_expire_cancel_still_propagating.py (4 test functions, 13 cases with parametrisation). NOT RUN (no pytest/SQLAlchemy in the sandbox); py_compile only.

**Known, not changed here:** a late fill seen with a TERMINAL status still goes through record_real_fill(is_partial=False), which sets FILLED, and is then overwritten with EXPIRED and the lock released. That is the E198 defect, so I left it alone.
