# Session82c — real-trade-service, exit_engine/exit.py

100%-coverage-plan Phase 1 #1 continuation, "check against current file" line
for the per-position dispatch loop inside evaluate_mode().

## Bug found and fixed

evaluate_mode()'s per-position body (everything from `held_days = ...` through
the final HOLD fallback branch) ran with NO exception isolation. Any
unexpected exception raised while evaluating ONE position — a bad
watchlist_entry_id in `_load_profile`, a malformed tick, a DB hiccup mid-loop
— propagated straight out of `evaluate_mode` and aborted the entire cycle:
every other open position in that mode got zero stop/target/emergency-exit
evaluation for that cycle, with no alert and no audit trail record. Same
incident class as session65's portfolio.import_broker_holdings fix (one bad
item must not drop every other item in the same batch) — just never applied
to this loop.

Fix: wrapped the per-position body in try/except. On failure: roll back any
partial writes from the failed position, log a HOLD exit-decision explaining
the skip (visible in the audit trail), increment `held`, and continue to the
next position. Other positions in the same cycle are unaffected.

Also closed a real, previously-fully-untested gap: `_load_profile` (the
function that resolves each position's per-catalyst exit profile — trail
schedule, breakeven trigger, max hold days, partial-exit fraction) and
`_trail_atr_mult` (age-based ATR trailing-stop multiplier) had zero direct
test coverage anywhere in the suite despite running on every single
evaluate_mode pass. Added tests for: manual-position defaults,
volume_shock's short-horizon routing (session12's fix), watchlist-sourced
horizon_class lookup, missing-WatchlistEntry graceful fallback, the default
schedule's age buckets, a custom schedule override, and the schedule-exhausted
fallback-to-last-entry branch.

New file: tests/test_exit_position_isolation_and_profile.py (10 tests).

## Verification

No pytest/sqlalchemy available in this sandbox (network disabled) — verified
via `python3 -m py_compile` + `python3 -m compileall` on the whole service,
plus an `ast`-based structural check confirming the try/except wraps exactly
the intended region without disturbing the for-loop's control flow, and a
careful hand-trace of each new test against the actual code paths. Run for
real on your own VM to confirm:

    cd services/real-trade-service
    python -m pytest tests/test_exit_position_isolation_and_profile.py -v

## Still open per 100_PERCENT_COVERAGE_PLAN.md (Phase 1 #1, exit.py)

- Small helper/import guards: `_clamp_for_atr`'s ImportError fallback
  (lines ~67-69) — needs faking the `return_sanity` import missing, low
  value, cheap to do next session.
- Everything from Phase 1 #2 onward (portfolio.py, manual_engine.py,
  dhan_client.py classifier table, auto_pilot.py sub-targets,
  candidate_engine/candidates.py) is unchanged — see the plan doc.
- CDSL/insufficient-funds/oversell/exchange-not-allowed/cutoff-sibling/
  generic-streak branches (the plan's other big exit.py row) turned out to
  already be fully covered by tests/test_exit_error_branches.py, which was
  present in this session's uploaded zip but predates this chat thread —
  no new work needed there, just confirmed by reading it.

## Unrelated: lock-leak fix verification (from pasted terminal log, not
   re-verified by this session, just noted)

The user's pasted log (session82b) shows
test_angelone_session_cross_loop_lock.py's 2 tests passing on their VM, and a
before/after ReadTimeout comparison (source-2 fallback / live_quotes read
failures) trending down after a restart — consistent with a previously
applied market-data-service lock-leak fix holding up in production. No code
changes made for that in this session; it's confirmation data only.
