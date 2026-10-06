# Group 208 — Live Dhan broker panel shows real positions, LTP and P&L (item 13)

## Problem
- Every LTP showed ₹0.00; "P&L" equalled the cost value (ABSLAMC +₹2,027 on a ₹2,027 position).
- Closed positions (KRBL, SATIN, DHANBANK) were listed as open.
- Capital card: Position Stocks Allocated ₹15,197 against Available ₹19,110, and Real Trade "Allocated ₹0".

## Causes
- Dhan's positions endpoint has no last-traded-price field and keeps closed rows with `netQty` 0. The panel
  read `lastTradedPrice || ltp || 0`, fell back to `unrealizedProfit || dayBuyValue` (the cost) when the profit
  was 0, and never filtered on net quantity. The balance bar did the same and sized rows off `buyQty`
  (still > 0 for a closed position).
- The capital card clamped "Real Trade allocated" to 0 whenever the Dhan balance was below Position Stocks'
  allocation, which hid the reason.

## Fix
- `real-trade-service/portfolio/broker_view.py` (pure): normalises each Dhan position. `is_open` = net qty
  != 0; average price from the right side (buy for long, sell for short); `ltp` None when missing; unrealised
  P&L computed from LTP (works for shorts), else Dhan's own figure if it sent one, else None; cost value;
  realised P&L.
- `GET /dhan/positions` keeps the raw `positions` list and adds `rows` and `open_count`. LTPs come from the
  market feed's priority lane for OPEN symbols only (closed rows are never quoted); a quote failure returns the
  positions without LTP instead of failing.
- Frontend (`RealAutoTrade.tsx`): "Open positions at Dhan" and the balance bar show only open rows; a missing
  LTP or P&L is "—", never ₹0.00 or the cost; works with an older backend too (raw list, no LTP, no P&L).
- `CapitalSplitCard.tsx`: a negative remainder shows "—" plus "Dhan balance is ₹X below Position Stocks'
  allocation" instead of ₹0; if Position Stocks' Available exceeds Allocated, a note says to compare it with
  the Dhan balance.

## Not changed (needs your call)
- The ledger itself. Position Stocks' Available includes booked P&L, so being above Allocated is not always a
  fault, and I found no evidence in the files of which part of the ₹3.9k gap is drift. Groups 205 to 207
  correct the wrongly booked exits that most likely fed it; if the gap stays after those repairs, compare
  `GET /ledger` against the Dhan balance and tell me the numbers.
- Item 15 (rejected-order retry loop) is still open.

## Tests
`real-trade-service/tests/test_group208_broker_view.py` (9) and `test_main_routes_trading.py` (route: raw list
kept, open rows get LTP, closed rows are never quoted, quote failure survives). `tsc` clean; not browser-tested.
Suite: 3331 passed excluding groups 171/172; the 4 `test_oracle_compat` failures and the group 172 error also
occur on the original group 204 code.
