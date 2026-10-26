# E198 - a late-filled entry keeps its lock (and stays FILLED when complete); lock decision uses shares held, not the status label

**Where:** real-trade-service/entry_engine/entry.py, `expire_stale_orders`, the status/lock lines after the REAL post-cancel block (this is the "E198 defect" noted in E199/E200).

**Bug:** a late fill seen with a TERMINAL broker status was booked through `record_real_fill(is_partial=False)`, which sets the order FILLED. The code below then tested `order.status == "PARTIAL"` (False), overwrote the status with EXPIRED and released the shared symbol lock while real shares were held, so position-stocks could buy the same symbol. The same test also missed any order with `filled_qty_so_far > 0` whose status was still PLACED.

**Fix:** the decision uses `filled_qty_so_far`:
- completely filled (`filled >= qty`, status FILLED): the order stays FILLED, the lock stays held, nothing is expired (function returns False for it, after committing the booking);
- any shares held but not all (`filled > 0`, or status PARTIAL): order becomes EXPIRED ("partially filled - no chase for the rest"), the lock is KEPT;
- nothing filled: unchanged (EXPIRED, lock released).

**Tests:** tests/test_e198_late_fill_keeps_status_and_lock.py (4). NOT RUN (sandbox has no pytest/SQLAlchemy and no network); py_compile only. Existing late-fill tests in test_rt_entry_helpers.py only assert on the position, so they are unaffected.

**Note:** I did not have your own E198 diff, so this is a fresh implementation of the defect as described in E199/E200. If your version differs, diff it against these lines of `expire_stale_orders`.
