# Session 112, round 4 (2026-09-25): `capital/ledger.py` coverage 53% → 100%

Next item by priority from the position-stocks-service coverage table after
round 3 (`orders/overnight_stop.py` → 100%). `capital/ledger.py` is the money
engine (reserve / release / daily-loss kill switch / broker sync / cost
reconcile) and was the largest remaining high-value gap: 53%, 81 missing
lines (71-79, 114-123, 133-250, 266-288, 301-302, 388, 447-448, 491, 496, 536,
556-563).

Baseline (this zip): 1403 passed, 82% overall.
After: **1485 passed, 84% overall, `capital/ledger.py` 173 stmts / 0 missed
(100%)**. No production code changed — tests only.

## What was added

`tests/test_ledger_coverage.py` (82 tests), grouped by function:

* `_pick_balance` — key order wins, numeric-string coercion, `0` is a hit (not
  skipped), `None` / non-numeric / unconvertible-type skip, all-miss.
* `_get_or_create` / `_maybe_lazy_reset_daily` — first-use defaults,
  idempotence; stale IST date resets pnl, peer pnl and kill switch (persisted,
  not just in memory) while available/total capital carry over.
* `sync_from_broker` — get_funds failure, no/zero/negative balance, first-key
  and key-shift warnings (and no repeat warning on the same key), the
  capital-erosion add-back (`total = scalp_alloc + capital_risked` of OPEN and
  EXIT_LEGS_REJECTED rows, CLOSED excluded), `EXIT_LEGS_REJECTED` alone
  blocking the available-capital hard reset, zero/negative-available reset to
  the free-cash slice only, positive-available untouched, and the
  peer-pnl-sync + shared-exposure-publish tail.
* `sync_peer_pnl` — the REAL function. The shared `env` fixture stubs it out
  (that was the fixture bug that made the first draft of these tests hit the
  stub); these tests restore it via a captured reference and stub only
  `urllib.request.urlopen`: success (endpoint + 3s timeout asserted), missing
  keys, network failure / malformed JSON / null pnl all keep the last cached
  value and return `None`.
* `reserve_capital` — kill-switch gate, zero-pool gate, sizing formula, wider
  stop ⇒ smaller position, boundary (need == available allowed), no decrement
  on refusal, peer losses do not block entry.
* `reserve_additional`, `release_capital` (accounting; trip at exactly the
  threshold, just below it, cumulative-today, profit offsetting; gate mirror
  incl. no gate row / gate already tripped / ledger already tripped),
  `reconcile_position_cost`, `reclaim_premature_release`, `reset_daily`,
  `get_state` (full dict, naive DB datetimes come out with `+00:00`),
  `book_late_realized_pnl` (total-only; a huge late loss never trips today's
  kill switch).

The autouse `_reset_balance_key` fixture uses `monkeypatch` on the module-level
`ledger._last_balance_key`, so no test can leak first-sync/key-shift state into
another (the first draft reset it by hand and would leak on a failing assert).

## Verification

* Full position-stocks-service suite: 1485 passed, 0 failed, 84% total.
* `pyflakes` clean on the new file.
* Mutation check: 41 hand-written mutations of `capital/ledger.py` (removed
  branches, flipped `<=`/`<`/`>=`/`>`, dropped `+=`/`-=`, sign flips, dropped
  tail calls, wrong status sets, wrong timeout, wrong dict key). 39 killed on
  the first pass; 3 non-equivalent survivors (`reserve_additional` `<= 0` vs
  `< 0`, `reconcile_position_cost` `delta == 0` early return,
  `reclaim_premature_release` `<= 0` early return — each only observable when
  available_capital is already negative, or via the log line) were killed by
  two added tests and one strengthened assertion. The 2 remaining survivors are equivalent mutants:
  `_pick_balance`'s `if v is None: continue` (float(None) raises TypeError,
  caught by the next handler → same result) and `reset_daily`'s trailing
  `pnl_last_reset_date = ist_today_str()` (`_get_or_create` has already set it
  to today by then). `ledger.py` verified byte-identical to the uploaded copy
  after the mutation run.

## Next by priority (from this run's coverage table)

`capital/shared_symbol_lock.py` 30% (65 missing), `capital/shared_order_budget.py`
55% (29), `capital/shared_exposure.py` 36% (16), `tz_utils.py` 71% (12),
`boot_forensics.py` 70% (41), then `db.py` 16% and `execution/dhan_client.py`
21% (need heavier fakes for Oracle/PG init and the Dhan SDK).
