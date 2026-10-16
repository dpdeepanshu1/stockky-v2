# Group 412 - delivery stop rejected "price is not multiple of the tick size" (16 Oct 2026)

## What was seen
Dhan order book: 86 failed orders, all `Railtel Corporation of India`, SELL / Delivery (CNC) / order type Trigger (a stop), price 243.59, qty 9.
Rejection: `EXCH:16283: The order price is not multiple of the tick size.`

## Cause
`tick_size_for_price()` (both `real-trade-service` and `position-stocks-service` `execution/dhan_client.py`) assumes a Rs 0.01 tick for any price
below Rs 250. The exchange refused 243.59, so for that stock the tick is not 0.01. The stop was therefore rounded on a tick finer than the
real one, and because a non-eDIS failure was retried on every sync pass it was re-sent until the order book showed 86 failures.

## Fix
* `dhan_client.round_stop_to_tick()` / `stop_tick_for_price()` (both services): stop and trigger prices use the band tick but never finer
  than Rs 0.05. A multiple of 0.05 is also a multiple of 0.01, so it is valid whichever tick the stock really has. Bands from Rs 1,000 up are unchanged.
* Used for: real-trade `place_order` trigger and stop-limit price, `broker_stop.trigger_for`, `broker_stop.limit_price_below`;
  position-stocks `place_cnc_stop_loss_market` (overnight CNC stop) and its log line.
* `broker_stop._place`: an `EXCH:16283` / tick-size rejection now backs the symbol off for `BROKER_STOP_EDIS_BACKOFF_S` (default 900 s) instead of being re-sent every pass.
* NOT changed: entry / target / super-order leg rounding (`round_to_tick`, 0.01 band below Rs 250). If the same error shows up there, see below.

## Check on the VM after deploy (deploy after market close)
    docker compose logs --since 24h real-trade-service position-stocks-service 2>&1 | grep -E "16283|tick size" | cut -c1-200 | tail
Any remaining hit that is NOT a stop (entry LIMIT, target, super-order leg) means the 0.01 band is wrong there too; then change the
first `_TICK_SIZE_BANDS` row to 0.05 in both dhan_client files and update the tests that pin 0.01 prices (test_breakeven, test_trailing,
test_reconcile, test_adaptive, test_dhan_client).

## Tests
* real-trade `tests/test_group322_broker_stop.py`: two expectations moved to the 0.05 tick (97.51 -> 97.50, 97.53 -> 97.55); new tests for the Railtel price, stop-limit
  price, tick-error detector and the back-off.
* position-stocks `tests/test_dhan_client.py`: new `TestStopTickFloor`.
