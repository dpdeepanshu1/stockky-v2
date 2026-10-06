# Group 207 — dashboard trade totals count only settled trades (item 12)

## Problem
Position Stocks fills of the day were about +₹134 at Dhan against about +₹26 on the dashboard, and the
30.6% win rate counted ₹0 rows as losses (by symbol on Dhan's numbers it is about 9 wins to 13 losses).

## Cause
`GET /trades/history` treated every non-OPEN row with a P&L as a trade and every P&L <= 0 as a loss, so
three kinds of row were mixed in as ₹0 losses: exits still on the entry-price placeholder, ERROR rows,
and genuine break-evens. The wrong ₹0 values themselves came from item 10 (group 205) and the ERROR rows
from item 11 (group 206); this group stops the summary from mislabelling whatever is left.

## Fix
- New `orders/trade_stats.py` (pure functions): classifies each row once as win, loss, breakeven,
  pending (marker `*_PENDING_RECONCILE` / `*_UNRESOLVED`), error, or rejected entry.
- `/trades/history` summary: `total_trades`, `wins`, `losses`, `win_rate_pct`, `total_pnl`, best and worst
  now use settled trades only. New fields: `breakeven`, `pending_reconcile`, `error_trades`. Win rate =
  wins / settled trades (breakevens stay in the denominator). The trades list is unchanged.
- `/trades/breakdown` (group 167) also leaves out pending placeholders.
- Frontend (Position Stocks, Trade History card): shows `nW / nL / nBE`, a breakeven segment in the bar, and
  a line "Not in the totals above: N exit(s) waiting for the real fill price, M ERROR row(s)". `tsc` clean;
  not browser-tested.

## Notes
- After deploying groups 205 and 206 and running their repair endpoints, the pending and error counts for
  today should fall to zero and the totals should match Dhan's order lines.
- Unchanged: the broker panel (item 13) and the reject retry loop (item 15).

## Tests
`tests/test_group207_trade_stats.py` (classification, summary, breakdown) and
`tests/test_main.py::TestTradesHistory` (endpoint). Full service suite: 2721 passed, 2 skipped.
