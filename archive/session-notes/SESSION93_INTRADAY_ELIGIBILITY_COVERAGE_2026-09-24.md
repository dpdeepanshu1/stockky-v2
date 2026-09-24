# Session 93 (2026-09-24): intraday_eligibility.py — first direct coverage

## What was already in this zip coming in

The pasted terminal transcript confirmed session92's 3 fixes actually pass
on the live VM: `watchlist_engine/afterhours_scan.py` is now **100%**
(1697 passed, up from 1694, still 1 xfailed). No failures — this session
picks the next item off the 100%-coverage plan's priority list ("cross-
service infra sweep: locks, budgets, oracle_compat.py, db.py migrations,
intraday_eligibility.py"). `shared_order_budget.py`, `shared_symbol_lock.py`,
and `oracle_compat.py` are already 100%; `intraday_eligibility.py` (68
stmts, 60% per the pasted table) is next and was a clean, self-contained
target.

## Why this file had never actually been tested

`intraday_eligibility.py` is referenced from `test_candidates_orchestration.py`,
`test_exit_circuit_limit_resend.py`, `test_exit_error_branches.py`, and
`test_rt_reconcile.py` — but every one of them **monkeypatches its public
functions away** (`get_restricted_symbols`, `record_restriction`) rather
than calling the real implementation. So none of the module's own logic —
including the cross-service `scalp_intraday_restricted` sister-table
mirroring this module exists specifically to do (see its own module
docstring) — had ever run under test.

## This session's work

New `tests/test_intraday_eligibility.py` (23 tests), covering all 6
functions in the file against a real in-memory-SQLite session with both
tables actually present: this service's own `trade_intraday_restricted`
(via `models.Base.metadata`) and a hand-built `scalp_intraday_restricted`
(the sister table belongs to position-stocks-service's codebase, so no ORM
model for it exists here — built with a matching shape via raw `CREATE
TABLE`, same as how the module itself only ever touches it via `text()`).

- `_sister_restricted_symbols` / `_sister_has_restriction` (6 tests): normal
  hit/miss, and — using the bare `db` fixture with the sister table never
  created — the fail-open "sister DB unreachable" path for both, confirmed
  non-raising.
- `_record_sister_restriction` (5 tests): fresh insert with and without a
  detail string, an existing row's `hit_count` incrementing on update, the
  detail-omitted UPDATE branch specifically confirmed to *leave the prior
  `last_detail` alone* (the SQL only appends `, last_detail = :detail` when
  `detail` is truthy — a later None-detail rejection must not blow away an
  earlier one's detail), and the missing-table rollback path (session still
  usable afterward).
- `record_restriction` (5 tests): empty-symbol no-op, new-row creation +
  sister mirror call, existing-row hit-count increment + detail update,
  existing-row with no new detail keeping the prior one, and the 255-char
  `last_detail` truncation.
- `get_restricted_symbols` (2 tests): own+sister union, and — monkeypatching
  `db.query` itself to raise — the own-table-unavailable degrade-to-
  sister-only path.
- `is_restricted` (5 tests): empty-symbol short-circuit, a hit in the own
  table, a hit in the sister table only, a miss in both, and the same
  own-lookup-exception → sister-check fallback as above.

## Verification status — same caveat as every prior round this project

**This sandbox still has no network access** (no `sqlalchemy` installed, so
not even a local import-check was possible this time, let alone a live
`pytest` run) — written and hand-traced against the real source, including
manually re-deriving the raw-SQL UPDATE statement's conditional
`last_detail` clause to get the "leaves it unchanged" test right.

## Round 2 (same day): fixed a real bug the live run caught

The user ran this for real and it correctly caught a bug in the test file
itself: 19 setup errors (`table scalp_intraday_restricted already exists`)
plus 1 assertion failure. Root cause — all `TestSisterRestrictedSymbols` /
etc. tests share one module-level in-memory-SQLite `_engine` (same
convention as `test_afterhours_scan_orchestration.py`'s `db` fixture), but
the sister table lives outside `models.Base` (it's position-stocks-
service's own schema — no ORM model for it in this codebase), so
`models.Base.metadata.drop_all()` in the `db` fixture never touched it.
Once one test created it via raw `CREATE TABLE`, it silently persisted —
rows and all — into every later test in the file: tests using
`db_with_sister` got "table already exists" on their own `CREATE TABLE`,
and `test_missing_sister_table_degrades_to_empty_set` (which needs the
sister table genuinely ABSENT) instead saw a prior test's leftover
`{TCS, INFY}` rows and failed its own assertion.

Fix: the `db` fixture now runs `DROP TABLE IF EXISTS
scalp_intraday_restricted` on every test's setup, before `db_with_sister`
(if used) recreates it fresh. Every test now starts from a genuinely clean
slate regardless of execution order. No changes to `intraday_eligibility.py`
itself or to any test's actual assertions/logic — this was purely a test-
fixture isolation bug in the new file, not a bug in the code under test.

Run for real before trusting the result:

```bash
cd services/real-trade-service
python -m pytest tests/test_intraday_eligibility.py -v
python3 -m pytest -q --cov=. --cov-report=term-missing
```

Expected: `intraday_eligibility.py` at or near 100% (the live run's
first attempt, despite the fixture bug, already got it to 72% on the 1700
tests that did pass — the fix above should close the rest). No production
code was changed this session — tests only.
