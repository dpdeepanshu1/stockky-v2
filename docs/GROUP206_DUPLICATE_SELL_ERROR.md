# Group 206 — a rejected duplicate SELL no longer turns a filled trade into ERROR (item 11)

## Problem
AVALON: bought 2359.20, sold 2412.70 (+₹53.50 at Dhan), stored as ERROR with ₹0. A duplicate SELL was
sent and the second one was rejected.

## Root cause
Two gaps in position-stocks-service:
1. Nothing checked whether the position had already exited before sending the flat SELL
   (stagnation / EOD / manual). Cancelling the super-order legs does not undo a leg that already
   filled, so the SELL went out on a flat position (rejected at best, an unintended short at worst).
   A retry after a client-side timeout could also duplicate an order Dhan had in fact accepted.
2. `_reconcile_eod_pending` treated any REJECTED/CANCELLED flat SELL as "position still open" and
   wrote ERROR + `*_SELL_DEAD`, re-claimed capital and the symbol lock, with no look at the exit
   that really filled.

## Fix
- `orders/eod_squareoff.py::_fire_flat_sell` (used by EOD sweep, manual exit and stagnation exit):
  - before sending, reads the super order; if TARGET_LEG or STOP_LOSS_LEG is already filled it raises
    `PositionAlreadyFlat` and sends nothing (the row stays OPEN and the normal reconcile books the leg
    fill; manual exit answers "Already closed").
  - before each RETRY, looks for a live/filled SELL of the same security and quantity in today's order
    book and adopts it instead of resending.
  - both checks fail open: a lookup error (e.g. a 403) never blocks an exit.
- `orders/reconcile.py::_reconcile_eod_pending`: on a dead flat SELL, first looks for the real exit.
  - a filled bracket leg: the row stays pending and the super-order pass books TARGET_HIT/STOP_HIT;
  - else exactly one other filled SELL (same security, exact quantity, not claimed by another position):
    its fill is booked and `dhan_exit_order_id` is repointed to it;
  - otherwise ERROR as before (nothing proves the position is flat).
- Repair for rows already damaged today: `POST /reconcile/repair-dead-sell-errors` (admin, dry run by
  default, `?apply=true`). Uses the same rule, restores status / real price / P&L, releases the capital
  and symbol lock the dead branch had re-claimed, and books the P&L to the ledger. Rows from earlier
  days are listed as skipped (Dhan's order book only holds today); fix those from the contract note.

## Not changed
- Items 12 (dashboard totals / win rate), 13 (broker panel) and 15 (reject retry loop) are still open.

## Tests
`tests/test_reconcile.py` (the old "dead SELL ignores a filled target" assertion encoded the bug and was
replaced; 6 dead-SELL cases, 6 repair cases) and `tests/test_eod_squareoff.py::TestDuplicateSellGuards`
(7). Full service suite: 2705 passed, 2 skipped.
