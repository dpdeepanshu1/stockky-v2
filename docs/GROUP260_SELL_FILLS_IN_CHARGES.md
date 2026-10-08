# Group 260 — sells were missing from the cumulative charges total (real-trade-service)

## Symptom
Real Auto Trade → Charges: "Today's Dhan charges" showed -₹93.06 (13 filled orders, STT ₹8.16, DP ₹81), but
"Charges since start" showed Today -₹1.37 and Total -₹55.95 with STT ₹0 and DP ₹0. The grand total was wrong by
exactly the sell side.

## Root cause
`charges_ledger.report()` prices every order from `trade_fills`. Only BUY fills ever wrote a `TradeFill`
(`portfolio.record_real_fill`). `reconcile._book_fill_delta`'s SELL branch went straight to
`record_real_exit_fill`, which books position/cash/P&L but never adds a `TradeFill`. So `report()` never saw a
single SELL: no STT (delivery STT is sell-side), no DP (₹13.5 per delivery sell), no sell-side exchange/SEBI/GST.

## Fix
1. `execution/reconcile.py::_book_fill_delta` — SELL branch now writes a `TradeFill` (qty = the confirmed
   increment, price = the increment price) before the position lookup, so a sell with no matching position is
   still counted. Same transaction as the status/position update.
2. `charges_ledger.report()` — SELLs already booked before this fix have no fill rows. Any order of the mode with
   `filled_qty_so_far > 0` and no fill rows is priced from `broker_fill_notional`, else
   `filled_qty_so_far × limit_price`, and dated by `updated_at`. Orders priced from neither (market sells on
   pre-fix rows) cannot be recovered and stay uncounted. Orders that do have fill rows are never double counted.
3. Frontend (`PositionStocksTab.tsx`) — the Position Stocks "Net (approx)" line subtracted the live order-book
   snapshot's charges (BUY leg only: the exit leg is not listed TRADED with a price). It now subtracts today's
   ledger charges (buy + sell of every closed trade), falling back to the snapshot until the ledger loads.

## Not changed
- position-stocks-service `scalp_charges_ledger` already books round trips (buy + sell), so its Today/Total were
  right; only the Net line above was understated.
- Legacy market sells are an estimate gap, not a bug: Dhan's contract note remains the source of truth.

## Tests
`tests/test_group260_sell_fills_charges.py` (10 tests). real-trade-service suite: 3754 passed, 1 skipped;
`charges_ledger.py` and `execution/reconcile.py` at 100%. One pre-existing teardown error in
`test_group172_volume_shock_history_reasons.py` (fails identically on the uploaded zip in the sandbox).

Run: `cd services/real-trade-service && python3 -m pytest tests -q --cov=charges_ledger --cov=execution.reconcile --cov-report=term-missing`
