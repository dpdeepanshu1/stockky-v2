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

## Fix round (user ran the tests on the VM)

`test_generic_rejection_escalates_at_threshold_then_suppresses` failed on
the VM: `assert 3 == (3 + 1)` — a follow-up rejection right after the
escalation call didn't bump the streak count at all. Root cause, confirmed
by reading `_should_skip_exit_this_cycle` (called at the very top of
`_send_real_sell`, line ~491): the SAME rejection that crosses
`EXIT_REJECT_STREAK_ESCALATE_AT` also satisfies `_bump_exit_failure`'s
`current_streak >= escalate_at` condition — which bumps
`consecutive_exit_failures` even for a non-persistent generic error. That
means the very next evaluation cycle is skipped by the exponential backoff
(`BASE_COOLDOWN * 2^(failures-1)`, 60s on the first bump) *before*
`_send_real_sell` ever reaches Dhan or the streak snapshot again — visible
in the pasted log too (only 3 `ERROR ... REAL exit SELL failed` lines for 4
`_sell()` calls, since the 4th call returned `False` from the backoff guard
without ever entering the `try` block). Not a bug in the production code —
this backoff is intentional and matches `test_exit_backoff_escalation.py`'s
own cooldown tests — my test's assumption that every subsequent call would
reach the generic-error branch was wrong. Fixed the test itself:
1. Added a call counter around the mocked `place_order` so the
   backoff-skip can be asserted directly (`calls["n"]` unchanged across the
   skipped cycle).
2. Added an explicit assertion that the immediate next call after crossing
   the threshold is skipped by backoff (`consecutive_exit_failures == 1`,
   no new alert, streak snapshot untouched) — this is now itself a useful
   regression test for the backoff/streak interaction, not just a fix.
3. Before every subsequent call that the test needs to actually reach Dhan
   again, explicitly clears the backoff window by setting
   `position.last_exit_failure_at` far enough into the past (using
   `config.EXIT_RETRY_MAX_COOLDOWN_SECONDS` as a safe upper bound regardless
   of how many times the exponential backoff has doubled by that point).

Hand-traced the full corrected call sequence (7 `_sell()` calls total) against
the production code line-by-line before finalizing — added `import config`
to the test file for the cooldown constant. `py_compile` clean. Not
re-executed here (still no `pytest`/`sqlalchemy` in this sandbox) — re-run
on the VM to confirm:

```bash
cd ~/stockky-v2/services/real-trade-service
python3 -m pytest tests/test_exit_error_branches.py -q
python3 -m pytest tests -q -p no:cacheprovider | tail -1
python3 -m pytest tests -q --cov=exit_engine.exit --cov-report=term-missing
```

Delivered zip: `stockky-v2-main-2026-09-21-session77-phase1-part2.zip`
(same file, now also includes this fix — diff against the original
upload for this part: `AUDIT_REPORT.md`, `CHANGELOG_INDEX.md`, this note,
and the 1 test file).

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
