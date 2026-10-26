# E184 - position-stocks: a part-filled entry kept the ordered size, and a cancelled remainder booked a held position as ERROR

Includes E183.

## What I did
Read `run_exit_reconciliation` (all of it) and the helpers it calls on the entry side: `_entry_fill_from_row`, `_apply_entry_correction`,
`_entry_fill_from_orderbook`, `_filled_entry_behind_dead_parent`, plus `ledger.reconcile_position_cost`. Checked every place that writes `ScalpPosition.quantity`
(only `overnight_stop.py`, which is for carried CNC positions with no super order).

## Findings (fixed)
1. **Quantity never followed the fill.** A MARKET entry on a thin stock can fill only part of the order (Dhan status `PART_TRADED`, `filledQty` < `quantity`).
   The position kept the ORDERED quantity, so booked P&L, `capital_risked`, the ledger and the flat SELL at EOD / manual exit all used shares that were never
   bought. A flat SELL of the full size against a part-filled long sells shares we do not own (an unintended short, or a Dhan rejection that leaves the real
   shares unsold). Reconcile corrected the entry PRICE from the fill but never the QUANTITY.
2. **A cancelled remainder booked the held shares as ERROR.** If Dhan cancels the unfilled rest, the parent row reads CANCELLED with `filledQty` > 0. The
   dead-entry path only looked for a full-size BUY+SELL pair in the order book (group 276), found nothing, booked ERROR "no trade", released the capital and the
   symbol lock, and stopped tracking a position Dhan still held. Nothing would ever sell it and the symbol was free to be bought again.

## Change (orders/reconcile.py)
- `_entry_filled_qty(row)`: shares the entry has filled (parent `filledQty` / `filled_qty` / `tradedQuantity`, else the `ENTRY_LEG` detail).
- `_sync_entry_quantity(db, pos, row)`, called once per position per pass BEFORE `_apply_entry_correction` (so the price correction works off the real quantity):
  sets `quantity` to the filled quantity, scales `capital_risked` in proportion, and returns/charges the difference through `ledger.reconcile_position_cost`.
  It follows later fills too but never above the order's own size (`quantity` on the row); when the row shows no ordered size it can only shrink.
  Only OPEN / EXIT_LEGS_REJECTED positions. Idempotent. One "Partial entry fill" info alert per (position, quantity).
- Dead parent with shares filled: after the existing BUY+SELL-pair check finds nothing, a row with `filledQty` > 0 keeps the position OPEN (not ERROR) at the filled
  quantity, keeps the capital reservation for those shares and the symbol lock, and sends ONE critical alert ("PARTIAL ENTRY, REMAINDER CANCELLED ... bracket may
  no longer exist - check Dhan"). EOD squareoff / the exit watchdogs then cover it. A dead parent with nothing filled is still ERROR, and the AAATECH
  full-fill BUY+SELL case is still booked as before.
- No new environment variable, column or migration.

## Tests
- `tests/test_reconcile_partial_entry.py` (new, 15 tests): shrink + capital + ledger, idempotent second pass, later fill grows up to the order size, cap at the order
  size, no-ordered-size never grows, full / zero fill unchanged, fill read from the ENTRY_LEG detail, capital off real quantity and real price, stop hit books P&L
  on the filled shares, closed position never resized, cancelled part fill stays OPEN with one critical alert, nothing-filled still ERROR, AAATECH pair still booked.
- NOT RUN with pytest here (pytest and SQLAlchemy are not in the sandbox). The real `reconcile.py` was run against a stub harness (fake DB, ledger, Dhan,
  notifier) on 14 equivalent scenarios: 14 / 14 passed. Run `bash scripts/docker_test.sh position-stocks-service` on the VM.

## Not changed / open
- Dhan's exact response for a cancelled part-filled super order (are the exit legs cancelled with the parent?) is assumed, not live-verified; the alert says the
  bracket may be gone for that reason.
- `entry.py` still reserves capital and sizes the flat SELL off the ordered quantity until reconcile's next pass (about one tick).
