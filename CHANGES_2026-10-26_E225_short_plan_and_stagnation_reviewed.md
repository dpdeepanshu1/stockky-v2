# E225 - review only: short `plan_short` and the stagnation exit (no code change)

Includes E224 (short selling and swing ship live behind their rails).

## Read
- `orders/short_entry.plan_short` end to end, plus `adaptive.compute` / `_levels_to_prices` / `apply_target_cap`, `rr_floor.geometry_reason`, `sizing._short_quantity`, `tighten_short_levels`.
- `eod_squareoff.run_stagnation_exit` and the exit side of `close_position_now`.

## Result: no fault found
- Levels: SHORT puts the target below and the stop above the entry, with a one-tick clamp; an unknown side raises (no default). `geometry_reason` re-checks target < entry < stop before anything is sized.
- Stop tightening (Market Weather stressed / panic) can only move the stop closer, never past price and never wider. Size is worked out from the NORMAL stop, so tightening lowers risk, never grows the position.
- Sizing: a short is rounded DOWN and never forced to one share past the budget; depth caps only lower it; the cost gate and the Time Machine gate can only refuse. `SHORT_LIVE_QTY_CAP` (1) is applied on top in `short_live`.
- Stagnation: the flat-band rule is symmetric (abs move); the no-follow-through rule uses the LOWEST price seen for a short (`min_price_seen`, kept current by `excursion.py`) and `_side.pnl_pct`. The cover goes through `close_position_now`, which sends the exit side from `_side.exit_transaction_type` (BUY for a short). Carried-overnight positions are skipped.

## Not changed / your call
- Stagnation is a DB switch (`stagnation_exit_enabled`); with it off a live short is left to its stop, target and the 14:55 cover.
- Not run here: anything needing SQLAlchemy / pytest (see E224 for the VM commands).

## Next unread
`short_live` position guard (plain-stop watch, `stop_watchdog` on shorts), `trailing.py` short branch.
