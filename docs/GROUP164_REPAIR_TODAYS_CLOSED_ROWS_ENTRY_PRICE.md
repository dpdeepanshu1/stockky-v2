# Group 164 - repair today's closed scalp rows that carry a stale entry price (position-stocks-service)

Group 162 corrects the entry price from the real fill for trades from then on, but rows already closed today
(e.g. UNITEDPOLY: stored 44.58, real fill 48.14) kept their phantom P&L and the ledger total built from it.

## What was added
- `POST /positionstocks/reconcile/repair-closed` (admin token from `/positionstocks/auth/login`).
  Dry run by default; `?apply=true` writes. The response lists `changes` (old/real entry, old/new P&L, delta),
  `skipped` (with reason), `unchanged`, `total_pnl_delta`.
- `orders/reconcile.py::repair_closed_entry_prices`: for positions closed today (IST) with a super order id, takes
  the real ENTRY fill from Dhan's super-order list (same rule as live reconciliation), then recomputes
  `entry_price`, `capital_risked`, `realized_pnl`, `realized_pnl_pct` from the stored exit price.
- `capital/ledger.py::adjust_closed_pnl_today`: moves available capital, today's and lifetime realized P&L by the
  total P&L difference (proceeds were already returned, so only the cost/P&L split changes). It does not
  re-run the daily-loss kill switch.

## Safety
Idempotent (a corrected row matches its fill and is skipped). Skipped with a reason: rows with overnight partials,
no matching super-order row, no real entry fill, or a fill more than 25% from the stored entry. Only today's rows
(Dhan's order book holds today only). OPEN / EXIT_LEGS_REJECTED / ERROR rows are never touched.

## Not covered
- The EXIT price is repaired from group 165 on (TARGET/STOP rows, unique order-book match only).
- The kill switch is not re-evaluated: if the corrected total is now below the daily-loss limit, new entries are
  not blocked until the next real loss books.

## How to use (after the market closes)
1. `curl -X POST -H "Authorization: Bearer $TOKEN" https://stockky.duckdns.org/positionstocks/reconcile/repair-closed`
2. Check `changes` against the broker's trade book.
3. Same call with `?apply=true`.

Tests: +9 in tests/test_repair_closed_entry_prices.py, +1 in test_main.py (2537 passed); reconcile.py and
ledger.py stay at 100%. Rebuild position-stocks-service.
