# Session 73 — cross-service audit fixes (2026-09-20)

Scope: 3 issues found in a full deep audit of `real-trade-service` and `position-stocks-service`
(requested as "audit properly and completely and list all open issues across these services").
Every item below says what was **changed in code** and what still **needs a live check** (nothing
here was run against a real Dhan account — no network/pip access in this sandbox, so no `pytest`
run either; verified via full `py_compile`/AST parse of every touched file plus a manual read of
the existing `tests/test_capital_share.py` / `tests/test_overnight_holds.py` to confirm neither
test's fixture data collides with the new behavior).

| # | Item | Services | Result |
|---|------|----------|--------|
| 1 | `capital_share_cap` blind spot | real-trade-service, position-stocks-service | Fixed — new shared-exposure table, both services now publish/read each other's open-position value |
| 2 | No exit-placement retry backoff | position-stocks-service | Fixed — mirrors real-trade-service's session40 `consecutive_exit_failures` design |
| 3 | Overnight holds not sector-diversified | real-trade-service | Fixed — new `OVERNIGHT_HOLD_MAX_PER_SECTOR` cap using the existing `NSE_SECTOR_MAP` |

## 1. `capital_share_cap` blind spot to the other service's holdings

**The bug:** `risk_engine/engine.py`'s `capital_share_cap` check (session52) computed
`total_shared_account_value = broker_cash_available + open_positions_market_value`, where
`open_positions_market_value` was always ONLY real-trade-service's own open positions — never
position-stocks-service's. Since both services share one real Dhan account, this undercounted the
true total by exactly whatever position-stocks-service was holding at the time, shrinking
real-trade-service's 50% cap below its genuine entitlement. Example: total account ₹100k
(RT holds ₹30k, PS holds ₹60k, ₹10k free) — the true 50% cap is ₹50k, but the old code computed
total=₹40k → cap=₹20k, rejecting RT's BUYs even though it was using only 30% of the account. Not
money-unsafe (it only ever makes the cap too small, never too large), but a real correctness bug
in a check whose entire purpose is a *fair* split, and it was silent — nothing logged that the
total was an undercount.

**The fix:** new shared table `stockky_shared_service_exposure`
(`models.py::SharedServiceExposure` in both services — same shared-table pattern as the existing
`SharedOrderBudget`/`SharedSymbolLock`, no migration needed since it's a brand-new table picked up
by each service's existing `create_all(checkfirst=True)`). Each service publishes its OWN
open-position market value there every sync cycle:
- position-stocks-service: `capital/ledger.py::sync_from_broker()` → `capital/shared_exposure.py::publish_own_exposure()`
- real-trade-service: `execution/equity_sync.py::sync_real_equity()` → `execution/shared_exposure.py::publish_own_exposure()`

real-trade-service now reads position-stocks-service's row and adds it into
`AccountState.other_service_open_positions_market_value` (new field, defaults to `0.0` — a lookup
failure or an unpublished row reverts to the OLD undercounting-but-never-unsafe behavior, never a
new failure mode) at all three `AccountState` construction sites: `entry_engine/entry.py`,
`manual_engine.py`, and `main.py`'s `/risk-engine/check` dry-run endpoint. `risk_engine/engine.py`'s
`capital_share_cap` check now sums all three terms for the true total.

position-stocks-service does **not** need to read real-trade-service's row back for its own sizing
— `capital/ledger.py`'s pool allocation already self-corrects off Dhan's live free cash, which
already reflects whatever real-trade-service has spent — but `get_other_service_exposure()` is
included on that side too for symmetry, unused today.

Fail-open everywhere (publish and read): a broken write/read must never itself block or corrupt a
real entry or exit.

**Live check:** once both services have run at least one sync cycle each with the deployed code,
`SELECT * FROM stockky_shared_service_exposure;` should show one row per service with a recent
`updated_at`. To confirm the fix actually changes behavior, check `/risk-engine/check` (REAL mode)
before and after position-stocks-service has an open book — `total_shared_account_value` in the
rejection message (if any) should now include position-stocks-service's holdings.

## 2. Exit-placement retry storm (position-stocks-service)

**The bug:** real-trade-service has had `TradePosition.consecutive_exit_failures` +
exponential-cooldown backoff since session40 (born from the DATAMATICS incident — 89 consecutive
REJECTED zero-fill exit-SELL attempts over ~4.5h with no backoff and no operator alert).
position-stocks-service never got the equivalent. Its fast loop calls `run_stagnation_exit()` every
cycle (~10-30s) against every OPEN position meeting stagnation criteria; if `_fire_flat_sell()`'s
**placement** itself kept failing (a persistent broker rejection — surveillance-restricted
security, margin shortfall — not merely a slow fill), the position never left "OPEN" status, so the
very next cycle retried it again with zero memory of the prior failure. Same unthrottled-retry
shape as the DATAMATICS incident, just at the placement step instead of the fill-confirmation step.
(A SELL that gets successfully accepted but later dies with zero fill is already handled correctly
by `reconcile.py`'s dead-order path, which moves the position to `ERROR` — outside the OPEN pool —
so that case was never at risk.)

**The fix:** new `ScalpPosition.consecutive_exit_failures` / `last_exit_failure_at` columns
(additive migration in `db.py`), new config knobs `EXIT_RETRY_BASE_COOLDOWN_SECONDS` (60) /
`EXIT_RETRY_MAX_COOLDOWN_SECONDS` (900) / `EXIT_RETRY_ALERT_THRESHOLD` (5) — same names/defaults as
real-trade-service's — and a new module `orders/exit_retry.py` (`check_cooldown` /
`record_failure` / `reset`). Wired into `orders/eod_squareoff.py::_fire_flat_sell`: checks cooldown
before any broker call, resets the streak the moment a placement succeeds (removing the position
from this loop's retry surface entirely, regardless of the order's eventual fill), and records a
failure (with alerting) only once every bounded retry within that single call is exhausted or a
permanent-rejection class is hit. Applies uniformly to every `_fire_flat_sell` caller — EOD
squareoff, manual exit, and stagnation exit — same precedent as real-trade-service gating both
automatic and manual exits through one shared function.

**Live check:** none available offline. Once deployed, a position whose exit keeps failing at
placement should show a 🚨 Telegram alert after 5 consecutive failures and stop being retried every
single cycle (watch the fast-loop logs for `exit-retry: ... in exit-placement backoff` lines
between attempts).

## 3. Overnight-hold sector/correlation diversification (real-trade-service)

**The gap:** session72 added `OVERNIGHT_HOLD_MAX_POSITIONS` (3) and
`OVERNIGHT_HOLD_MAX_SINGLE_SYMBOL_PCT` (15%), but ranking inside `_select_overnight_holds` was
still pure conviction-score — three highly-correlated same-sector names could still be held
overnight together, concentrating gap risk in one overnight catalyst (a sector-wide headline, a US
peer-sector selloff) instead of spreading it. Flagged in session71, never closed.

**The fix:** new `config.OVERNIGHT_HOLD_MAX_PER_SECTOR` (default 1 — no two recognized-same-sector
names held overnight at once). Reuses the existing `market_context/sector_signal.py::NSE_SECTOR_MAP`
(the same map already used for the overnight US-sector signal) rather than inventing new sector
data. A symbol with no recognized sector (the map is deliberately partial, per its own docstring)
is never compared against another symbol for this specific check — there's genuinely nothing to
compare — so it can't be blocked by, or block, anything else on sector grounds; it still only ever
occupies its own single "slot," so the existing `OVERNIGHT_HOLD_MAX_POSITIONS` cap still applies to
it normally.

**Live check:** none available offline (no network access to verify `NSE_SECTOR_MAP` coverage
against today's actual candidates). Watch the next multi-candidate overnight-hold night — the
kept reasons string now appends `, sector=<SECTOR>` for any recognized symbol, so a quick look at
that field confirms the cap is actually binding when it should.

## Files changed this session
- `services/position-stocks-service/capital/ledger.py`
- `services/position-stocks-service/capital/shared_exposure.py` (new)
- `services/position-stocks-service/config.py`
- `services/position-stocks-service/db.py`
- `services/position-stocks-service/models.py`
- `services/position-stocks-service/orders/eod_squareoff.py`
- `services/position-stocks-service/orders/exit_retry.py` (new)
- `services/real-trade-service/config.py`
- `services/real-trade-service/entry_engine/entry.py`
- `services/real-trade-service/execution/auto_pilot.py`
- `services/real-trade-service/execution/equity_sync.py`
- `services/real-trade-service/execution/shared_exposure.py` (new)
- `services/real-trade-service/main.py`
- `services/real-trade-service/manual_engine.py`
- `services/real-trade-service/risk_engine/engine.py`

Verified: full `py_compile` across every `.py` file in both services (clean), diffed the whole
extracted zip against the original upload to confirm exactly these 15 files changed and nothing
else, manually traced every `AccountState(` construction site (4 total, including the offline test
harness which is intentionally untouched — it's a synthetic DEMO-only harness with no
`broker_cash_available` concept at all) and every `_fire_flat_sell` call site.

## Still open (not addressed this session — out of the requested scope)
- Oracle Autonomous DB session-cap headroom (session33, infra not code — periodic
  `SELECT status, COUNT(*) FROM v$session GROUP BY status` check recommended, no code fix applies).
