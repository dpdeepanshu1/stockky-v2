# Group 284 - make the group 192 entry-reject tests independent of the time of day

`orders/entry.py::_rejected_entry_reject` counts only dead entries whose `closed_at` falls on today's IST date.
`tests/test_group192_entry_reject_learning.py::test_two_dead_entries_today_block_for_the_rest_of_the_day` created them
100 and 90 minutes ago, so between 00:00 and 01:40 IST they landed on yesterday's date and the guard correctly saw
nothing -> the assertion failed. The same pattern made `test_day_limit_disabled_leaves_only_the_cooldown` pass for the
wrong reason. Both now use offsets of a few minutes (that test also sets a 1-minute cooldown so it still checks "expired").

Test-only change; no production code touched. This is a likely cause, not a confirmed one: the original traceback was
never seen and the sandbox has no pytest/sqlalchemy. If the test still fails after this, send its traceback.
