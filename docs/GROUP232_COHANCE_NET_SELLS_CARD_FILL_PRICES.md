# Group 232 - sold-today holdings are netted (COHANCE), closed cards use the real BUY fill

Items 1 and 3 of the open list after the group 231 log review. Rebuild real-trade-service and
position-stocks-service.

## Item 1 - COHANCE phantom OPEN (real-trade-service portfolio/portfolio.py)
Problem: COHANCE was sold at 455.50 but stayed OPEN (qty 2). Dhan keeps a holding sold today in
`get_holdings()` until settlement, while `get_positions()` already shows it as `netQty = -2`. The ghost
check only asked "does the broker list this symbol / how many?", saw the still-listed holding, and
left the row OPEN, so a stop/target could fire a second SELL.
- `holdings_sync_reconcile` now keeps the settled-holdings qty separately and reads the SIGNED `netQty`
  (and `sellAvg`) from live positions.
- Netting applies only when holdings >= our qty_open (the settlement-lag case): effective qty =
  holdings + net (floored at 0). Zero left -> the existing GHOST_CLOSED path (qty 0, CLOSED, cash
  refunded at avg entry); some left -> the existing QTY_SYNCED cap-down.
- Not applied when holdings already dropped below our qty (cap-down handles it; no double count), when
  net is >= 0, when the position is younger than `HOLDINGS_SYNC_GUARD_MINUTES`, or when a SELL is
  PLACED/PARTIAL (pending-orders loop owns that fill). A non-numeric net is ignored.
- The GHOST_CLOSED event and log line now say why (net -2, sell avg 455.50).
- After the close the row is CLOSED today, so the 24h re-import guard keeps the still-listed holding out.
- Limit: no exit fill is booked (no realized P&L), same as any ghost close. The real P&L is on Dhan.

## Item 3 - closed cards on the real BUY fill (position-stocks-service orders/reconcile.py)
Problem: THELEELA (-1 shown, +7.50 on fills), MASTERTR (-47 vs -53.07), RPTECH, RPEL: entry_price stayed
the scan-time signal price because the super-order row carried no usable entry price.
- `_entry_fill_from_orderbook`: BUY on the position's own security id, TRADED, exact quantity, average
  within 5% of the stored entry. One match is used; several are resolved by createTime nearest
  opened_at (within 10 min and 60 s clear of the next), else nothing changes. Logged on every use.
- Used as a fallback in `_apply_entry_correction` (live) and in `repair_closed_entry_prices`
  (also when the super order is missing from today's list).
- Skipped when the row shows the entry rejected/cancelled (no BUY fill exists, no extra Dhan call).
- `_cached_order_list`: one order-book call per 20 s shared across positions; failures not cached.
- `auto_repair_closed_entry_prices` runs the repair (apply=True) from `_fast_reconcile_loop`, throttled by
  `ENTRY_REPAIR_AUTO_INTERVAL_S` (default 300, 0 = off). Idempotent, ledger moves by the P&L difference,
  never raises. POST /reconcile/repair-closed still works for a manual dry run.
- Limit: Dhan's order book is today-only, so earlier days are not repaired.

## Not changed (needs data)
INDORAMA (four orders, one card, 116 qty / -8.18 vs 9.28 on the fills) and the missing ICIL, INOXINDIA,
DOMS cards: send the `trade_orders` rows with timestamps and order IDs, and which service booked them.

## Tests
real-trade `test_group232_holdings_net_sells.py` (10); position-stocks `test_group232_entry_fill_orderbook.py` (23).
Full suites: real-trade 3561 passed, 2 skipped, 4 failed + 1 error; position-stocks 2893 passed, 2 skipped,
2 failed. Every failure also fails on the unmodified group 231 upload (oracle_compat x4, group172
teardown error, test_dhan_client valid_creds, group210 throttle test).
