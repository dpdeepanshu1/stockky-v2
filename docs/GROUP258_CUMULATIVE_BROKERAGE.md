# Group 258 - brokerage "till date" on both Charges tabs

Ask: a column showing total brokerage since the start, in the Charges tab of real-trade-service and position-stocks-service, plus how to reduce it.

## Why it was not a simple SUM
- position-stocks-service deletes closed `scalp_positions` after `TRADE_HISTORY_RETENTION_DAYS` (3), so a SUM over them only ever covers 3 days. New table `scalp_charges_ledger` (models.py): one row per settled closed position, PK = position id (booking is idempotent), never purged. Booked by `orders/charges_ledger.py` from `GET /charges/cumulative` and, before deletion, from `run_retention_cleanup`. Counts from first deploy; older trades are already gone.
- real-trade-service never purges `trade_orders` / `trade_fills`, so no table: `charges_ledger.py` computes it from every filled order. New `GET /charges/{mode}/cumulative` (admin in REAL).

## Rate card
Same as the tabs: Rs 20 or 0.03% per executed leg, whichever is lower; CNC delivery Rs 0. Env: `CHARGES_BROKERAGE_PCT` (0.03), `CHARGES_BROKERAGE_CAP_RS` (20), real-trade only `CHARGES_DELIVERY_BROKERAGE_RS` (0). Measurement only, feeds no gate (`BROKERAGE_PER_ORDER` and the cost gates are untouched). If your Dhan contract note shows no intraday brokerage, set both to 0.
real-trade SELL orders have no stored product, so a SELL takes the product of the latest earlier BUY of the same symbol (CNC if unknown).

## Frontend
Position Stocks > Charges and Real Auto Trade > Charges each get a "Brokerage since start" card: total till date, with GST, trade/order counts, per-day list (14 days); real-trade also splits by product and lists top symbols and orders at the cap.

## Tests
`position-stocks-service/tests/test_group258_charges_ledger.py` (8), `real-trade-service/tests/test_group258_charges_ledger.py` (7), real pytest, all pass. `tsc --noEmit` clean. Not run against a live DB. The new table is created by `create_all` on boot.
