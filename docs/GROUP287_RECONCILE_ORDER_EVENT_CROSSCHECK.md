# Group 287 - reconcile cross-checks a dead parent against the order-update WebSocket

## Why
Group 276 (AAATECH): a super-order PARENT row read REJECTED while the BUY really traded. Group 276 looks in the order book
for a TRADED BUY. When the order book proves nothing, the position was booked ERROR / Rs0. The order-update WebSocket
(group 280, `execution/order_ws.py`) is a second, independent record of what traded; reconcile did not read it.

## What changed (position-stocks-service)
- `OrderEventStore.find_entry_fill(symbol, qty, since_ts)`: latest BUY event for the symbol that is TRADED for exactly `qty`
  shares and was received at or after `since_ts`. `-EQ` suffix ignored. Memory only.
- `reconcile._entry_fill_from_order_events(pos)`: wraps it (window = opened_at - 120 s); never raises, returns the average fill.
- Dead-entry branch of `run_exit_reconciliation`: after the group 276 order-book check finds nothing, the WebSocket is asked.
  - Always logs one `WS_CROSSCHECK` warning per position when the events disagree with the REJECTED parent.
  - `RECONCILE_USE_ORDER_EVENTS=1`: the row stays OPEN (not ERROR), so EOD squareoff still covers it, same as "BUY filled, no
    exit proven". Default `0`: outcome unchanged, only the log line.
- Needs `DHAN_ORDER_WS_ENABLED=1`; with the listener off the store is empty and nothing changes.

## Not verified live
The event field names (`Symbol`, `TxnType`, `Status`, `TradedQty`, `AvgTradedPrice`) follow Dhan's docs and the group 280
parser; no live frame was seen. Hence the flag defaults to 0.

## Before turning it on
1. `DHAN_ORDER_WS_ENABLED=1` for a session; `GET /orders/ws-status` should show `connected` and `order_events` climbing.
2. Compare `GET /orders/events/<order_id>` for a few real orders with Dhan's order book (symbol, B/S, qty, avg price).
3. Watch for `WS_CROSSCHECK` lines. If each one matches a trade you can confirm in the Dhan app, set `RECONCILE_USE_ORDER_EVENTS=1`.

## Tests
`tests/test_group287_order_events_crosscheck.py` (16): store lookup (match, -EQ, wrong side/status/qty/symbol, age, latest wins),
helper edge cases, reconcile with flag off (ERROR + log), on (OPEN), no matching event, wrong qty, order-book proof still wins.

## Group 288 addendum: the live check is now one call
`GET /orders/ws-compare` (read-only) fetches today's Dhan order book and compares it, by order id, with the pushed events:
status, side, filled quantity, average price (1 paisa tolerance) and symbol. Response: `compared`, `matched`, `mismatched`
(with the differing fields), `missing_in_ws` (placed before the listener connected; not a mismatch), `safe_to_enable`, `verdict`.
`safe_to_enable` is true only with at least 3 compared orders and zero mismatches. `available:false` + `reason` when the listener is off
or the order book cannot be fetched. Changes nothing.

`DHAN_ORDER_WS_ENABLED=1` is now the value in `.env.example` and `.env.oracle.recommended` (the listener is read-only; the code default stays 0, so
copy the line into your real `.env`). `RECONCILE_USE_ORDER_EVENTS` stays 0: set it to 1 only after `/orders/ws-compare` says `safe_to_enable: true`.
Tests: `tests/test_group288_ws_compare.py` (14).
