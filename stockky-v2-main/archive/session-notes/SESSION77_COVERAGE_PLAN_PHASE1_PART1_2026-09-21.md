# Session 77 — 100%-coverage plan, Phase 1 continued: exit_engine/exit.py (2026-09-21)

Continuing the phased plan from the prior turn (`100_PERCENT_COVERAGE_PLAN.md`).
This session tackles the first item on Phase 1's checklist: closing the biggest
remaining gaps in `exit_engine/exit.py` (57% -> targeting 90%+).

## What got added

1. `tests/test_exit_expire_stale_exit_orders.py` (7 tests) — full coverage of
   `expire_stale_exit_orders()`, previously 0% direct (coverage log lines
   266-327): no-op with nothing stale, DEMO-mode is always a no-op (only REAL
   ever has a resting LIMIT exit order), successful cancel + full-remainder
   resend as MARKET, successful cancel + partial-fill resend of only the
   still-open remainder, a Dhan cancel failure correctly leaves the order
   PLACED (not falsely marked EXPIRED — the function's own docstring
   explicitly calls this out as intentional, "don't guess whether it already
   filled"), no open position found still expires the stale order but sends
   nothing, and a since-fully-filled order sends nothing either.
2. `tests/test_exit_send_real_sell_success_and_ip.py` (5 tests) — the
   success path of `_send_real_sell` (order + `TradeOrderEvent` row
   creation, rejection-streak reset via `save_snapshot`, notification
   content), Dhan returning a response with no usable order id (must be
   treated as a failure, no phantom `TradeOrder` row left behind), both
   notification variants of the invalid-IP branch (just-disarmed vs
   already-disarmed), and the pre-session38-migration fallback path where
   `entry_product_type` is NULL — asserts it still correctly falls back to
   the original same-day-opened heuristic (INTRADAY for a same-day position,
   CNC for an older one) rather than crashing or guessing wrong.

No new bugs found in either file — all behavior matched what the code's own
docstrings already claimed. This is expected and fine: closing a coverage gap
finding "no bug" is still valuable (it converts a documented-but-unverified
claim into a verified one), same as round 3's earlier finding for `evaluate_mode`.

## Verification

`python3 -m py_compile` and `python3 -m compileall` clean on the whole
service. No network in this sandbox this session either — `pytest`/
`sqlalchemy` are not installed here, so these 12 new tests are written and
compile-checked but have not actually been *run* in this environment. Traced
each assertion by hand against the current code before writing it. Run on
the VM to confirm before trusting them:

```bash
cd ~/stockky-v2/services/real-trade-service
python3 -m pytest tests/test_exit_expire_stale_exit_orders.py tests/test_exit_send_real_sell_success_and_ip.py -q
python3 -m pytest tests -q -p no:cacheprovider | tail -1
python3 -m pytest tests -q --cov=exit_engine.exit --cov-report=term-missing
```

Diffed the extracted zip against the session76 upload: only
`AUDIT_REPORT.md`, `CHANGELOG_INDEX.md`, this note, and the 2 new test files
changed — `exit_engine/exit.py` itself is untouched this session (no code
changes needed, only tests).

Delivered zip: `stockky-v2-main-2026-09-21-session77-phase1-part1.zip`.

## Still open (Phase 1, exit_engine/exit.py) — per the coverage plan

Per `100_PERCENT_COVERAGE_PLAN.md`'s table, still untested in this file:
- CDSL-eDIS / insufficient-funds / oversell (3 sub-cases) / exchange-not-
  allowed branches (lines ~684-880 in the pre-this-session numbering)
- The two `_cutoff_key` sibling branches to session76's circuit-limit fix:
  `is_intraday_cutoff_error` and `is_security_intraday_restricted_error`
  (~896-1001) — need their own explicit resend-suppression tests, not
  assumed symmetry with the now-tested circuit-limit branch
- The generic-rejection streak/escalation branch and its threshold-crossing
  behavior (~1044-1096)
- `evaluate_mode`'s per-position dispatch-loop error isolation (~266-327 in
  the *original* pre-session76 numbering — re-check against current line
  numbers, this may already be what session77 covered under a different
  label; verify with `--cov-report=annotate` before re-writing)

Next phase-1 items after exit.py: `manual_engine.py` (0%) and
`execution/dhan_client.py`'s error-classifier table, per the plan.
