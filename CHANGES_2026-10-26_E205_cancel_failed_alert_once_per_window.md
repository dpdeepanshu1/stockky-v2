# E205 - "Dhan cancel failed" is alerted once per window per order, not once per pass

**Where:** real-trade-service/entry_engine/entry.py, `expire_stale_orders` cancel-failed branch (REAL), plus a small helper `_cancel_fail_alert_due`.

**Problem:** when the Dhan cancel for a stale entry fails, the order is deliberately left PLACED for reconcile (a failed cancel can mean "already filled"). It is also still stale, so the same branch runs again on the next cycle and on every main.py self-heal read, each time sending a Telegram. A persistent failure (gateway down, order not found) buried the alert channel in identical messages, which hides the first, useful one and anything else that follows.

**Fix:** the alert is sent once per order (keyed by Dhan order id) per `EXPIRE_CANCEL_FAIL_ALERT_S` seconds (default 900; `0` = alert every pass, the old behaviour). The warning log line is unchanged and still fires every pass; the order handling is unchanged (left PLACED, retried). The helper never raises: on any error it errs toward sending the alert. The memory is a small in-process dict, pruned past 500 entries; a restart simply re-alerts once.

**Test rig:** TestExpireStaleOrders clears the alert memory per test (order ids repeat across tests).

**Tests:** tests/test_e205_cancel_failed_alert_once_per_window.py (4). NOT RUN (no pytest/SQLAlchemy in the sandbox); py_compile only.
