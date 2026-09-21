# Session 77 (part 2) — 100%-coverage plan, Phase 1: exit_engine/exit.py's error-classification branches (2026-09-21)

Continues directly from part 1 (`SESSION77_COVERAGE_PLAN_PHASE1_PART1_2026-09-21.md`),
same session's coverage run as the starting point:

```
exit_engine/exit.py     482    157    67%   67-69, 131, 165-166, 176-182, 378-380,
                                             544-545, 547-548, 577, 684-689, 706,
                                             719-745, 776-780, 795-812, 833-840,
                                             857-880, 896-934, 960-1001, 1044-1049,
                                             1060, 1091-1092, 1096, 1197-1198,
                                             1282-1285, 1353-1380, 1417-1420,
                                             1456-1464, 1509-1515, 1523-1530
448 passed, 1 xfailed in 28.66s
```

This part targets everything in the 684-1096 range — the error-classification
ladder inside `_send_real_sell`'s except block.

## What got added

`tests/test_exit_error_branches.py` (21 tests):

1. **CDSL/eDIS** (3 tests) — single throttled alert, re-fires after
   `CDSL_ALERT_COOLDOWN_MIN` elapses, confirmed `is_persistent` (bumps
   `consecutive_exit_failures`) and does NOT touch the separate
   `exit_reject_streak_<id>` snapshot key used by the generic branch.
2. **Insufficient funds** (2 tests) — same alert/cooldown pattern, confirmed
   persistent.
3. **Oversell** (5 tests) — all 3 documented sub-cases from the code's own
   comments: `broker_qty<=0` → `force_close_real_position(..., "oversell_ghost_close")`;
   `0<broker_qty<qty_open` → `qty_open` capped to the broker's real figure,
   "Qty synced" alert; `broker_qty>=qty_open` → pure no-op, no mutation, no
   ghost-close (a timing issue, retried next cycle as-is). Plus the
   holdings-sync-itself-raising path (separate throttled alert,
   "Qty sync failed") and confirmed oversell never bumps either streak
   counter (`excluded=True` in `_bump_exit_failure`, and never touches
   `exit_reject_streak_<id>` either since it's a different except branch
   entirely).
4. **Exchange-not-allowed (EXCH:16387)** (2 tests) — alert/cooldown,
   confirmed persistent.
5. **`_cutoff_key` siblings** (5 tests) — `is_intraday_cutoff_error` and
   `is_security_intraday_restricted_error` each get an explicit
   resend-suppression test: `place_order` mocked with a call counter, first
   call raises and sets `intraday_cutoff_hit_<id>_<today>`, next 5 calls the
   same test-session all short-circuit at the top-of-function check (`if
   load_snapshot(db, _cutoff_key): return False`) before ever reaching
   `place_order` again — call count stays at 1. Not assumed symmetry with
   session76's circuit-limit test; each sibling has its own test using its
   own exact error-marker string. Also: confirmed both never bump the
   generic streak, and confirmed `record_restriction()`'s best-effort
   contract (a raised exception inside it doesn't prevent the alert/skip
   handling that follows).
6. **Generic-rejection streak escalation** (3 tests) — exercised end-to-end
   through `_send_real_sell` itself (the existing
   `test_exit_backoff_escalation.py` only unit-tests `_bump_exit_failure` in
   isolation; this is the first test to drive the actual except-block
   ladder). Worked out the real state machine by hand-tracing the code
   rather than assuming "N rejections = N alerts": alerting is
   cooldown-throttled, so only the very first rejection alerts plainly:
   subsequent rejections stay silent until the streak first reaches
   `EXIT_REJECT_STREAK_ESCALATE_AT`, at which point the escalation ("STUCK")
   alert fires exactly once — and critically, the escalation branch does
   NOT gate on the cooldown (`elif streak >= escalate_at:` has no `due`
   check), so it fires immediately on threshold-crossing even if still
   inside the plain-alert's cooldown window. After escalating it goes quiet
   again (guarded by `escalated=True and not due`), then re-fires (still the
   "STUCK" message) once cooldown elapses again with the streak still at or
   above threshold. Also confirmed a stream of *different* unrecognized
   error strings still increments one shared per-position counter (the key
   is `exit_reject_streak_<position.id>`, not keyed by error message) and
   confirmed the exact `_bump_exit_failure` crossover point:
   `consecutive_exit_failures` stays 0 for every rejection while
   `current_streak < escalate_at`, then bumps by exactly 1 the moment
   `current_streak >= escalate_at` — matching `_bump_exit_failure`'s own
   `should_bump = is_persistent or (current_streak >= escalate_at)` contract,
   now proven through the real call site instead of only the direct-call
   unit tests in `test_exit_backoff_escalation.py`.

No new bugs found — every branch behaved exactly as its own inline comments
document. This is the same "verified, not just assumed" outcome as part 1;
worth calling out specifically for the escalation state machine since it's
the most intricate piece of logic in the file and the easiest one to get
subtly wrong in a test (an earlier draft of this test file assumed "every
rejection alerts" and would have failed against the real cooldown-throttled
behavior — caught and fixed by hand-tracing the code before finalizing, not
by running the test, since pytest still isn't available in this sandbox).

## Verification

`python3 -m py_compile` clean on the new test file and the whole service.
Same sandbox limitation as every session this week — no network here,
`pytest`/`sqlalchemy` not installed — so these 21 tests are written and
hand-traced call-by-call against the current code (including working through
the exact snapshot-key read/write sequence and cooldown-elapsed math for the
escalation test) but not yet executed in this environment. Run on the VM to
confirm:

```bash
cd ~/stockky-v2/services/real-trade-service
python3 -m pytest tests/test_exit_error_branches.py -q
python3 -m pytest tests -q -p no:cacheprovider | tail -1
python3 -m pytest tests -q --cov=exit_engine.exit --cov-report=term-missing
```

Diffed the extracted zip against the part-1 upload: only `AUDIT_REPORT.md`,
`CHANGELOG_INDEX.md`, this note, and the 1 new test file changed —
`exit_engine/exit.py` itself is untouched this session (no code changes
needed, only tests, same as part 1).

Delivered zip: `stockky-v2-main-2026-09-21-session77-phase1-part2.zip`.

## Still open (Phase 1, exit_engine/exit.py) — per the coverage plan

- Lines 67-69, 131, 165-166, 176-182, 378-380 — small helper/import guards,
  flagged in the plan as "usually cheapest to close" — not yet looked at
  this session.
- Lines 544-545, 547-548, 577 — need to re-check against the current file;
  may already be covered by part 1's pre-migration-fallback test (line
  numbers shifted slightly between the coverage run this note started from
  and the current file) — confirm with `--cov-report=annotate` before
  writing anything new here.
- 1197-1198, 1282-1285, 1353-1380, 1417-1420, 1456-1464, 1509-1515,
  1523-1530 — likely `evaluate_mode`'s trail/breakeven tail; part 1's
  `expire_stale_exit_orders` tests may have already closed some of this
  range. Confirm with `--cov-report=annotate` rather than re-guessing blind.

Once those are closed (or confirmed already-closed), `exit_engine/exit.py`
should be at or near the plan's 90%+ target and Phase 1 can move to its next
item: `manual_engine.py` (0%) and `execution/dhan_client.py`'s
error-classifier table, per `100_PERCENT_COVERAGE_PLAN.md`'s suggested
session sequence.
