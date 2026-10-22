# E45 - trailing stop audit + trail knobs on the page + requirements drift guard (2026-10-23)

## Question: do both services use a trailing stop?
**Yes, both. Nothing new was built for the trail itself** (a second trail on top of the first would only fight it).

| Service | Where | What it does |
| --- | --- | --- |
| position-stocks-service | `orders/trailing.py`, called from the fast-reconcile loop in `main.py` | For every OPEN position with a Super Order: once the peak is `TRAIL_ACTIVATE_PCT` (1.0 %) above entry, the STOP_LOSS_LEG is moved to `peak x (1 - distance)`, `distance = max(TRAIL_MIN_DISTANCE_PCT 0.4, own stop % x TRAIL_DISTANCE_STOP_FRACTION 0.6)`. Only ever up, at least `TRAIL_MIN_STEP_PCT` per move, never below entry + breakeven buffer, always under the live price, never at/above the target; Dhan read-back verifies the move; a rejected modify backs off 60 s. ON by default (`TRAILING_STOP_ENABLED`). Needs `USE_SUPER_ORDER` (on). |
| real-trade-service | `exit_engine/exit.py` step 5 (age-aware ATR trail), plus breakeven (step 4) and the structure trail (`STRUCTURE_TRAIL_ENABLED`, default on) | Price above entry: stop = `LTP x (1 - ATR% x multiplier)`, multiplier tightens with days held and x0.70 / x0.85 near the day high; ratchet up only; skipped on a corporate-action ATR; after a partial exit the remainder rides this trail. `execution/broker_stop.py` then re-places the resting Dhan stop higher (never lower) when the software stop moved. |

Checked and fine: no path lowers a stop; the position-stocks trail cannot end below entry once active; the real-trade trail only runs above entry.

## What changed
1. **Trail knobs on the Real Money Calibration page** (position-stocks, group "Exit and stop protection"): `TRAIL_ACTIVATE_PCT`, `TRAIL_DISTANCE_STOP_FRACTION`, `TRAIL_MIN_DISTANCE_PCT`, `TRAIL_MIN_STEP_PCT`. Only the on / off switch was there before. Shipped values equal the config defaults (tested), all read at call time (`check_control_catalog.py` passes), so a change applies on the next pass. **No default changed.** Real-trade has no page entry for the ATR trail multipliers on purpose: they come from the learned decay profile, not a single number.
2. `tests/test_e45_trailing_wired.py` (4 tests): the loop still calls the trail, it is on by default, the knobs are on the page with the right shipped values, and an active trail cannot go below entry.
3. **F33 part 2, requirements drift guard**: `scripts/check_requirements_drift.py` (+ `scripts/test_check_requirements_drift.py`, 5 tests, wired into CI). Compares each sub-app requirements file with its parent service file. 10 differences exist today and are accepted in the script's baseline on purpose (no pin changed): yfinance 1.5.2 (event) vs 0.2.40 (parent), httpx x3, fastapi / uvicorn / httpx / pydantic in `decision`, `shap` / `imbalanced-learn` only in the ML extra file. A NEW difference, or a fixed one still listed, fails CI. Run `--list` to see them.

## Not done on purpose
- One installable package for the market-structure engine and the gateway router split: both touch every Dockerfile / the 14.7 k-line `main.py` of a live money system and need a deploy-and-watch cycle of their own; not mixed into this zip.
- Deleting the sub-app requirements files: compose builds the parent folders, but the sub-app Dockerfiles still reference their own file.
- Your decisions are still open and untouched: risk per trade (real-trade 5.0), Real Trade general entry cut-off (the 14:00 `ENTRY_LAST_TIME_IST` already exists), position-stocks entry cap (15 placeholder), and whether Kronos / TimesFM / structure / ORB / regime gate go from log or off to enforce.
