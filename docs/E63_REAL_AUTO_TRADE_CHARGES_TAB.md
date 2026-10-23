# E63 - E31 step 7: RealAutoTrade.tsx "charges" tab as its own component

`frontend/src/components/RealAutoTrade.tsx` 1,769 -> 1460 lines. The Charges tab (an inline function inside the JSX, 311 lines of body) moved to `frontend/src/components/realAuto/tabs/ChargesTab.tsx` (332 lines), body unchanged: its local `OrderCharge` interface, the charge maths and the JSX are the same text.

`ChargesTab` takes 9 props: `mode`, `loggedIn`, `status`, `orders`, `liveDhanOrders`, `liveLoading`, `loadLiveDhanData`, `cumBrokerage`, `cumBrokerageError`. State and the loaders that fetch the Dhan orders and cumulative brokerage when the tab opens stay in `RealAutoTrade`.
`RealAutoTrade.tsx` no longer imports `chargesRates` (`chargesForLegs`, `sumCharges`, `DP_CHARGE_INCL_GST`, `ChargeBreakdown`, `ChargeLeg`), `ChargesPeriod` or `fmtChargeDay`; the new file imports them.

## How it was checked (no node_modules, no React types in the sandbox)
* the 311 body lines are identical to the original (`diff` empty);
* TypeScript compiler over all of `src`: no new error kinds, no unresolved-name or import errors, nothing but the known React-types noise in the new file; total 9,127 -> 9,124 (errors that left with the code);
* the 10 node test files in `npm test` pass.
**Not run here:** `npm ci`, full `tsc` with React types, `vite build`. Run them on the VM. Because this tab does money arithmetic, also open it once on the VM and compare the totals with the page before the change (same date range).

## Next slices
Still inside `RealAutoTrade`: board ~45, overview ~394, log (~to the end of the JSX). Next: log, then board, then overview (the largest and the one with the most state).
