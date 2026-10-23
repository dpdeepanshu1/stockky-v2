# E60 - E31 step 4: RealAutoTrade.tsx "watchlist" tab as its own component

`frontend/src/components/RealAutoTrade.tsx` 2,459 -> 2,313 lines. The Watchlist tab JSX (146 lines) moved to `frontend/src/components/realAuto/tabs/WatchlistTab.tsx` (170 lines), unchanged.

`WatchlistTab` takes 7 props: `mode`, `loggedIn`, `candidates`, `candidatesLoading`, `expandedCandidateId`, `setExpandedCandidateId`, `loadCandidates`. State, the 5-second poll while the tab is open, and the candidate loader stay in `RealAutoTrade`.
`RealAutoTrade.tsx` no longer imports `StructurePanel`, `StructureGateCard`, `structureView` (`coveragePct`, `gateStatus`, `tally`) or `SOURCE_LABELS`; only this tab used them, and the new file imports them. (`realTradeApi.gateStatus` in `RealAutoTrade` is a different function and is untouched.)

## How it was checked (no node_modules, no React types in the sandbox)
* the 146 JSX lines are identical to the original (`diff` empty);
* TypeScript compiler over all of `src`: same total (9,128) before and after, no new error kinds, no unresolved-name or import errors. The one non-noise error in the new file is the existing `key` prop on `StructureGateCard`, which only appears because React's types are missing here;
* the 10 node test files in `npm test` pass.
**Not run here:** `npm ci`, full `tsc` with React types, `vite build`. Run them on the VM.

## Next slices
Still inside `RealAutoTrade`: board ~45, overview ~394, live ~213, pipeline ~281, charges ~317, log. Next: live, then pipeline.
