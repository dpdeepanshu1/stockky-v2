# E187 - position-stocks: every cycle re-logged and re-wrote the same quality-gate rejections

Includes E186 (and E185, E184).

## What I read
`main.py::_run_cycle` from the top to the end (reconcile, eDIS and overnight-stop checks, short square-off, the EOD gate, the enable / armed / entry-window gates, the
capital sync, scan, the intraday-restricted / window-health / cooldown / unaffordable / price-floor filters, the concurrent quality gate, the shortlist and the entry
loop), `_trading_loop`, and `orders/entry.py::_log_candidate / log_quality_reject`.

## Finding (fixed)
The top-N ranked names are quality-gated on every ~15 s cycle. One that fails (CENTRUM: fundamental_score 12 < floor 25; MRPL / TDPOWERSYS: EVENT_CHECK_UNAVAILABLE)
fails again with the same reason, and each time the cycle wrote an INFO line AND a `ScalpCandidateLog` row (`db.add` + Oracle commit) - the same pair twice in 15 s in
the 2026-10-26 log. Over a session that is hundreds of duplicate audit rows that bury the dashboard's candidate log, plus one extra Oracle round trip per rejected
name per cycle (the log shows `oracle slow exec` warnings).

## Change (main.py)
- The quality-gate reject line and the audit row are written when the symbol's reason first appears, changes, or is older than `_SKIP_LOG_REPEAT_S` (300 s); otherwise
  the line goes to DEBUG and no row is written. Same helper as E185 (`_skip_is_new`), keyed `Q:<symbol>` so it never collides with the entry-gate skip state.
- Nothing about the decision changes: the candidate is still rejected, still listed in the cycle's `checked` stage detail, and a reason change is logged at once.
- No new environment variable, column or migration.

## Read and found fine (no change)
- Order of the gates, the EOD sweep staying unconditional (with E183's retry: while the day is not marked done each cycle calls the sweep and returns), the
  entry-window cutoffs, the circuit breaker gating only the entry attempt, capital-starved fall-through to the next pick, `_finalize` stamping `last_cycle_run_at` on every path.
- Cosmetic: the EOD stage detail always says "flattened all open positions", even on a retry pass that still has failures (the alert for that comes from E183's give-up).

## Tests
Two added to `tests/test_log_dedupe_e185.py` (separate keying; same reason not new until stale). Not run with pytest here (not installed); the helper was run directly in E185.
