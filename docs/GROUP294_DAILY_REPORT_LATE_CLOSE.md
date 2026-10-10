# Group 294 - the end-of-day report also picks up a trade that closes after it was built

Follow-up to group 293. Listed there as not covered: "a trade that CLOSES after the report time (for example a manual exit at
15:50) is only picked up through a refresh while some other trade still has estimated charges".

## What changed (real-trade-service)
- `trade_records.closed_count_for_day(db, mode, day)`: one COUNT of CLOSED positions of the mode whose `closed_at` falls on that
  IST day. Returns None when it cannot be read (never raises).
- `build_daily_snapshot` stores it as `closed_count` (None when unreadable).
- `auto_pilot._daily_report_refresh_due(existing, db=None, mode=None)`: after the existing gates (refresh on, before
  `DAILY_REPORT_REFRESH_UNTIL_IST`, snapshot at least `DAILY_REPORT_REFRESH_MINUTES` old) the snapshot is also due when the live
  count differs from the stored `closed_count`. The count is only read once the snapshot is old enough, so at most one COUNT per
  refresh interval until 18:00. A snapshot without `closed_count` (built before this group) or an unreadable count never triggers.
- A refresh caused by a late close goes through the same path as before: overwrite, and one `Daily trade report (updated)`
  message only if trades, charges or net P&L changed. No new env settings.

## Still not covered
- After `DAILY_REPORT_REFRESH_UNTIL_IST` (18:00) or with `DAILY_REPORT_REFRESH_MINUTES=0`, use `POST /positions/{mode}/report/daily/run`.
- A day that had no snapshot at all (no trades by 15:45) is still built once, as before, on the first tick that finds none stored.
  A trade closing later that day is then picked up by the same count check, because the empty snapshot also stores its count.

## Tests
`tests/test_group294_late_close_refresh.py` (12 new): due / not due (same count, too soon with no count read, after the cutoff,
refresh off, no count stored, unreadable count, no db), the COUNT itself on real SQLite (mode, status, IST day bounds), and a tick
test on real SQLite: report with final charges, a manual exit closes afterwards, next refresh adds it and sends one update,
nothing more after that. 8 + 5 mutations on the new logic, all caught after adding the tests for the survivors.

Full real-trade suite: **4156 passed, 1 skipped** (4144 + 12 new). Other services are unchanged in this group.
