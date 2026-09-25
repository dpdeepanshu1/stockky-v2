# Session 112, Round 27 — position-stocks-service `orders/eod_squareoff.py` stagnation-exit coverage

Closes 2 of the 4 lines flagged against this file per the round-24
coverage run: 1044-1045, inside `run_stagnation_exit`'s per-position loop.

## What was covered

- **Malformed `opened_at` (1044-1045):** `except Exception: continue`
  guarding `opened_at = as_aware(pos.opened_at)`. `tz_utils.as_aware`
  itself never raises for a real `datetime` (naive or aware) or `None` —
  this branch exists for a corrupted/non-datetime value making it back
  from the DB (e.g. `dt.tzinfo` access failing on a non-datetime type).
  No existing test produced that condition. New test
  `test_malformed_opened_at_skips_that_position_and_continues` monkeypatches
  `eod.as_aware` to raise only for one position's `opened_at` value (real
  implementation otherwise, same isolation-test convention already used
  for the broker-rejection and unexpected-`close_position_now`-error
  cases in the same class) and asserts that position is left `OPEN` while
  its sibling is still closed as `STAGNATION_EXIT` in the same pass —
  i.e. the exception doesn't abort the whole sweep.

## Not done this round

- `orders/eod_squareoff.py`'s other 2 flagged lines (per round-24's
  count of 4) are still open — not yet re-identified against a fresh
  coverage run; the other two may already be closed by unrelated work
  since round 24, needs a fresh `--cov-report=term-missing` to confirm.
- The four 1-line gaps in `orders/adaptive.py` / `entry.py` /
  `reconcile.py` / `screening/engine.py` are still open per round-24.
- `config.py` 471-475 — still open, same `importlib.reload` risk noted
  since round 23.
- No `sqlalchemy`/`pytest` in this sandbox — the new test is statically
  verified with `py_compile` only, plus the exception-isolation control
  flow hand-verified against a plain-Python stand-in for the loop body
  (not the real ORM/DB path). Re-run pytest on the real box to confirm,
  same as every round since 23.
- PB FinTech `legDetails` root-cause thread still untouched.
