# Group 280 - Phase B (measure) and Phase C (Dhan Data API): what could be built and tested offline

## Already in place before this group (checked, not rebuilt)
- AAATECH reconcile bug: fixed in group 276 (`tests/test_reconcile_dead_parent_fill.py`; `POST /reconcile/repair-dead-entry-errors`).
- Trade breakdowns: position-stocks `GET /trades/breakdown` (group 167), real-trade `GET /positions/{mode}/breakdown` (group 277).
- Dhan depth on real-trade watchlist entries (group 277); scalper best bid/ask spread gate (`MAX_SPREAD_PCT`).
- Dhan intraday candles exist in market-data (`dhan_data/history.py`, `/history` with a minute interval).

## Done (position-stocks)
1. **Per-trade record**: new nullable column `scalp_positions.signal_price` (auto-migrated), set at both entry sites (scanner price / manual price). Entry slippage = (entry_price - signal_price) / signal_price; reconcile later corrects entry_price to the real fill, so this is fill vs signal. Older rows have none.
2. **`GET /trades/report?days=1..30`** (`orders/trade_report.py`): per exit reason, exit hour (IST), entry hour (IST) and scan window (the entry tier): trades, win rate, gross, charges, net, expectancy (net per trade), average slippage. A trade is a win or loss on NET after estimated round-trip charges (same rate card as the charges ledger). days=1 is the daily report. Rows are only kept for `TRADE_HISTORY_RETENTION_DAYS`.
3. **Depth check before entry** (`orders/depth_gate.py`, C3): market-data `/quote` gives Dhan's 5-level `spread_pct` and `book_value_5`; a spread above `ENTRY_DEPTH_MAX_SPREAD_PCT` (0.5) or a book below `ENTRY_MIN_BOOK_VALUE` (0 = off) is skipped (`DEPTH_SPREAD:` / `DEPTH_THIN_BOOK:` in the candidate log) before any capital is reserved. No answer, timeout or no depth never blocks. `ENTRY_DEPTH_GATE=0` turns it off. Not applied to the manual BUY.
4. **Order-update WebSocket listener** (`execution/order_ws.py`, C1), READ-ONLY: connects to `wss://api-order-update.dhan.co`, logs in (MsgCode 42), keeps the latest state and last transitions of each order; reconnects with back-off and re-reads the stored token each time. `DHAN_ORDER_WS_ENABLED=1` starts it (default 0). `GET /orders/ws-status`, `GET /orders/events/{order_id}`. It places and cancels nothing and no trading code reads it yet.

## Not done, and why
- **Using the order events in reconcile** (the part that removes AAATECH-type errors): message shapes come from Dhan's documentation and were not checked against a live session. Turn the listener on for one session, read `/orders/ws-status` and `/orders/events/{id}` against Dhan's order book, then wire it into reconcile as a cross-check before it decides anything.
- **C2 live feed for exits**, **C4 limit orders at the touch**: both change live order behaviour (Dhan converts API market orders to limit orders with protection) and need a live session to validate.
- **Size down from the real book**: only skip is implemented; sizing would have to change the capital reservation.
- **C5 Dhan intraday candles for VWAP / opening range / ATR**: the data exists in market-data; the scalper and opening gate have not been switched to it.
- **C6 exact charges from Dhan's ledger / trade history**: charges are still the estimated rate card.
- Entry "catalyst" is a real-trade concept; the scalper has no catalyst field, so its tier is the scan window.
- Real-trade has no `signal_price` record yet (it already has net P&L and source in its breakdown).

Tests: position-stocks `tests/test_group280_trade_report.py`, `test_group280_depth_gate.py`, `test_group280_order_ws.py`, `test_entry.py` (signal price, depth wiring), `test_main.py` (routes).
