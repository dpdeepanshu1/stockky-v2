# Session 112, round 6 (2026-09-25): `capital/shared_order_budget.py` coverage 55% → 100%

Next item by priority after round 5 (`capital/shared_symbol_lock.py` → 100%).
This is the cross-service Dhan account-wide order-rate guard (one row per IST
day in `stockky_shared_order_budget`, shared with real-trade-service). It was
only ever exercised as a mocked collaborator of `orders/entry.py` and
`orders/eod_squareoff.py`: 55%, 29 missing lines (46-51, 67-68, 109-115,
142-147, 153-160) — i.e. the IntegrityError race swallow, the fail-open branch
of `check_and_reserve`, the error branch of `record_order_unconditional`, and
all of `status()`'s snapshot logic.

Baseline (round 5 zip): 1539 passed, 86% overall.
After: **1577 passed, 86% overall, `capital/shared_order_budget.py` 64 stmts /
0 missed (100%)**. No production code changed — tests only. (Overall % is flat
at the rounded figure: this file is small; the whole `capital/` package except
`shared_exposure.py` is now 100%.)

## What was added

`tests/test_shared_order_budget.py` (38 tests):

* **Gate** — `check_and_reserve` fills exactly to the cap, then refuses
  stickily without incrementing; warning reports `used/budget`; a counter
  already past the cap (exits overshoot) still refuses; budget is read from
  config at call time; a new IST day starts a fresh counter and leaves
  yesterday's row alone; `rowcount == 0` is treated as exhausted; exhausted
  with a missing row falls back to the budget figure instead of crashing.
* **Atomicity regression (session "AUDIT FIX")** — the cap check must be the
  UPDATE's own WHERE clause. The test loads the row (identity map says 0),
  bumps the DB to the cap behind the session's back with
  `synchronize_session=False`, and asserts the call is refused. A
  read-then-increment implementation would trust the stale 0 and overshoot.
* **Never gate exits** — `record_order_unconditional` increments far past the
  cap, is cumulative and committed (verified with `rollback()` afterwards),
  touches only today's row, and swallows every failure (including a failing
  `rollback()` inside the handler). Exits and entries share one counter.
* **Fail open** — update error, `_ensure_row_exists` error and a failing
  rollback in `check_and_reserve` all return `True` and log.
* **`_ensure_row_exists`** — creates a committed zero row; no-op without a
  commit when the row exists; the REAL `UNIQUE(trade_date)` race (first lookup
  hidden so the INSERT collides) is swallowed with the session still usable
  afterwards; non-IntegrityError failures propagate (callers own fail-open).
* **`status()`** — no row → zeros; used/remaining; exactly at cap; over cap
  never reports negative `remaining`; only today's row counts; budget mirrors
  config; DB error → zeros + log, never raises.
* **`_get_or_create_row`** — dead code (see below), tested directly: creates
  via flush (not commit), returns existing without duplicating.

## Verification

* Full position-stocks-service suite: 1577 passed, 0 failed. `pyflakes` clean.
* Mutation check: 29 hand-written mutations of `shared_order_budget.py`
  (dropped flush/commit/rollback, inverted None checks, `<` → `<=`, hard-coded
  budget, removed `trade_date` / cap `WHERE` clauses, `+1` → `+2`, flipped
  return values, dropped `try/except`, wrong status fallbacks, unclamped
  `remaining`, un-dated status query). 28 killed first pass; 1 real gap —
  removing the `trade_date` pin from `check_and_reserve`'s UPDATE survived,
  because every earlier test had a single row; closed with
  `test_only_todays_row_is_incremented`. 0 survivors. File verified
  byte-identical to the uploaded copy after the run.

## Observation (not changed — flagged)

`_get_or_create_row` is dead code in this service (nothing calls it; only
`_ensure_row_exists` is wired up) — the same as the duplicated copy in
real-trade-service, whose own test file already flags it as a removal
candidate. Left in place: removing it is a production-code edit and it is
harmless; it is covered so it cannot rot silently. Say the word and it (plus
its two tests, here and in real-trade-service) can be deleted.

## Next by priority

`capital/shared_exposure.py` 36% (16 missing), `tz_utils.py` 71% (12),
`boot_forensics.py` 70% (41), `auth/admin_auth.py` 29% and
`auth/dhan_credentials_ro.py` 46%, then `pipeline_status.py` 31%, `db.py` 16%,
`execution/dhan_client.py` 21%, `feed/*` and `main.py`.
