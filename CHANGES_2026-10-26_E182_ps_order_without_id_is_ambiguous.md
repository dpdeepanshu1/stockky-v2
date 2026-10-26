# E182 - position-stocks: an order answered "success" with no order id was recorded as a position with no order id

Includes E181.

## What I did
Read `position-stocks-service/execution/dhan_client.py` `place_super_order` (MARKET path via `dhan_http.post`, LIMIT path via the SDK) and `place_order`,
`_extract_data`, `is_ambiguous_placement_error`, `find_recent_buy_order`, and every caller: `orders/entry.py` (auto BUY, manual BUY), `orders/short_live.py`,
`orders/plain_short.py`, `orders/sell_probe.py`, `orders/eod_squareoff.py`.

## Finding (fixed)
`place_super_order` / `place_order` ended with `return _extract_data(resp) or {}`. A reply of status "success" whose data held no order id (empty data,
null, a status-only body, a blank id) came back as `{}` and was treated as a placed order:
- Long entry (auto and manual): `dhan_super_order_id = str(result.get("orderId") or ... or "")` = "" and the code went on to count the order, write an
  OPEN position with no order id and notify "BUY placed". The BUY may be live at Dhan with a stop, or may not exist at all (a phantom position holding
  capital). No recovery lookup ran, because that only runs on an exception, and reconcile has nothing to follow without an id.
- The short entry handled "no id" as a plain failure (`ambiguous=False`), which also permits the plain-order fallback, i.e. a possible second SELL while the
  first Super Order may exist.
- The exit SELL (`_fire_flat_sell`) got `{}` back as a success.

## Change
- `DhanNoOrderIdError(RuntimeError)` and `_require_order_id()` in `dhan_client.py`. `place_super_order` (MARKET and LIMIT paths) and `place_order` now raise
  it when the success reply carries no order id (`orderId`, `order_id` or `id`, non-blank). The error type is in `_AMBIGUOUS_TYPE_NAMES`, so
  `is_ambiguous_placement_error()` is True for it.
- Nothing else changed in the callers: each already treats an ambiguous placement error as "look at Dhan's order book first". Now a no-id success gets
  that same treatment: a matching young order is adopted; an unreadable book keeps the symbol claimed and rested and alerts (E177/E178/E180); a book with
  no match is a normal failure; the short entry no longer falls back to a plain SELL (ambiguous); the exit SELL is remembered as ambiguous and checked
  before any retry or later call (E181).
- A clean Dhan failure (`status: failure`) is unchanged: it still raises `RuntimeError("Dhan API error ...")` and is not ambiguous.
- No new environment variable.

## Read and judged fine
The MARKET payload (no price key, side-aware clamps), the `TypeError` retry without `tag` (a TypeError is raised before anything is sent), the arm check,
and the LIMIT-leg orientation guards.

## Tests
- tests/test_dhan_client.py: `test_no_order_id_in_result_raises_ambiguous_and_skips_verification` (replaces the old test that expected a silent `{}`),
  `TestPlacementWithoutOrderId` (6 empty shapes on the MARKET path, LIMIT path, SELL, alternate id keys, clean failure stays not ambiguous),
  `TestNoOrderIdIsRecovered` (BUY recovery adopts the order found at Dhan; unreadable book flags unknown). Two payload tests now return an order id.
- Not run with pytest here (no pytest/SQLAlchemy in the sandbox). The real `dhan_client.py` was run against 15 scenarios in a throwaway stub harness
  (all passed); the two `TestNoOrderIdIsRecovered` tests import `orders.entry` and were not run at all.

## VM commands
    bash scripts/docker_test.sh position-stocks-service
    python3 scripts/gen_env_reference.py --check
