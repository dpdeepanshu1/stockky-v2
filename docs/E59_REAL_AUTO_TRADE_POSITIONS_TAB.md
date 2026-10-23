# E59 - E31 step 3: RealAutoTrade.tsx "positions" tab as its own component

`frontend/src/components/RealAutoTrade.tsx` 2,556 -> 2,459 lines. The Positions tab JSX (101 lines) moved to `frontend/src/components/realAuto/tabs/PositionsTab.tsx` (129 lines), unchanged.

`PositionsTab` takes 13 props: `mode`, `loggedIn`, `pipeline`, `positions`, `autoPilotPositions`, `dematPositions`, `armed`, `actionBusy`, `actionMsg`, `setActionMsg`, `loadPositionsAndOrders`, `doReconcile`, `doClosePosition`. State, effects, polling (including the Pipeline poll that stays active while this tab is open) and the close / reconcile handlers stay in `RealAutoTrade`.
`RealAutoTrade.tsx` no longer imports `ApprovalsPanel` or `PositionCard` (only this tab used them); both are imported by the new file.

## How it was checked (no node_modules, no React types in the sandbox)
* the 101 JSX lines are identical to the original (`diff` empty);
* TypeScript compiler over all of `src`: compared with the E58 run, the only difference is errors that left `RealAutoTrade.tsx` together with the moved code. The two `key`-prop errors on `PositionCard` now show in `PositionsTab.tsx`; they were there before and come from the missing React types, not from this change. No new error kinds;
* the 10 node test files in `npm test` pass.
**Not run here:** `npm ci`, full `tsc` with React types, `vite build`. Run them on the VM.

## Next slices
Still inside `RealAutoTrade`: board ~45, overview ~394, live ~213, watchlist ~152, pipeline ~281, charges ~317, log. Next: watchlist, then live.
