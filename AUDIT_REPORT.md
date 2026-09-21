# Test & audit report — 2026-09-21

Scope: `services/position-stocks-service` (order placement, EOD square-off, reconciliation, screening) and
`services/real-trade-service/risk_engine`. All tests are offline: in-memory SQLite, scripted fake broker, no network.

## Result

| | before | after |
|---|---|---|
| position-stocks-service tests | 41 | **1,220** |
| position-stocks-service coverage (same measurement) | 38 % | **79 %** |
| real-trade-service tests | 40 | **375** (+2 pinned gaps) |
| real-trade-service coverage (same measurement) | 19 % | **39 %** |
| `risk_engine/engine.py`, `resilience/circuit_breaker.py`, `execution/reconcile.py` | 0 % | **99 % / 100 % / 99 %** |
| `entry_engine/entry.py` (pricing, sizing, drift, ranking, account state, expiry helpers) | 0 % | 38 % of the file — see "Still untested" |

Per module (position-stocks): `entry.py` 10→99 %, `eod_squareoff.py` 17→99 %, `reconcile.py` 40→99 %,
`breakeven.py` 15→100 %, `exit_retry.py` 50→100 %, `adaptive.py`→99 %, `screening/engine.py` 27→99 %,
`quality_gate.py` 25→100 %, `intraday_eligibility.py` 24→100 %, `circuit_breaker.py` 41→100 %.

Quality check: for every module the tests were run against deliberate breaks (flipped `>=`, removed capital
release, gate skipped, wrong sign, …). ~230 breaks in total; every one was caught except a handful that behave
identically to the original code (clock-dependent boundaries, guards made redundant by another guard).

## Bugs found — all fixed in this zip (`production_fixes.patch`, 4 files)

| # | Where | Bug | Impact | Fix |
|---|---|---|---|---|
| 1 | `position-stocks/orders/entry.py` `attempt_entry` | `from screening import intraday_eligibility` **inside** the circuit-limit branch made the name a function-local for the whole function → the INTRADAY-RESTRICTED (T2T/ASM/GSM) branch raised `UnboundLocalError`, swallowed as a warning | A restricted stock's rejection was **never recorded**, so the bot re-tried buying it every cycle | deleted the inner import |
| 2 | `entry.py` `attempt_entry` + `attempt_manual_entry` | only `SecurityNotResolvedError` was handled around `get_security_id()`; any other error (Dhan not connected, scrip-master download failure) propagated | ledger reservation **and** cross-service symbol lock leaked until restart | release both, re-raise |
| 3 | `entry.py` `attempt_manual_entry` | manual BUY with quantity ≤ 0 rejected *after* the symbol lock was claimed, never released | blocked real-trade-service from that symbol until restart / admin force-release | release lock before raising |
| 4 | `entry.py` (plain-MARKET fallback, `USE_SUPER_ORDER=false`) | the BUY's broker order id was discarded | entry could never be traced/verified | stored in `dhan_entry_order_id` |
| 5 | `position-stocks/orders/reconcile.py` `run_exit_reconciliation` | a TARGET/STOP fill landing on a position that was already an EOD/manual flat-SELL placeholder called `release_capital(position_value=capital_risked)` again | `available_capital` inflated by `capital_risked` (double release) | release 0.0 for placeholder rows (mirrors `_resolve_pending_with_price`) |
| 6 | `real-trade-service/risk_engine/engine.py` | BUY with qty ≤ 0, or stop ≥ entry, was **APPROVED** (`abs()` hid the sign) | protected upstream by entry/manual engines, but not for `/risk-engine/check` or any new caller | new check 4c → `invalid_order` |
| 7 | `real-trade-service/execution/reconcile.py` `reconcile_real_orders` | no per-order error isolation: one order whose booking raised escaped the whole pass; `main.py` only logged "self-heal failed" and never marked the reconcile done | the same order failed at the same point every cycle, so **every order queued behind it — including SELL confirmations — was never reconciled** | each order wrapped in `try/except` → rollback, `tally["errors"] += 1`, continue (diff is the wrapper plus re-indentation) |

Every fix has a regression test that fails on the original code and passes now
(`TestAuditRegressions`, `TestInvalidOrderGeometry`).

## Behaviours pinned, not changed — need a decision

* `orders/adaptive.py`: the docstring promises "never enter < 2:1 R:R", but with `MAX_STOP_PCT=5` and
  `MAX_TARGET_PCT=8` a wide-ATR stock gets stop 5 % / target 8 % = **1.6 : 1** (the target cap wins over the floor).
  Test: `test_floor_is_NOT_guaranteed_when_the_target_cap_binds__CURRENT_BEHAVIOUR`.
* `eod_squareoff.py` carry filter: if the ledger's `total_allocated_capital` is 0 the overnight pool-exposure cap is
  skipped entirely (`total_pool > 0` guard). `test_unknown_pool_size_disables_the_cap__CURRENT_BEHAVIOUR`.
* `entry.py`: a broker "success" with no order id is still recorded as an OPEN position with a blank id.
* `real-trade-service` `AccountState` comment says unpopulated capital fields "fail closed"; with broker cash and both
  position values all 0 the capital-share check is skipped. `test_zero_total_shared_value_skips_check__CURRENT_BEHAVIOUR`.
* **Partial fills are booked at the cumulative average price** (`real-trade-service/execution/reconcile.py`): Dhan's
  `averageTradedPrice` is the average of the whole order, but each newly-confirmed increment is booked at that
  cumulative average. 5 @ ₹100 then 5 @ ₹102 (cum. avg ₹101) becomes 5 @ 100 + 5 @ 101 → position avg ₹100.50 and cash
  debited ₹1,005 instead of ₹1,010; exits drift the same way. A proper fix needs each increment's own price — derivable
  for BUYs from `TradeFill`, but `record_real_exit_fill` writes no fill row, so it needs a small record/schema change.
  Pinned as a strict xfail: `TestKnownGaps.test_partial_fills_should_be_booked_at_the_increments_own_price`.
* **Shares that fill between the last reconcile and a stale-order cancel are never booked** (`entry_engine/entry.py`
  `expire_stale_orders`): it cancels at Dhan and marks the order EXPIRED, `cycle_runner` runs it *before*
  `reconcile_real_orders`, and reconcile only looks at PLACED/PARTIAL orders — so those shares are owned at Dhan but
  invisible here (the exact failure the function's docstring describes for the un-cancelled case). Narrow window, high
  severity. Pinned as a strict xfail: `test_shares_filled_just_before_the_cancel_should_still_be_booked`.
  Suggested fix: after a successful cancel, reconcile that order once (or re-read its book row) before marking EXPIRED.
* `entry_engine/entry.py` `_get_market_regime`: `int(data.get("market_score") or 50)` reads a genuine score of **0** as a
  neutral 50, so the worst possible market reads as healthy. Use `is None`. Pinned `..._CURRENT_BEHAVIOUR`.
* Once-per-day EOD gate: positions whose flat-SELL fails are **not** retried until tomorrow (documented in `main.py`).
  Pinned by `test_rejection_leaves_position_open_and_alerts`.

## Still untested (real-trade-service) — largest risk first

`entry_engine/entry.py::evaluate_mode` (the ~830-line candidate → order pipeline: gates 1-5.6, sizing, risk call,
order placement — only its helpers are covered), `exit_engine/exit.py` (27 %), `portfolio/portfolio.py` (13 %),
`execution/auto_pilot.py` (19 %), `manual_engine.py` (0 %), `main.py` (0 %), `candidate_engine/candidates.py` (0 %),
`watchlist_engine/*` (0 %), and in position-stocks `orders/overnight_stop.py` (79 %), `pipeline_status.py`,
`tz_utils.py`, `main.py`.

## CI

`.github/workflows/service-tests.yml` runs both suites on every push / PR (Python 3.11 = Dockerfiles, 3.12 = the VM).
