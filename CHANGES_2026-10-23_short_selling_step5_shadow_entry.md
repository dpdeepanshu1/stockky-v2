# Short selling step 5: the SHADOW short entry, `scan_shorts()` and the mirrored fail-closed gates (Position Stocks only)

**No order can be placed by anything in this step.** `orders/entry.py` is not edited (a test pins that), `SHORT_MODE` still has no `live` value, and the new code never imports a broker module, reserves capital, claims a symbol lock or spends order budget. With `SHORT_MODE=off` (the default) the only thing that runs is "close shadow rows left over from an earlier switch-on". Longs are unchanged: the full position-stocks suite passes (4122 passed, 2 skipped; baseline before this step 4077).

## What it does
With `SHORT_MODE=shadow`, after each long cycle: scan for fallers, keep those the Market Board also ranks bearish, run the quality check, walk the gates, and record every short Stockky WOULD have taken as a `scalp_short_shadow` row. Each row is then followed with live ticks to its stop, target or the short square-off time. This is the evidence for plan gate S2 (4 weeks, 100 would-be shorts, positive after charges, not from one or two days).

## Files
- NEW `orders/short_gates.py`: ten mirrored gates, strictest first, **all fail CLOSED** (a missing tick, VWAP, day range, circuit band, book, board row, structure summary or news answer refuses; a gate that raises refuses). time window 09:45 to 13:30, price guard (stale tick, slippage = price BELOW signal, day fall), near-day-low, gap-up still above open, VWAP above (mirror of VWAP_BELOW), circuit (roles swap: lower = no room, upper = cannot be bought back, wider buffer), depth readable + spread, board must say `bear` with conviction and not be chased, structure (bullish trend / fresh bullish BOS / thin-volume breakdown / stretch far below EMA20, data required), quality (weak fundamentals are fine; positive catalyst, bulk deal, results today or tomorrow, unchecked news all refuse).
- NEW `orders/short_entry.py`: `attempt_short_shadow`, `resolve_open`, `summary`, `run_short_cycle`. Sizing uses the same budget formula as `ledger.reserve_capital(side="SHORT")` without reserving; `sizing.compute_quantity(side="SHORT")`, mirrored levels from `adaptive.compute(side="SHORT")`, rr floor + geometry, depth caps, cost gate, shared guards (entry pause, stop-out guard, calendar cut-offs, held-long check). One shadow per symbol per day; at most `SHORT_MAX_POSITIONS` unresolved. Refusals are counted by code and logged as one `SHORT_SKIP` row per symbol per 5 minutes; a recorded short logs `SHORT_SHADOW`.
- `screening/engine.py`: new `scan_shorts()` (rolling FALL on the same windows and thresholds, same volume and spread floors, mirrored range / VWAP / consistency multipliers). `scan()` is not edited.
- `models.py`: new table `ScalpShortShadow` (`scalp_short_shadow`, created by `create_all`, no column migration).
- `main.py`: one guarded call to `run_short_cycle` after each AUTO cycle (inside the existing cycle lock; swallows its own errors) and read-only `GET /short/shadow` (rows, S2 progress, gate counters).
- `config.py`, `control/catalog.py`: 10 new switches, all live: `SHORT_MAX_POSITIONS` (2), `SHORT_ENTRY_START_IST` (09:45), `SHORT_ENTRY_CUTOFF_IST` (13:30), `SHORT_SQUAREOFF_TIME_IST` (14:55), `SHORT_VWAP_TOLERANCE_PCT` (0.15), `SHORT_MIN_ENTRY_RANGE_POSITION` (0.08), `SHORT_MAX_DAY_FALL_PCT` (5.0), `SHORT_UPPER_CIRCUIT_BUFFER_PCT` (3.0), `SHORT_MIN_BOARD_CONVICTION` (50), `SHORT_GAP_UP_REFUSE_PCT` (1.0).
- `calibration-runner-service/autocalib.py`: `SHORT_MAX_POSITIONS`, `SHORT_UPPER_CIRCUIT_BUFFER_PCT`, `SHORT_ENTRY_CUTOFF_IST`, `SHORT_SQUAREOFF_TIME_IST` added to PROTECTED (no report may loosen them).
- `docs/ENV_REFERENCE.md` regenerated (the generator does not list the `_ms_float` style switches, so the SHORT_* names are in `config.py` and the catalog, not in that file).

## Shadow result rules
Stop is checked first (conservative). A stop is booked at the price actually seen, a target at the target level. At `SHORT_SQUAREOFF_TIME_IST` an open row closes at the live price (EOD). A row left from an earlier day is EXPIRED with no result booked. Charges are estimated with the short's legs swapped (sell = entry, buy = exit). A missing live price leaves the row open.

## Tests (new: 45; all run with real pytest)
`test_short_step5_gates.py` (19, one per gate and per missing-data case, plus "no broker / no capital call in the short code" and "entry.py untouched"), `test_short_step5_scan.py` (8), `test_short_step5_shadow_entry.py` (18, real models on SQLite: record, size, cap, throttle, held-long, no capital, stop/target/EOD/expiry, off-mode leftovers, full cycle). `test_short_foundation.py` now allows `orders/short_entry.py` as the second reader of `SHORT_MODE`.

## Known limits
- Mirror metrics the daily structure summary does not carry (room down to daily support, chase below a supply zone, mirrored board chase) are NOT judged; the gate uses the fields that exist and says so. Add them when the structure engine exports them.
- The shadow price is the last tick, not a fill: no spread or slippage on entry. Treat shadow P&L as optimistic and judge S2 after charges AND with a margin.
- Short-side shortlist weighting (`screening/shortlist.py`), the Market Weather short dial, the frontend (SHORT badge, switches page, shadow table) and the odds recorder are NOT built (plan steps 6 to 8).
- S0 (Dhan written answer on SELL Super Orders + the 1-share round trip) is still open and blocks everything live.

## Next
Frontend + calibrator dial (step 6/7), then run `SHORT_MODE=shadow` for 4 weeks. `live` is added only after S0 and the exit-side review.
