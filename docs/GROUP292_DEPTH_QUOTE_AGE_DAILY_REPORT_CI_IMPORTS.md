# Group 292 - depth gate refuses an old book, end-of-day trade report, CI test-import fix

Builds on group 289 (`as_of` / `age_s` on every market-data quote), group 290 (per-trade record and expectancy report) and
group 280 / 286 (depth gate, 20-level size-down).

## 1. Depth gate refuses to judge an old book (position-stocks-service)

### Why
`orders/depth_gate.py` is the only place position-stocks reads market-data `/quote`. It judged spread and best-5 book value on
whatever came back, even if that book was a minute old (market-data serves cached rows). An old picture could clear a trade
the live book would have refused, or size one against depth that no longer exists.

### What changed
- `orders/depth_gate.py`
  - `_quote_age_s(body, symbol)`: how old the book is NOW. `age_s` (a duration on market-data's clock) plus the seconds the
    answer sat in our 5 s cache; tz-aware `as_of` is the fallback. Negative, NaN, infinite, bool, non-numeric ages and naive
    `as_of` strings are not usable.
  - `_fresh_quote(symbol)`: a book older than `ENTRY_DEPTH_MAX_QUOTE_AGE_S` (default **20 s**, `0` = off) is re-read ONCE (cache
    entry dropped first). Still old -> **unknown depth**: `reject_reason` does not block and `max_qty_from_book` does not size.
    It never clears or rejects a trade on a stale picture.
  - No readable age follows `ENTRY_DEPTH_QUOTE_AGE_UNKNOWN`: `allow` (default, behaviour before this group) or `refuse`.
  - `max_qty_from_depth20` ignores a `/depth` body whose own `age_s` is over the limit (no cap).
- `config.py`: `ENTRY_DEPTH_MAX_QUOTE_AGE_S`, `ENTRY_DEPTH_QUOTE_AGE_UNKNOWN`.
- Not changed on purpose: the entry price itself. position-stocks takes it from the AngelOne tick buffer, already limited by
  `ENTRY_MAX_TICK_AGE_S` (45 s); real-trade's equivalent limit is 10 s. Say if position-stocks should be tightened.
- Rollout: needs the group 289 market-data build for `age_s`; against an older market-data every answer is "unknown age" and is
  allowed through by default. Switch off: `ENTRY_DEPTH_MAX_QUOTE_AGE_S=0`.

## 2. End-of-day report (real-trade-service)

### What changed
- `trade_records.py`: `snapshot_key`, `records_for_day`, `build_daily_snapshot` (today's records and report plus the rolling
  7-day report), `format_daily_message` (Telegram text), `save_daily_snapshot` (True only when it reads back),
  `load_daily_snapshot`, `list_daily_snapshots`. Snapshots live in the existing `trade_resilience_cache` table under
  `daily_report:<MODE>:<YYYY-MM-DD>`: no migration.
- `execution/auto_pilot.py`: `_daily_report_step`, called at the top of every schedule tick, independent of arming and of a
  gate row. Runs once per IST weekday from `DAILY_REPORT_TIME_IST` (default **15:45**). The stored snapshot is the once-a-day
  guard, so a restart cannot send it twice. A day with no closed trades stores an empty snapshot and sends no push. A failed
  build or store is retried at most every 10 minutes and nothing is pushed unless the snapshot was stored. A push that is not
  delivered is logged; the snapshot stays readable through the route.
- `main.py`: `GET /positions/{mode}/report/daily?day=` (404 when none stored, 400 on a malformed day),
  `GET /positions/{mode}/report/history?limit=`, `POST /positions/{mode}/report/daily/run` (rebuild, store and push now; returns
  `stored: true`, the push itself is best effort). REAL needs admin login, same as the other `/positions` routes.
- `config.py`: `DAILY_REPORT_ENABLED` (default on), `DAILY_REPORT_TIME_IST`.
- `tests/conftest.py`: the step is off by default for every older test so none depends on the hour the suite runs.

## 3. CI fix (position-stocks-service tests)
GitHub Actions failed at collection with `ModuleNotFoundError: No module named 'test_reconcile'` / `'test_main'`: the tests
folder is a package (`tests/__init__.py`), so sibling tests must be imported as `tests.<module>`. Fixed in
`test_group287_order_events_crosscheck.py` (also one import inside a test body), `test_group288_ws_compare.py`,
`test_group291_dead_parent_unreadable_book.py`, `test_reconcile_dead_parent_fill.py`. Test-only.

## Tests
- position-stocks: `tests/test_group292_depth_quote_age.py` (46). Full suite: **3180 passed**.
- real-trade: `tests/test_group292_daily_report.py` (43, collaborators faked) and `tests/test_group292_daily_report_db.py`
  (26, real in-memory SQLite, the real schedule tick and the real HTTP routes). Full suite: **4099 passed, 1 skipped**.
- Both run with real pytest and `--cov` exactly as `.github/workflows/service-tests.yml` does, on Python 3.12 and on Python 3.11.15
  (a fresh venv per service from its own `requirements.txt`, the CI matrix): same counts on both, 3180 and 4099 + 1 skipped.
- Mutation checks: each new piece was broken on purpose and the tests failed every time except one redundant `isfinite` check
  (the range test already rejects NaN / inf).

## Still open
- The order-update WebSocket event shapes need a live session before `RECONCILE_USE_ORDER_EVENTS=1`.
- Watch for `depth gate ...: /quote price is Ns old` and `daily report ... stored` log lines on the first live trading day.
- Charges of sells that fill after the report time are estimated in that day's snapshot until the ledger books them.
