# Group 215 - entry cash is claimed best-conviction-first (real-trade-service)

Cumulative on group 214. Source: audit item A7 of group 155 ("capital check order"). Rebuild: `docker compose build real-trade-service && docker compose up -d`.
Files changed: `entry_engine/entry.py` (+ tests).

## What was wrong
`evaluate_mode` fetches up to 20 unconsumed candidates ordered by `received_at` and walks them in that order. Each candidate that clears the gates and
risk_engine is staged and its cost is added to `reserved_cash` at once, so the NEXT candidate is sized and cash-checked against what is left. Gate 6
(cross-candidate ranking) runs only after the loop and only ranks candidates that were already approved. So whoever was queued first claimed the
cash / per-trade room first: a weak early candidate could use it up and a stronger one queued seconds later was rejected for cash before Gate 6 ever
saw it. Reserved cash is deliberately not released for candidates Gate 6 later drops (documented in the code), so the effect lasted the whole cycle.

## Fix
- New `_order_candidates_for_capital`: after the fetch, the batch is walked highest `conviction_score` first. The sort is stable, so equal convictions keep
  `received_at` order; a missing conviction counts as 0 (last); any error leaves the order unchanged.
- Only the ORDER changes. The 20-row fetch (still oldest first), every gate, the cash maths, Gate 6 and the placement code are untouched.
- `ENTRY_CAPITAL_ORDER_BY_CONVICTION=0` (or false/no/off) restores `received_at` order. Blank / anything else = on.

## Side effect to know
If two unconsumed rows exist for the same symbol, the duplicate-symbol guard keeps whichever is walked first, which is now the higher-conviction row
(before: the older row). The other is WAIT'd as before.

## Not changed
- Conviction is the largest weight of Gate 6's composite (0.65) but not the whole of it, so the walk order is an approximation of Gate 6's order, not a copy.
  A full fix (rank everything first, then check cash in that order) would restructure the loop; not done here.
- The `received_at` / limit-20 fetch: a 21st candidate is still not seen this cycle.

## Tests
- New `tests/test_group214b_capital_order.py` (13): helper (order, ties, None, env off, single/empty, bad value) and `evaluate_mode` with a risk stub
  that has room for one entry: the stronger, later-queued candidate now gets the slot; with the env off the older one does (old behaviour); every
  candidate is still evaluated and consumed.
- Sandbox: full real-trade-service run 3425 passed, the same 4 failed + 1 error as on the uploaded zip (group171 x4, group172 x1; they pass alone).
