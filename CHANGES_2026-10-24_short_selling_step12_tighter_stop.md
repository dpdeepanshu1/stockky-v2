# Short selling step 12: tighter stop for stressed and panic shorts - E101

Position Stocks only. Longs untouched. A short is only ever made SAFER by this: the stop moves closer, the size does not grow.

## What changed
- `config.py` / `control/catalog.py`: `SHORT_STOP_MULT_STRESSED` (ships 0.8) and `SHORT_STOP_MULT_PANIC` (ships 0.6), range 0.3 to 1.0, 1.0 = off, live, a higher value loosens (so a change needs the confirm). Both are on the calibrator's PROTECTED list. They are not compose settings and not overlay dials (no runner dial, so none of the dial-count tests change).
- `orders/short_entry.py`: `stop_mult_for_state(state)` (stressed / panic only; every other state, junk, NaN = 1.0; clamped to 0.3 .. 1.0) and `tighten_short_levels(levels, live, mult)` (stop distance x mult, re-rounded to the tick, never on the wrong side of price, never wider than before, target not moved). `plan_short` applies it right after the levels are built, using the market state stamped by the overlay (`control.kit.current_profile`), before the RR / geometry / opening-stop checks, so a stop that becomes too tight is refused by the same gates, not widened.
- **Size is worked out from the NORMAL stop** (`budget_stop_pct`, returned by `plan_short` and passed to `reserve_capital` in `orders/short_live.py`). A tighter stop therefore lowers the rupee risk; it never buys more shares.
- The shadow row and the live position both store the tightened stop, so the shadow result matches what live would do.
- `SHORT_MARKET_STATE_OFF` refusals now say the state: "Market Weather: no shorts today (state calm, short dial 0)". They already land in the candidate log as `SHORT_SKIP` (one row per symbol per 5 minutes) and in the refusal counters on the Short selling panel; I did not add a new screen for them.

## Tests
`scripts/test_short_live.py` now has 47 cases (17 new: the state to multiplier table, clamps, NaN / junk, panic 1% -> 0.6%, stressed 1% -> 0.8%, mult 1 untouched, a stop that would round onto price is left alone, no live price). `tests/test_short_live.py` adds a static check that sizing uses the pre-tightening stop and that both switches are in the catalog and protected. `check_control_catalog` OK (it needed the literal `config.NAME` read), `check_shared_copies` OK.
Not run here: pytest / SQLAlchemy (so `test_short_step4` / `step5` still need a VM run), and the real `plan_short` end to end (only the pieces and the source order are tested).

## Still open
Item 10 (exit tests against the real exit code, needs real pytest on the VM). Then P2 items 13 to 18.
