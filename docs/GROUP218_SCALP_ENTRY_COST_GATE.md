# Group 218 - scalp entry cost gate (position-stocks-service)

Cumulative on group 217. Item 5 of the 2026-10-07 loss/profit review. Rebuild: `docker compose build position-stocks-service && docker compose up -d`.
Files changed: `orders/cost_gate.py` (new), `orders/entry.py` (one check in `attempt_entry`), `config.py` (9 settings),
`tests/test_group218_cost_gate.py` (new, 20), `tests/test_entry.py` (+5 in `TestCostGate`).

## What the review found, and a correction
real-trade-service compares a candidate's expected edge with its round-trip transaction cost (`cost_model.py`, Gate 5.6) but this service never did, so a
scalp was bought whatever it cost. That part stands.

Correction to my own review: I wrote that a round trip costs "about 0.1%, roughly a third of the +0.3% average win". That figure was not derived from the repo.
With this repo's rates (Rs 0 brokerage) the statutory levies on an INTRADAY round trip are about **0.04%** of trade value (STT 0.025% on the sell, exchange
0.00325% x2, SEBI, GST on those, stamp 0.003% on the buy). About 0.1% is only reached once you add a spread/slippage allowance. Also: recorded P&L in this
service is price-based, there is no charges field, so the +0.3% / -1.3% averages are gross of all costs.

## Change
`orders/cost_gate.py` is a standalone INTRADAY copy of the real-trade estimate (services do not share code at runtime).
`attempt_entry` calls it after the quantity is settled: `edge = qty * entry * target_pct` against
`cost = levies(buy at entry, sell at target) + SCALP_COST_SLIPPAGE_ALLOWANCE_PCT of trade value`. If `edge / cost < SCALP_MIN_EDGE_TO_COST_RATIO` the entry is
skipped with a `COST_GATE:edge_to_cost=...` reason (edge, cost in Rs and %, qty, value, target), the capital reservation and symbol lock are released, and no
order or shared-budget slot is used.

Exempt: the forced 1-share first live order (a deliberate probe), and manual entries (`attempt_manual_entry`, your own decision). Fails open on bad inputs or
a config error.

| Env | Default | Meaning |
|---|---|---|
| `SCALP_COST_GATE_ENABLED` | 1 | `0` turns the gate off |
| `SCALP_MIN_EDGE_TO_COST_RATIO` | 3.0 | same default as real-trade-service |
| `SCALP_COST_SLIPPAGE_ALLOWANCE_PCT` | 0.10 | my assumption for spread + slippage per round trip; adjust from real fills |
| `BROKERAGE_PER_ORDER` | 0 | flat Rs per leg; same env name as real-trade-service, so one .env value serves both |
| `STT_INTRADAY_SELL_PCT`, `EXCHANGE_TXN_PCT`, `SEBI_TURNOVER_PCT`, `GST_PCT`, `STAMP_DUTY_BUY_PCT_INTRADAY` | as real-trade | statutory rates |

## Honest limit: at the default Rs 0 brokerage this gate passes almost everything
Cost is about 0.14% (levies plus the 0.10% allowance) against targets of about 1.4-3.5%, a ratio of roughly 10-25 against a minimum of 3. The gate only
starts rejecting when `BROKERAGE_PER_ORDER` is set, when the target is very small, or when the allowance is raised. Worked example (entry Rs 500, qty 40,
target 1%, edge Rs 200): at Rs 0 brokerage cost is Rs 27.24 (ratio 7.34, allowed); with Rs 20 per order cost is Rs 74.44 (ratio 2.69, skipped).
So this is a safety net that is only as good as the brokerage figure. Please set `BROKERAGE_PER_ORDER` (and check the other rates) from a real Dhan contract
note. I did not assume your plan.

It also does not address the larger issue the review found: an average win of +0.3% against an average loss of -1.3%. A ratio check at the target does not see
that trades rarely reach the target; the trailing stop (group 217) and the ranking/entry-timing items (#4, #6) are what act on that.

## Tests
- `tests/test_group218_cost_gate.py` (20): levies hand-calculated for a Rs 1000 buy sold at Rs 1010, flat brokerage with GST, STT on the sell only, linear scaling,
  ratio exactly at the minimum, tiny target, readable reject text, switch off, allowance flipping the result, unusable inputs and config errors failing open.
- `tests/test_entry.py::TestCostGate` (5): normal entry unaffected; flat brokerage gives a clean skip (capital, lock, orders, budget all untouched); gate off;
  forced first order not gated; a skipped symbol can trade later.
- Sandbox: those files pass (139 with `test_entry.py`, `cost_gate.py` 100% covered). Whole position-stocks suite: 2802 passed, 2 failed - the same 2 as on the
  group 216 zip (`test_group192...two_dead_entries_today`, time-of-day dependent; `test_group210...throttled_to_one_pass_per_interval`). api-gateway env sweep
  guard tests (27) still pass.
