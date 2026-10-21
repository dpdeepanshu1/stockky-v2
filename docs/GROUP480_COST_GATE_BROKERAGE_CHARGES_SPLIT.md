# Group 480 (2026-10-21): the position-stocks cost gate counts percentage brokerage, plus scripts/charges_split.py

Why: the 21 Oct review asked whether brokerage and charges are what is losing money, and whether a bigger position per stock would cut them.
Checked against Dhan's published rate card (dhan.co/pricing) and the code.

## What the check found
* Dhan intraday brokerage is the LOWER of Rs 20 and 0.03 % of the order value, per executed order. Below about Rs 66,700 per order that is a percentage,
  so a bigger position does NOT lower the fee percentage; it scales profit and loss alike. A same-day round trip on Rs 3,000 costs about Rs 3.2 (0.11 %) at the code's own rates.
* real-trade's cost model and charges ledger already price brokerage as a percentage with the Rs 20 cap. **position-stocks' entry cost gate did not**: it used only
  `BROKERAGE_PER_ORDER` (default 0) as a flat fee, so it priced brokerage at Rs 0 and under-counted the round trip by about 0.06 %.
* The real-trade charges ledger already reports every component (`GET /charges/{mode}/cumulative` -> `total.components`: brokerage, stt, exchange, sebi, gst, stamp, dp).
  The group 470 note already records why September charges were high: one-day holds cost about Rs 16.8 each against Rs 2.0 same-day (delivery STT + DP charge), and
  `OVERNIGHT_HOLD_ENABLED` has defaulted to false since group 470.

## Changes
| Change | Setting | Default | Rollback |
|---|---|---|---|
| position-stocks cost gate: each leg costs max(`BROKERAGE_PER_ORDER`, min(cap, value x pct / 100)) | `SCALP_COST_GATE_BROKERAGE_PCT`, `SCALP_COST_GATE_BROKERAGE_CAP_RS` | **0.03**, 20 | `SCALP_COST_GATE_BROKERAGE_PCT=0` |

* Feeds only the entry cost gate (`orders/cost_gate.py`), not the charges ledger. Effect: a scalp whose target only just clears Rs 0 brokerage is now skipped
  (`COST_GATE:edge_to_cost=...`). With the usual 1.4-3.5 % targets the gate still passes.
* `scripts/charges_split.py` (read-only, standard library): splits an `export_records.py` file into same-day versus carried trades and into entry-value buckets
  (charges, charges per trade, charges as % of value, gross, net, how many trades still carry estimated charges). Run:
  `python3 scripts/export_records.py` then `python3 scripts/charges_split.py rt_*.json --since 2026-10-01`.
  Use it to see whether the charges are still high after 20 Oct (same-day trades should be near Rs 2 each).

## Not changed (on purpose)
* No larger positions and no leverage: with negative net expectancy a bigger size only loses faster. Raise `MAX_TRADE_VALUE` in steps, only after about 30 trades that are positive after charges.
* No live opening-range entry: the recorder (group 479) needs about 30 would-enter outcomes first.

## Tests
`position-stocks-service/tests/test_group480_cost_gate_brokerage.py` (8), `scripts/test_charges_split.py` (5; added to the CI scripts step and path filters).
