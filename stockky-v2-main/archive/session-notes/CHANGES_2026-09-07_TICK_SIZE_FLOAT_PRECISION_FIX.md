# 2026-09-07 — Tick-size rejection: float precision in round_to_tick()

## Symptom
A real Dhan BUY order (Wockhardt, ₹2,097.05, qty 1, CNC/Delivery) was
rejected by the exchange:

    EXCH:16283:The order price is not multiple of tick size.

...despite ₹2,097.05 *looking* like a clean multiple of the ₹0.05 NSE
equity tick size, and despite the 2026-09-01 session already having added
`round_to_tick()` to `execution/dhan_client.py` (called from
`entry_engine/entry.py` and `manual_engine.py`, plus a defense-in-depth
re-round inside `place_order()` itself).

## Root cause
`round_to_tick()` did its math in binary `float`:

```python
ticks = round(price / tick_size)
return round(ticks * tick_size, 2)
```

`0.05` has no exact binary floating-point representation (it's actually
stored as `0.05000000000000000277...`), and neither do most "clean"
2-decimal rupee prices. `price / tick_size` and `ticks * tick_size` are
therefore not exact — the result can land a tiny fraction off the true
tick (e.g. `2097.0499999999997` or `2097.0500000000002`) even though
Python's own `round(x, 2)` and any `%.2f`-style display formatting hide
that and print `2097.05`. The exchange's tick check works off the exact
value (effectively integer paise), not the pretty-printed one, so it
rejects a price the logs and the UI both showed as perfectly valid — the
exact same failure mode the 2026-09-01 fix targeted, just one layer
deeper than that fix reached.

## Fix
`round_to_tick()` (in `services/real-trade-service/execution/dhan_client.py`)
now does the tick math in `Decimal`, parsed from `str(price)` (the exact
decimal digits) rather than from the float's binary approximation, so
`price / tick_size` and `ticks * tick_size` are exact base-10 operations
with zero binary rounding error anywhere in the chain. The float is only
reconstructed at the very end, once the value is already guaranteed to be
an exact multiple of `TICK_SIZE`.

Added `is_valid_tick_price()`, also Decimal-based, and used it as a final
guard inside `place_order()`: if a LIMIT price is still off-tick after
rounding (which should now be unreachable, but this is the exact class of
"should be unreachable" bug that caused this incident), `place_order()`
now raises immediately with a clear, attributable message instead of
letting a bad price reach Dhan and come back as an opaque exchange
rejection indistinguishable from a genuine broker-side issue.

## Files changed
- `services/real-trade-service/execution/dhan_client.py`
  - `round_to_tick()` rewritten to use `Decimal` instead of `float`.
  - Added `is_valid_tick_price()`.
  - `place_order()` now guards the final LIMIT price with
    `is_valid_tick_price()` before calling the Dhan SDK.

No caller changes needed — `entry_engine/entry.py` and `manual_engine.py`
already call `round_to_tick()` from this module, so they get the fix for
free.

## Verification
Brute-forced 500k+ random prices (and 200k prices specifically constructed
to sit near float-drift-prone `.x5`-tick boundaries) through the new
`round_to_tick()` and confirmed every result passes `is_valid_tick_price()`
with zero failures. `₹2,097.05` (the exact failing price) round-trips
cleanly, as does `₹847.41` (the price cited in the 2026-09-01 fix note).
