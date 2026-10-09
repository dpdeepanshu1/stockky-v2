# Group 285 - group 192 reconcile test asserted the wrong thing

`tests/test_group192_entry_reject_learning.py::TestReconcileLearnsFromRejection::test_reason_on_the_super_row_is_stored_and_symbol_is_restricted`
failed with `assert 1 == 0` on `broker.plain_calls == 0`. This is the real failure (run with pytest this time, not guessed).

Cause: the test counted every plain order-book read made during the whole `run_exit_reconciliation` pass. Since the
dead-parent check was added, `_filled_entry_behind_dead_parent` -> `_cached_order_list` reads the order book once per
dead entry, a different and intended guard. The rejection-reason lookup itself (`_entry_reject_reason`) is correct: it
returns the reason from the super row and does not touch the order book.

Fix (test-only): reset the counter after the reconcile pass and assert that `_entry_reject_reason` called directly on a
row that carries the reason makes zero order-book calls. No production code changed.

Verified: tests/test_group192_entry_reject_learning.py 22 passed; full position-stocks suite 3053 passed
(`PYTHONPATH=tests python3 -m pytest tests -q` from services/position-stocks-service; `test_reconcile_dead_parent_fill.py`
imports `test_reconcile` by bare name, so it needs `tests` on the path).

Group 284's IST-midnight change (offsets of a few minutes) was already in the zip and is unchanged.
