# Group 276 - reconcile dead-parent fix, watchlist chase limit, depth fields, price-source contract

## Done
1. **position-stocks `orders/reconcile.py`** - `_filled_entry_behind_dead_parent`: when the super-order parent row reads REJECTED/CANCELLED, today's plain order book is searched for a TRADED BUY (same security id, exact quantity, price within 5% of the stored entry). Found with a filled exit (TARGET/STOP leg, or the one other filled SELL) -> booked as TARGET_HIT/STOP_HIT with the real prices and P&L. Found without an exit -> the row stays OPEN (not ERROR) so EOD squareoff still covers it. Nothing proven -> old ERROR path. `DEAD_PARENT_FILL_CHECK=0` restores the old behaviour. `POST /reconcile/repair-dead-entry-errors` (dry run; `?apply=true`) fixes today's existing ERROR rows. Tests: `tests/test_reconcile_dead_parent_fill.py`.
2. **real-trade `entry_engine/entry.py`** - `WATCHLIST_MAX_CHASE_PCT` (default 0.02). The band check (5-7%) still marks a row "missed"; the new limit only holds a row that is more than 2% above its catalyst and keeps it active. Two older band tests set the limit to 0. Tests: `tests/test_group276_chase_cap.py`.
3. **market-data `dhan_data/quotes.py`** - `depth_fields()`: best_bid, best_ask, spread_pct, bid_qty_5, ask_qty_5, book_value_5 from the quote item's `depth` block; absent/crossed/garbled depth adds nothing. Tests: `tests/test_group276_dhan_depth.py`.
4. **`scripts/check_price_source_imports.py`** + `scripts/price_source_allowlist.txt` - fails on a NEW direct yfinance import in any service (complements api-gateway's own guard test).

## Not done (see the reply for the full list)
Websocket order updates, enabling/using the live feed for exits, wiring spread/depth into entry rules, migrating gateway/analysis/training off yfinance, per-trade expectancy report, delivery-holding stop review.
