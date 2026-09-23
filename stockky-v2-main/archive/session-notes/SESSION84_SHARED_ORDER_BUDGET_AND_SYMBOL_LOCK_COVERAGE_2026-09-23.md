# Session 84 — 2026-09-23: shared_order_budget.py + shared_symbol_lock.py coverage closed to 100%

## Context

User ran the full coverage sweep on their VM (corrected `cd` path issue
from a prior transcript — tests live under
`services/real-trade-service/tests/`, not repo root). Real output:

```
exit_engine/exit.py               492      0   100%
portfolio/portfolio.py             430      0   100%
execution/dhan_client.py           382      0   100%
execution/auto_pilot.py            750    611    19%
execution/shared_order_budget.py    55     31    44%
execution/shared_symbol_lock.py     56     33    41%
783 passed, 1 xfailed, 5 warnings in 88.35s
```

Plus two coverage warnings: `Module candidate_engine.candidates was never
imported` and `Module execution.intraday_eligibility was never imported`.

## Root causes

- **`execution.intraday_eligibility` warning**: not a real gap — wrong
  `--cov` flag. The module actually lives at the repo root as
  `intraday_eligibility.py` (`execution/` has no file by that name).
  Correct flag is `--cov=intraday_eligibility`.
- **`candidate_engine.candidates` warning**: real — grepped
  `tests/` for any reference to `candidate_engine`, found none.
  `candidate_engine/candidates.py` (2077 lines) has zero test coverage,
  genuinely never started.
- **`execution/shared_order_budget.py` (44%) and `execution/
  shared_symbol_lock.py` (41%)**: both modules are only ever exercised
  indirectly through `entry.py` / `manual_engine.py` / `exit.py` /
  `portfolio.py` call sites, which always hit the same "happy path"
  (symbol not yet locked, budget not yet exhausted, no DB error) — every
  race-condition branch, exhausted-budget branch, and fail-open exception
  branch in both modules had never been directly tested.

## Fix

Two new test files, no production code changed:

**`tests/test_shared_order_budget.py`**
- `_get_or_create_row()` — both branches. Also flagged (not fixed): this
  function is dead code, nothing in the codebase calls it — only its
  sibling `_ensure_row_exists()` is actually wired into
  `check_and_reserve()`/`record_order_unconditional()`. Candidate for
  removal in a future session.
- `_ensure_row_exists()` — the IntegrityError race-swallow branch
  (simulated via a monkeypatched `db.commit()` that raises once).
- `check_and_reserve()` — under-budget success, budget-exhausted (False)
  + warning log, fail-open on exception (+ rollback-also-fails sub-case).
- `record_order_unconditional()` — unconditional increment even past
  budget (exits are never gated), fail-open on exception (+ rollback-
  also-fails sub-case).

**`tests/test_shared_symbol_lock.py`**
- `try_claim()` — fresh claim, already-ours no-op, blocked-by-other-
  service (+ warning log), both IntegrityError race sub-branches (lost
  to the other service / turned out to already be our own row) plus the
  inner re-SELECT-also-fails sub-case, and the outer fail-open exception
  branch.
- `release()` — non-blocking exception branch (prior tests only covered
  the normal delete-and-commit path via other test files).
- `status()` — was exercised by **zero** tests anywhere before this
  session. Now covers the normal multi-row snapshot shape and the
  fail-safe `[]`-on-exception branch.

IntegrityError races are simulated with a small query-call-counting mock
(`_FakeQueryResult`) rather than genuine concurrent DB connections —
sqlite in-memory engines in this sandbox don't reliably share state
across separate connections, so a real two-connection race isn't
practical to simulate deterministically. The mock still exercises the
exact same code branches (initial SELECT, INSERT raises IntegrityError,
rollback, re-SELECT) with deterministic, controlled results.

## Verification

`py_compile`-clean. **Not executed** in this sandbox — no pytest and no
network here (same limitation noted since session73) — hand-traced
against the source line-by-line instead. User should run on the VM:

```bash
cd ~/stockky-v2/services/real-trade-service
python3 -m pytest tests/test_shared_order_budget.py tests/test_shared_symbol_lock.py \
  --cov=execution.shared_order_budget --cov=execution.shared_symbol_lock \
  --cov-report=term-missing -q
```

Expect both modules at 100%.

## Still open (priority order)

1. `execution/auto_pilot.py` — 19%, 611 of 750 lines missing. By far the
   largest gap left — this is the live auto-trading orchestrator. Not
   started this session; needs its own dedicated round(s) given its size
   (large untested blocks at 166-203, 355-384, 449-492, 522-555, 586-649,
   659-721, 729-829, 1077-1114, 1130-1226, 1272-1409, 1444-1554, 1720-1806).
2. `candidate_engine/candidates.py` — 0%, 2077 lines, never touched.
3. Re-run with `--cov=intraday_eligibility` (corrected module path) to
   get its real number — previously misreported as "never imported".
