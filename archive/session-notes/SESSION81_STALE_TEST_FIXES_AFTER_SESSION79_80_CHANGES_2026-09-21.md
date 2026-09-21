# Session 81 — stale test fixes surfaced by `stockky_full_test.sh` (2026-09-21)

`stockky_full_test.sh` reported 3 FAILED tests across two services. All three
turned out to be tests that hadn't been updated after two deliberate,
already-documented production changes from sessions 79 and 80 — not
regressions in application code. No application/service code changed in
this session; only the two test files below.

## 1. real-trade-service: `test_cost_model.py::test_gate_uses_config_defaults_when_no_override_given`

Failure: `assert 50.0 < 20.0`.

Cause: session79 (see `config.py`, `MIN_TRADE_VALUE` comment) deliberately
dropped `MIN_TRADE_VALUE`'s default from `3000.0` to `20.0` at the user's
explicit request ("no min trade value... if needed set one like 10 or 20
rs"). The test's ₹50 trade_value (`entry_price=50.0, qty=1`) cleared the
new ₹20 floor, so `result.trade_value < config.MIN_TRADE_VALUE` was no
longer true — the test was asserting against the old ₹3,000 floor.

Fix: `entry_price` dropped to `5.0` so `trade_value=5.0` stays under the
current ₹20 floor regardless of which value is configured, preserving the
test's original intent (a tiny trade should fail the gate).

## 2. position-stocks-service: `test_reconcile.py::TestRunExitReconciliation::test_dead_exit_legs_flag_the_position_for_a_plain_sell` (both parametrized cases)

Failure: expected `p.status == "EXIT_LEGS_REJECTED"`, got `"OPEN"`.

Cause: session80 (see `orders/reconcile.py`'s dead-leg-detection comment,
the fix that motivated this zip's filename) deliberately changed the
EXIT_LEGS_REJECTED trigger from "either exit leg dead" to "every exit leg
that exists on the order is dead." This was the actual fix for the
REFEX/LLOYDSENT sync gap: previously, if only the TARGET_LEG rejected while
STOP_LOSS_LEG was still live and later filled for real on Dhan, the old
single-leg trigger flagged the position early and reconcile stopped polling
it, so the real fill (price, P&L, capital release) was never picked up.
The test's two parametrized cases (`REJECTED`+`PENDING`,
`PENDING`+`CANCELLED`) each had exactly one dead leg — precisely the case
the fix now leaves alone.

Fix:
- `test_dead_exit_legs_flag_the_position_for_a_plain_sell` parametrize
  cases updated so **both** legs are dead in each case
  (`REJECTED`+`CANCELLED`, `EXPIRED`+`REJECTED`) — this is the case that
  should still flag `EXIT_LEGS_REJECTED`.
- New test `test_single_dead_leg_does_not_flag_while_the_other_leg_is_still_live`
  added, covering the original two single-dead-leg parametrize cases,
  asserting the position stays `OPEN` — this directly locks in the
  REFEX/LLOYDSENT fix so it can't silently regress back to the old
  either-leg-dead behavior.

## Verification

```
services/real-trade-service:        475 passed, 1 xfailed
services/position-stocks-service:  1220 passed, 1 skipped
services/decision-prediction-service: 13 passed
services/market-data-service:         12 passed
```

No `FAILED` lines remain in any suite. Live-endpoint checks
(`/api/system/health`, per-service `/health`, REAL-mode status/positions)
were all green in the run that prompted this cleanup and are unaffected by
this session (test-only changes).
