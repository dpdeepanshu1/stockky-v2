# Group 293 - the end-of-day report corrects itself once the charges ledger has booked the sells

Follow-up to group 292 (end-of-day report). Listed there as open: "charges of sells that fill after the report time are
estimated in that day's snapshot".

## Why
The snapshot is built once at 15:45 IST. For any closed trade whose orders the charges ledger had not fully booked yet, the
record carries an ESTIMATE (`charges_source` = `estimate` or `ledger_estimated`, or none). The ledger syncs on its own
schedule, so the stored snapshot and the Telegram message could keep those estimates for good.

## What changed (real-trade-service)
- `execution/auto_pilot.py`: `_daily_report_refresh_due(existing)`. A stored snapshot of today is rebuilt when refresh is on
  (`DAILY_REPORT_REFRESH_MINUTES`, default **15**, `0` = off), the time is before `DAILY_REPORT_REFRESH_UNTIL_IST` (default
  **18:00**), the snapshot was built at least that many minutes ago (unreadable build time counts as old) and at least one
  trade of the day is not on a final `ledger` source. The rebuilt snapshot overwrites the stored one and carries
  `refreshed_at` and `refresh_count`. A failed refresh keeps the old snapshot, pushes nothing and is retried at most every
  10 minutes (same throttle as the first build).
- One message `Daily trade report (updated)` is sent only when trades, charges or net P&L differ from the previous snapshot.
  Unchanged figures are stored silently.
- `trade_records.py`: `charges_pending`, `snapshot_figures`; `format_daily_message(snap, updated=False)` adds
  `(charges still estimated for N of M trades)` while any trade is not final.
- **Bug fixed on the way:** `save_daily_snapshot` returned True whenever any snapshot read back under the key, so when an
  overwrite was lost (the cache helper swallows write errors) the OLD snapshot counted as stored. It now requires the
  read-back to be the snapshot just written (same `generated_at`). This also covers the manual `POST .../report/daily/run`.
- `config.py`, `.env.example`, `.env.oracle.recommended`: `DAILY_REPORT_REFRESH_MINUTES`, `DAILY_REPORT_REFRESH_UNTIL_IST`.
- A manual run (`force`) is a fresh report, never an "update".

## Not covered
A trade that CLOSES after the report time (for example a manual exit at 15:50) is only picked up through a refresh while some
other trade still has estimated charges. Otherwise use `POST /positions/{mode}/report/daily/run`.

## Tests
`tests/test_group293_daily_report_refresh.py` (new) and one end-to-end test in `tests/test_group292_daily_report_db.py` on real
in-memory SQLite: report with no ledger rows, ledger books both orders, next tick corrects the snapshot and sends one update,
later ticks do nothing. Each new branch was broken on purpose (13 mutations) and caught.

Full real-trade suite: **4144 passed, 1 skipped** on Python 3.12 and on Python 3.11.15 (per-service venv from `requirements.txt`).
position-stocks is unchanged in this group (3180 passed in group 292). The `.env.example` guard tests in market-data pass.
