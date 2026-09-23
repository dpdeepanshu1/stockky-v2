# Session 7 (2026-09-12) — full audit: circuit breaker bug, dead code, shared order-budget ghost feature

Scope: entire `services/position-stocks-service/` and `services/real-trade-service/`
codebases (pyflakes run repo-wide, not just touched files), plus `docker-compose.yml`.
Full detail in TRACKING.md §3.14 and STATUS.md rows 16-18 — this is a pointer.

## 1. Real bug: circuit breaker reset never actually reset
`resilience/circuit_breaker.py`'s `is_open()` declared `global _open_since` only,
not `_failure_count` — so `_failure_count = 0` inside it created a local shadow
variable instead of resetting the module-level counter. After the 60s reset
window, `is_open()` correctly returned False once, but the stale (>= threshold)
count never cleared, so one more failure would instantly re-trip the breaker.
Fixed: added `_failure_count` to the `global` declaration.

## 2. Dead code cleanup (pyflakes-driven, both services)
Unused imports/variables in `main.py`, `screening/engine.py`,
`execution/dhan_client.py` (also removed a dead `global` on a read-only var),
`feed/angelone_session.py`, `feed/scrip_master.py`, `feed/ws_client.py`. Both
services now pass `pyflakes` with zero findings across their entire codebases.

## 3. Significant finding: §3.8's shared order-rate guard was pure documentation
TRACKING.md/STATUS.md described a shared Dhan account-wide order-rate guard as
fully built across several sessions, with specific claimed file paths and
wiring. A repo-wide search found **zero matches** for `shared_order_budget` in
any `.py` file on either service — none of it ever existed. Built for real:

- `SharedOrderBudget` model (table `stockky_shared_order_budget`) added to
  both services' `models.py` — same table/columns, separate `Base` per
  service (safe: `create_all()` is idempotent, whichever service boots first
  creates it).
- `position-stocks-service/capital/shared_order_budget.py` +
  `real-trade-service/execution/shared_order_budget.py` — duplicated logic,
  fail-open on any DB error.
- position-stocks-service: gated check in `orders/entry.py` (right before the
  actual Dhan call, not the earlier gates — avoids over-counting attempts
  rejected for unrelated reasons), unconditional record in
  `orders/eod_squareoff.py`, new `/status` field.
- real-trade-service: gated check in `manual_engine.py`'s manual REAL BUY path
  only (NOT the automatic entry path, matching the original design),
  unconditional record in `exit_engine.py`'s `_send_real_sell` (shared by
  both AUTO and manual sells).
- `SHARED_DAILY_ORDER_BUDGET` (default 5000) in both `config.py`s, optional
  override documented in `docker-compose.yml` for both services.
- Confirmed NOT needed: `orders/reconcile.py` doesn't place any Dhan orders
  itself (pure polling), so no wiring belongs there. Position-stocks-
  service's own `/kill` endpoint also doesn't place orders (only disarms +
  trips a flag) — the doc's "manual kill-switch closes" language describes
  real-trade-service's own behavior, not this service's simpler kill switch.

## 4. Minor doc fix
STATUS.md's Next Steps item 9 (a proposed `/candidates/log` endpoint) had a
stray "requires live data" line copy-pasted from the item above it — that
endpoint is actually buildable from a sandbox anytime (pure DB read, no
external calls). Corrected.

## Verification done in sandbox
- `pyflakes` clean on both services' ENTIRE codebases (not just touched files).
- `py_compile` clean on every file touched this session in both services.
- Handled real-trade-service's changes carefully given it trades real money
  live — minimal, surgical insertions at exactly the order-placement points,
  no restructuring of surrounding logic.
- NOT verified: an actual live boot of either service with this new table/
  code — that requires the live VM (still blocked on session 5's nginx fix
  being applied) and, for the manual-BUY gate specifically, a live Dhan
  session to confirm the RuntimeError path actually surfaces as a clean
  REJECTED order rather than an unexpected 500.
