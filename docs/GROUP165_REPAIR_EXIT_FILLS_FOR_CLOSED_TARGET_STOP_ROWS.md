# Group 165 - repair route also corrects exit prices of TARGET_HIT / STOP_HIT rows (position-stocks-service)

Follow-up to group 164, which left exit prices alone. A TARGET/STOP row whose leg dict carried no traded-price key
was booked at the leg's static trigger price, not the real fill (group 162 stops doing that for new trades; today's
earlier rows still have it).

## Change
`POST /positionstocks/reconcile/repair-closed` (dry run by default, `?apply=true` writes) now, for rows with status
TARGET_HIT or STOP_HIT, reads today's order book once and replaces the booked exit with the real SELL fill when
`_exit_fill_from_orderbook` finds EXACTLY ONE match: own security id, TRADED, exact quantity, price within 10% of the
booked exit. Anything else leaves the exit as it was. P&L, capital and the ledger move by the combined entry+exit
difference. Each change now lists `old_entry/real_entry`, `old_exit/real_exit`, `old_pnl/new_pnl`, `delta`.
- Flat-SELL statuses (STAGNATION_EXIT / EOD_SQUAREOFF / MANUAL_EXIT) already book the real fill and never read the order book here.
- If the order-book fetch fails, entry repair still runs and the response carries `exit_error`.
- `_exit_fill_from_orderbook` gained an optional `orders=` argument so one fetch serves all rows (live behaviour unchanged).

## Still not covered
The daily-loss kill switch is not re-evaluated after a repair (group 166 reports the limit status). Only today's rows can be repaired.

## Note on the test run
`tests/test_trade_gates.py` has 5 tests that depend on the wall clock (they build "closed N minutes ago" rows and
compare IST dates), so they fail whenever the machine's IST date has just rolled over (it was past 00:00 IST in my
sandbox). They fail identically on the uploaded group 162 zip, so this change did not cause them; they pass in
IST daytime.

Tests: +9 in tests/test_repair_closed_entry_prices.py (reconcile.py and ledger.py stay at 100%). Rebuild position-stocks-service.
