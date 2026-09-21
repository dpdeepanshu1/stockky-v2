# Session 76 — circuit-limit exit resend-storm fix (2026-09-21)

Uploaded zip: `stockky-v2-main-2026-09-21-session-round3-watchlist-fix.zip`
(real-trade-service's round-3 `evaluate_mode` test-coverage work). User asked
to fix the remaining open items from that round's own report and re-deliver.

## What was open going in

Round 3's report listed `exit_engine/exit.py`'s `_send_real_sell` internals —
same-day CDSL/INTRADAY product-type branching and the DATAMATICS retry-storm
cooldown math — as the largest remaining coverage gap, alongside a handful of
`entry_engine/entry.py` branches (regime-cache TTL, Dhan-error classification
in the placement-failure handler, less-common `check_pending_fills`/
`expire_stale_orders` branches).

## What was found

Auditing `_send_real_sell`'s error-classification chain turned up one real
bug, not just an untested-but-correct path:

**The `is_circuit_limit_error` branch never set `_cutoff_key`.** Its own
comment already says a circuit-band SELL rejection is "permanent for the
session" — same category as `is_intraday_cutoff_error` and
`is_security_intraday_restricted_error`, both of which set `_cutoff_key` so
`_send_real_sell`'s very first check (`if load_snapshot(db, _cutoff_key):
return False`) skips any further Dhan call for the rest of the day. The
circuit-limit branch was missing that one line. Since it also correctly
never counts toward the generic exponential-backoff streak (a circuit hit
isn't a broker/account problem), nothing else was stopping a resend either —
a position stuck at its NSE circuit band got its SELL resent to Dhan every
single exit cycle (`EXIT_CHECK_INTERVAL_SECONDS`, ~45s) for as long as it
stayed circuit-locked. For a lower-circuit stock that can be hours. This is
exactly the DATAMATICS-style retry-storm class sessions 40 and 72 already
closed for every other placement-failure path — this branch (added session
41b, after most of that hardening) was simply missed.

## Fix

- `exit_engine/exit.py`: added `save_snapshot(db, _cutoff_key, {"hit": True})`
  in the `is_circuit_limit_error` branch, matching its sibling branches.
- Made the bottom-of-function `_excluded` set (which feeds `_bump_exit_failure`)
  explicitly list all three self-suppressing branches (oversell,
  intraday-cutoff, security-intraday-restricted, circuit-limit) instead of
  leaving two of them to rely on `is_persistent` happening to stay False.
  Purely a robustness/clarity change — behavior was already correct there,
  this just stops a future edit to the persistent-error list from silently
  changing it.

## Tests added

`tests/test_exit_circuit_limit_resend.py` (2 tests):
- a circuit-limit rejection is not resent on the next 5 simulated exit
  cycles (was: resent every time before this fix)
- `consecutive_exit_failures` stays at 0 for this failure mode (confirms the
  fix didn't accidentally start treating it as a persistent/broker-level
  failure, which would double-count against the wrong counter)

## Checked, no bug found

- `entry_engine/entry.py`'s `_get_market_regime` cache: correctly locked,
  correct TTL semantics, no bug.
- `entry_engine/entry.py`'s own `is_circuit_limit_error` handling on the BUY
  side: a rejected BUY just gets marked WAIT and re-evaluated fresh from the
  candidate engine next cycle — no resend-loop risk the way an already-open
  position's SELL has, so no equivalent fix needed there.
- `check_pending_fills` / `expire_stale_orders`: both already careful about
  not declaring an order dead until Dhan confirms it, matches their existing
  docstrings, no bug found.

## Verification

`python3 -m py_compile` and `python3 -m compileall` clean on both services.
No network access this session (same as several prior sessions) — `pytest`/
`sqlalchemy` aren't installed in this sandbox, so the new test file could not
actually be executed here. Traced its two assertions by hand against the
fixed code path instead of running them; recommend actually running
`python3 -m pytest tests/test_exit_circuit_limit_resend.py -q` on the VM
before relying on it.

Delivered zip: `stockky-v2-main-2026-09-21-session76-circuit-limit-fix.zip`.

## Still open

Everything else round 3 flagged as untested is unchanged: `_send_real_sell`'s
CDSL/insufficient-funds/oversell/exchange-not-allowed branches are still only
exercised indirectly, and `portfolio/portfolio.py`, `execution/auto_pilot.py`,
`manual_engine.py`, `execution/dhan_client.py`, `candidate_engine/candidates.py`,
`main.py`, the shared locks, and `watchlist_engine/*` all remain at the
coverage levels round 3's table recorded.
