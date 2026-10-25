# E146 - risk rail keeps drawing when the stop has been trailed to / above the entry (2026-10-25)

Frontend only (display). No backend, API, switch or trading behaviour changed.
Roll back: restore `v7View.ts`, `RiskRail.tsx`, `design-v7.css`, `v7.test.mjs` from E145.

## Problem found in the E145 review
`railView` refused any long with stop >= entry (and any short with stop <= entry). The trail / breakeven logic (Real Trade `STRUCTURE_TRAIL_ENABLED` defaults on; Position Stocks `orders/trailing.py` and `orders/breakeven.py`) moves the stop exactly there, so the rail vanished on winning trades.

## Fix
- Stop at/past the entry but still before the target = `locked`: the rail starts at the ENTRY, a red stop tick shows where profit is locked, legend reads Entry / Stop / Target, words say "profit is locked in".
- Price at/through a locked stop = `at_stop` (never "under water"); a locked rail never feeds the near-stop warning (riskUsedPct 0).
- Still refused: stop past the target, target on the wrong side of the entry, missing/zero levels.
- Untrailed rails are unchanged (same numbers, same classes).

## Checked here
`tsc --noEmit` clean; `npm test` all 25 suites pass (v7 logic suite now 11 tests, one new for trailed stops, two refusal pins changed); `vite build` succeeds.

## NOT checked
Not looked at in a browser (none in the sandbox): open Position Stocks -> Positions and Auto Trade -> Positions on a trade whose stop has trailed and check the red stop tick sits right of the entry tick. Backend suites at this point: real-trade 5373, position-stocks 4315, api-gateway 9044, market-data 1733 passed (Python 3.12).
