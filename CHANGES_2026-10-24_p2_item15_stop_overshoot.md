# P2 item 15: stop overshoot - one real bug fixed, and the overshoot is now measured - E105

Position Stocks, longs and shorts. Reconcile (`orders/reconcile.py`). No setting changes; no stop width changes (the plan says none of that comes before this).

## 1. Fixed: a stop or target hit with no fill price was booked at the ENTRY price
`_extract_leg_price` falls back in this order: a real fill on the leg, the leg's static `price`, the PARENT row's `averageTradedPrice`, then the position's own trigger price. The parent row of a Super Order is the entry leg, so its `averageTradedPrice` IS the entry fill. When Dhan's leg had no price and the order book had no single matching exit order, the exit was booked at the entry price: P&L exactly 0, whether the position lost on its stop or won on its target, until the 5-minute repair corrected it. That also hides a real overshoot (the plan's "17 of 24 stop hits lost more than planned" was measured on records like these).
- `_extract_leg_price(..., entry_price=0.0)`: when the parent average equals `entry_price` (within half a paisa) it is skipped, with a warning, and the position's own trigger price (its stop or target) is used. A parent average that differs from the entry fill is still used. All three callers now pass `entry_price`; a caller that does not keeps the old order exactly.
- Effect: such a hit now books at its trigger (a stop at the stop, a target at the target) instead of at the entry. For a stop that really slipped this still understates the loss (the trigger is the best estimate left), but it is no longer zero; the repair job still replaces it with the real fill.

## 2. Measured: how far past its stop a stop hit really filled
- `stop_overshoot_pct(pos, exit_price)` (pure): percent of the stop that fired, positive = worse than the stop (a long exits below it, a short above it).
- `note_stop_hit(pos, exit_price, confirmed)`: called for every STOP_HIT booked by reconcile. Only a CONFIRMED fill (a real price on the leg, or the unique order-book match) is a measurement; a booked trigger price is counted as "estimated". A fill beyond the stop logs one line `STOP_HIT filled x.xxx% BEYOND its stop`.
- Counters (process lifetime, reset on restart) on `GET /review-fixes/status` under `stop_overshoot`: `stop_hits`, `confirmed`, `estimated`, `beyond`, `mean_pct_of_beyond`, `max_pct`. Read-only, nothing is blocked or resized. `scripts/records_report.py` still gives the same figure over stored trades.

## What this says about the next step
The counters show how many stop hits are measured and how big the overshoot is. If most of them are `estimated`, the problem is the missing price (Dhan's leg shape), not the stops; if most are confirmed and `beyond`, the stop-market slippage is real and the sizing allowance (`STOP_OVERSHOOT_ALLOWANCE_PCT`, real-trade) is the lever to consider. Not decided here.

## Proof
- `scripts/test_short_exit_paths.py`: 75 cases (was 61), the new section 11 runs the REAL reconcile against shorts and longs: a bare stop leg books at 101 not 100, a bare long target at 102 not 100, parent average equal to entry skipped, different parent average used, old callers unchanged, real fill still wins, overshoot signs for both sides, counters (confirmed vs estimated, beyond, mean, max), a target hit is not a stop hit, junk never raises. Five deliberate breakages (entry fill used again, sign swapped, estimates counted as measured, measure not called, target hits counted as stops) are each caught.
- `tests/test_reconcile_leg_price.py`: 6 new pytest tests; the existing extractor tests are unchanged and still describe the old order for callers without `entry_price`.
- Not run here: pytest and SQLAlchemy (pypi blocked). On the VM: `python3 -m pytest tests -q` in position-stocks-service (check `test_reconcile.py` and `test_reconcile_leg_price.py` first).

## Not changed
Real-trade has its own exit code and was not touched. The question of whether the stop leg should be a stop-limit instead of stop-market is a Dhan behaviour and order-type decision and is yours; it is not changed here.
