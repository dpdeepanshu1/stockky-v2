# Session 112, round 5 (2026-09-25): `capital/shared_symbol_lock.py` coverage 30% → 100%

Next item by priority after round 4 (`capital/ledger.py` → 100%). This is the
cross-service guard that stops position-stocks-service and real-trade-service
both holding a live position in the same symbol on the one shared Dhan account
(the AEGISVOPAK double-buy, 17 Sept). Until now it was only ever exercised
through mocks of its *callers* (`test_entry.py`, `test_reconcile.py`,
`test_eod_squareoff.py`) — nothing tested the module itself: 30%, 65 missing
lines (54, 63-86, 104-109, 115-128, 137-159, 170-200).

Baseline (round 4 zip): 1485 passed, 84% overall.
After: **1539 passed, 86% overall, `capital/shared_symbol_lock.py` 93 stmts /
0 missed (100%)**. No production code changed — tests only.

## What was added

`tests/test_shared_symbol_lock.py` (54 tests). The two properties that matter
are tested from both directions:

* **BLOCK** — a symbol held by the peer refuses our BUY, including when we only
  find out on INSERT. The race is reproduced with the REAL `UNIQUE(symbol)`
  constraint (peer row inserted, session expunged, first SELECT hidden so
  `try_claim` believes the symbol is free → genuine `IntegrityError`), for
  both outcomes: lost to the peer → `False`; "won" by our own service (a
  concurrent request here) → `True`. The session is proven usable afterwards
  (the failed INSERT was rolled back).
* **FAIL OPEN** — vanished-winner-row, re-read failure, arbitrary DB error and
  a failing `rollback()` all still return `True` from `try_claim`; `release`,
  `status`, `force_release`, `cleanup_stale` never raise (a broken lock must
  never block an entry or, especially, an exit).

Also: symbol strip/upper normalisation on every entry point; `release` only
deletes our own row and never the peer's; `force_release` deletes either
side's row and names the holder in the warning; `status` ISO timestamps and the
null-`claimed_at` branch; `cleanup_stale` keeps locks backed by an `OPEN` or
`EXIT_LEGS_REJECTED` position, releases locks whose only positions are
terminal (`CLOSED`/`TARGET_HIT`/`STOP_HIT`/`EOD_SQUAREOFF`/`MANUAL_EXIT`/
`ERROR`), never touches peer-held locks, commits exactly once per batch and
not at all when nothing was released, and is idempotent. Delete-then-
`rollback()` tests prove `release`/`force_release` actually *commit* rather
than merely flush.

## Verification

* Full position-stocks-service suite: 1539 passed, 0 failed, 86% total.
  `pyflakes` clean on the new file.
* Mutation check: 36 hand-written mutations of `shared_symbol_lock.py`
  (dropped normalisation, inverted/removed own-service check, wrong service
  name on insert, dropped commits, dropped rollbacks, wrong except type,
  peer-row filter removed from `release`/`cleanup_stale`, status sets
  narrowed, inverted `has_open`, dropped `released.append` / `db.delete`,
  return values flipped). 34 killed first pass; 1 real gap (`force_release`
  without `commit()` still passed because the same session sees its own
  flushed delete) was closed with the rollback-persistence tests, and 1 was a
  bad mutation pattern that never applied — re-run with a correct pattern and
  killed. 0 survivors. File verified byte-identical to the uploaded copy after
  the run.

## Observation (not changed — flagged)

`cleanup_stale` returns its `released` list even when the closing `commit()`
fails and the whole batch is rolled back, so `main.py`'s startup log line
("cleaned up N stale symbol lock(s)") can claim locks were cleared that are
actually still there. Cosmetic only (the locks simply get retried on the next
startup / via `DELETE /symbol-lock/{symbol}`, and stale locks fail safe by
only blocking our own re-entry) — left as is in a tests-only round; a
two-line fix (`released = []` in the except branch) if you want it.

## Next by priority

`capital/shared_order_budget.py` 55% (29 missing), `capital/shared_exposure.py`
36% (16), `tz_utils.py` 71% (12), `boot_forensics.py` 70% (41), then `db.py`
16% and `execution/dhan_client.py` 21%.
