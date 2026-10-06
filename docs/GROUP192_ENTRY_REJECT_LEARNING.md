# Group 192 - rejected entry orders are learned from, not retried (position-stocks-service)

Cumulative on group 191. Priority 1 of the 2026-10-06 open-issue list (HEGAM retry loop). Rebuild position-stocks-service
and the frontend.

## What happened
HEGAM Advanced Materials was bought 13 times between 09:34 and 09:38 IST. Every order was rejected by Dhan RMS with
"Order rejected as this stock is not allowed to be traded in Intraday". No shares were bought and no capital was at
risk, but it was 13 live orders on the broker account, 13 rows counted as "Closed Today (13)", and they spent
order budget.

## Cause
Dhan's Super Order API ACCEPTS the order and RMS rejects it a moment later, so `orders/entry.py` had already returned
"success" and its synchronous restricted-symbol handling (which only runs when `place_super_order` raises) never ran.
The rejection surfaced later in `orders/reconcile.py` as a dead ENTRY_LEG. That path marked the row ERROR with the
generic text "Entry leg REJECTED on Dhan (reconciled)", never read Dhan's reason, never recorded the restriction, and
freed the symbol again. The re-entry guard ignores ERROR rows (no exit price). Next cycle: same symbol, same order.

## Changes
1. `orders/reconcile.py`: on a dead entry it reads the rejection text (`omsErrorDescription`, `omsErrorCode`,
   `errorMessage`, `rejectionReason`, `remarks`, `reason` on the super-order row; if none, the plain order book entry
   with the same orderId) and stores it: `Entry leg REJECTED on Dhan (reconciled): <reason>`. A REJECTED entry whose
   reason is "not allowed to be traded in Intraday" or a circuit-limit rejection is recorded with
   `intraday_eligibility.record_restriction` (also mirrored into real-trade-service's table by that function). A
   CANCELLED entry or any other reason is not recorded. Lookup and recording are best effort and never stop the
   position being closed out.
2. `orders/entry.py::_rejected_entry_reject`: a symbol with a dead entry is skipped for
   `ENTRY_REJECT_COOLDOWN_MINUTES` (default 30) and, after `ENTRY_REJECT_MAX_PER_SYMBOL_DAY` (default 2) dead entries
   today, for the rest of the IST day. 0 disables either part. Fails open on a DB error. Logged to the candidate log
   as `ENTRY_REJECT_COOLDOWN:` / `ENTRY_REJECTED_TODAY:`.
3. `GET /trades/history` summary has a new `rejected_entries` count. Rejected entries were already outside
   `total_trades`, win rate and P&L (no realized P&L) and outside the loss brake (`trade_gates` ignores ERROR rows).
4. Frontend `PositionStocksTab.tsx`: rejected entries leave "Closed Today (N)" and appear in a collapsed
   "Rejected Entries Today (N) - no shares bought" section; the row shows Dhan's reason.

## Not changed
- The synchronous BUY-rejection branch in `entry.py` (still correct for rejections Dhan raises at placement).
- ERROR rows from dead SELL orders ("..._SELL_DEAD") stay in Closed Today: those can mean real shares still held.
- The first rejected order still gets placed (the restriction is unknowable before Dhan answers); the loop is what ends.

## Tests
`tests/test_group192_entry_reject_learning.py` (22 cases). position-stocks-service suite in the sandbox: 2675 passed
(2653 on the uploaded zip); `reconcile.py`, `entry.py`, `config.py` at 100%; the other non-100% files are identical to
the uploaded zip. Frontend `npx tsc --noEmit` clean.

## Config
`ENTRY_REJECT_COOLDOWN_MINUTES=30`, `ENTRY_REJECT_MAX_PER_SYMBOL_DAY=2`.
