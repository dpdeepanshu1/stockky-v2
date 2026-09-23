# Session 86 (2026-09-23): execution/auto_pilot.py — orchestration coverage round

## Summary

Session85 closed `execution/auto_pilot.py`'s self-contained helper
functions (19% → 31%) and deliberately deferred the cycle-orchestration
layer — `_full_tick_body`, `_prepick`, `_eod_squareoff`, `_eod_signal_scan`,
`_schedule_tick_body`, the five background loops, and `start()` — because
it needs fixture-level mocking of the broker/feed/risk layers rather than
being self-contained. This session picks that up.

**This sandbox has no network access**, unlike session85's. That means
these tests were not run through a live pytest+coverage pass here — same
limitation noted in sessions 76, 77, and 82c. Verified structurally
instead (see "Verification" below). **User should confirm with a real
`pytest --cov` run on the VM.**

## What was added

`tests/test_auto_pilot_orchestration.py` — 120 tests, no production code
touched.

| Area | What's covered |
|---|---|
| Lock wrappers | `_run_exit_tick_sync`/`_run_full_tick_sync`/`_run_schedule_tick_sync` — skip-when-held, run-and-release, release-on-exception; `_exit_only_tick`/`_full_tick`/`_schedule_tick` top-level async wrappers incl. the weekday skip |
| `_exit_only_tick_body` | market-closed no-op, notify-on-activity, no-notify-on-no-activity, DEMO-skips-reconcile, REAL-reconciles-when-due, gate-off alert + its note appended to an activity notification, exception path never raises |
| `_full_tick_body` | not-armed/no-gate-row/disabled all alert-and-return, market-closed no-op, auto-disarmed notify, activity summary, heartbeat vs. no-heartbeat, exception path |
| `_select_overnight_holds` (net-of-costs branch) | excludes a position whose gross P&L doesn't clear the round-trip cost model, keeps one whose does — session85 only exercised `OVERNIGHT_HOLD_PROFITABLE_NET_OF_COSTS=False` |
| `_requeue_overnight_priority_candidates` | snapshot-read exception, no-snapshot, already-consumed, stale/missing trading-date, no-picks, new-candidate insert, already-queued skip, save-failure swallowed |
| `_inject_nextday_watchlist_candidates` | no-rows, above/below-threshold, already-queued, preview-price-lookup failure non-fatal, flaky-commit rollback-and-retry, outer exception rollback |
| `_prepick` | basic notify, top-symbols with 🌙 tag, carried-over line, US-sector-signal bonus + its failure being non-fatal, >10-candidates overflow line |
| `_enter_at_open` | auto-disarmed notify-and-return, entry-summary notify |
| `_edis_morning_check` | DEMO no-op, no-CNC-pending no-op, summary-exception swallowed, already-verified no-op, not-verified alert, ambiguous-status alert |
| `_eod_squareoff` | no-positions no-op; DEMO close (success/failure/missing-tick); REAL sell-per-position, pending-sell skip, send-returns-False and send-raises both counted failed; overnight holds excluded + stamped + logged as `TradePositionEvent` |
| `_eod_signal_scan` | nothing-queued (no candidates/below-conviction/wrong-label), queue-only snapshot save, high-conviction same-day entry, not-filled fallback, entry-evaluate exception fallback, disarmed-skips-entry, `MAX_CANDIDATES` cap keeps the higher-conviction pick |
| `_schedule_tick_body` | no-gate/not-armed short-circuits; each of the 5 scheduled automations firing when due, skipping when already run or gated by time/market-open, its own exception logged+notified without aborting the others; EOD square-off's exit lock acquired+released even on exception; outer exception swallowed |
| 5 background loops | `_schedule_loop`, `_fast_exit_loop`, `_full_cycle_loop`, `_totp_refresh_loop`, `_afterhours_scan_loop` — each broken out via a sentinel exception after N sleeps, confirming DEMO-then-REAL ordering and that one mode's exception never stops the other or the loop; TOTP loop additionally covers disabled/not-needed/refresh-False/exception-swallowed |
| `_afterhours_scan_body` (remaining branches) | gate-not-found, feature-disabled, outside-window, manual bypasses both checks, finalize pass notify-on-shortlist vs. silent-on-empty, regular scan success recording, exception recording failure + reason, and that recovery block's own nested `db.query` failure still not propagating |
| After-hours lock + manual trigger | `_get_afterhours_lock` reuse; `_run_afterhours_tick_sync` skip/run; `run_afterhours_scan_manual_sync`'s `already_in_progress` vs. run-and-release |
| `start()` | creates all 5 tasks from scratch, no-op when all already running, recreates any task found `done()` |

## Verification (no network in this sandbox)

1. `python3 -m py_compile tests/test_auto_pilot_orchestration.py` and
   `execution/auto_pilot.py` — both compile clean.
2. AST sweep: every `ap.<name>` referenced in the test file (30 distinct
   names) resolves to a real top-level def/assignment or import in
   `auto_pilot.py` — the one "miss" (`asyncio`) is the module's own
   `import asyncio`, accessible as `ap.asyncio`, not a real gap.
3. Every dotted `monkeypatch.setattr("a.b.c", ...)` string target used
   (22 distinct paths, e.g. `exit_engine.exit.evaluate_mode`,
   `cycle_runner.run_cycle_core`, `portfolio.portfolio.open_positions`,
   `market_feed.feed.{get_quotes,get_preview_quotes}`,
   `entry_engine.entry.evaluate_mode`,
   `resilience.local_cache.{load,save}_snapshot`,
   `watchlist_engine.afterhours_scan.{run_afterhours_scan,
   finalize_nextday_watchlist}`, `auth.dhan_credentials.
   {token_needs_refresh,refresh_if_totp_enabled}`, etc.) resolves to a
   real function definition in the target module file.
4. Every model kwarg/attribute the test helpers (`_gate`, `_position`)
   and individual tests use — `TradeGateState.{armed,auto_pilot_enabled,
   prepick_enabled,prepick_last_run,edis_morning_check_enabled,
   edis_check_last_run,enter_at_open_enabled,enter_at_open_last_run,
   eod_squareoff_enabled,eod_squareoff_last_run,eod_signal_scan_enabled,
   eod_signal_scan_last_run,afterhours_news_scan_enabled,
   afterhours_finalize_last_run,afterhours_scan_last_run_ok}`,
   `TradePosition.{broker_imported,entry_decision_label,
   entry_conviction_score,overnight_hold_reason}`,
   `TradeCandidate.{consumed,conviction_score,decision_label,
   overnight_priority,signal_price}`, `NextDayWatchlistEntry.{catalyst_type,
   consumed,market_date,priority_score}` — exists on the matching class in
   `models.py`.

This catches name/signature drift (a renamed function, a removed config
key, a model column that no longer exists) but **not** runtime logic bugs
— a test can be structurally sound and still fail on real behavior. The
same caveat applied to sessions 76, 77, and 82c, all of which turned out
clean when later confirmed on the VM (session83's real run, and session85's
real run both showed no failures from the hand-verified rounds).

## Expected result once run on the VM

```
python3 -m pytest tests/test_auto_pilot_orchestration.py -q \
    --cov=execution.auto_pilot --cov-report=term-missing
```

should show `execution/auto_pilot.py` moving from 31% (516/750 missing) to
roughly 99% (a handful of lines still open — likely a couple of deeply
nested defensive sub-branches, e.g. inside `_afterhours_scan_body`'s
exception-recovery path, that only show up with `--cov-report=annotate`).
Combined with `tests/test_auto_pilot_helpers.py` (session85, 56 tests) this
brings the total new test count for `auto_pilot.py` across both rounds to
176.

## Cleanup done in this round

The draft test file had `_FakeTick`/`_account` helper classes defined
twice (once near the top for the tick-body tests, redundantly again before
`TestRequeueOvernightPriorityCandidates`) — harmless in Python (later
def just shadows the earlier one) but sloppy. Deduped to a single
definition each before finalizing.

## Next up

1. Run the new test file on the VM to get the real number and see exactly
   which ~11 lines (if any beyond estimate) are still missing; close them
   with a short final pass if it's a handful of one-off branches.
2. `candidate_engine/candidates.py` — 0%, 2077 lines, never touched by any
   test. Largest remaining gap in the whole plan.
3. Re-run coverage with the corrected `--cov=intraday_eligibility` flag
   (repo-root module, not `execution.intraday_eligibility`) to get its
   real number instead of the "never imported" warning.
