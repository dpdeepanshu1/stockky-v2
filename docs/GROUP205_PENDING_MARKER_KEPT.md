# Group 205 — flat-SELL exits no longer freeze on the entry-price placeholder (item 10)

## Problem
STAGNATION_EXIT / EOD_SQUAREOFF / MANUAL_EXIT rows showed sell price = buy price and ₹0
(CELLO −₹13.75, MARINE −₹22.50, SATIN +₹71 on Dhan, all ₹0 in the dashboard).

## Root cause
`position-stocks-service/orders/reconcile.py::run_exit_reconciliation`. A flat SELL is booked with
`exit_price = entry_price` and a `*_PENDING_RECONCILE` marker; the real fill is read later by
`_reconcile_eod_pending` (order list) or `resolve_stuck_pending` (trade history, prior days).
When `_reconcile_eod_pending` could not resolve the row on a tick (SELL not in the order book yet,
still TRANSIT, filled without a price field, or the order-list call failed under the AngelOne/Dhan
rate limit), the row fell into the super-order fallback, which **cleared the marker** because the
entry leg was traded. With no marker, no later pass ever looked at the row again, so the placeholder
and ₹0 became permanent.

## Fix
- The fallback keeps the marker (logs once per position). Same-day rows are retried every tick,
  prior-day rows are recovered from trade history, and an unrecoverable row ages out to
  `*_UNRESOLVED` with a Telegram alert instead of a silent ₹0.
- New `rearm_cleared_placeholder_exits(db, apply=False, days=3)` and
  `POST /reconcile/rearm-placeholder-exits?apply=true&days=3` (admin): finds rows already damaged
  by the old behaviour (flat-SELL status, exit == entry, P&L 0, no marker, closed in the last N days,
  no overnight partials) and writes the marker back so the normal passes book the real fill and the
  ledger P&L. Dry run by default; lists every row it would touch.

## Using it on the VM
1. Deploy, then `POST /reconcile/rearm-placeholder-exits` (dry run) and check the list.
2. `POST /reconcile/rearm-placeholder-exits?apply=true`. Today's rows resolve on the next reconcile
   tick; earlier rows on the next stuck-pending sweep (or `POST /reconcile/pending/resolve`).
3. `GET /reconcile/pending` shows what is still waiting.

## Not changed
- Rows whose SELL order id was never stored and cannot be matched uniquely by quantity still age out
  to `*_UNRESOLVED` after `PENDING_RECONCILE_MAX_AGE_DAYS` (3) with an alert. Check those against Dhan's contract note.
- Items 11 (filled trades marked ERROR / duplicate SELL), 12 (dashboard totals), 13 (broker panel) are still open.

## Tests
`position-stocks-service/tests/test_reconcile.py`: changed the old "clears the sentinel" test, added
not-visible-yet, TRANSIT, order-list failure and four re-arm tests. Full service suite: 2687 passed, 2 skipped.
