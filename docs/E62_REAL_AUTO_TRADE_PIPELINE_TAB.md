# E62 - E31 step 6: RealAutoTrade.tsx "pipeline" (Run Cycle) tab as its own component

`frontend/src/components/RealAutoTrade.tsx` 2,038 -> 1769 lines. The Pipeline tab JSX (275 lines) moved to `frontend/src/components/realAuto/tabs/PipelineTab.tsx` (311 lines), unchanged.

`PipelineTab` takes 19 props: `mode`, `loggedIn`, `status`, `armed`, `gate1`, `pipeline`, `positions`, `orders`, `cycleBusy`, `cycleResult`, `afterhoursRunBusy`, `autoPilotBusy`, `featureBusy`, `doRunCycle`, `doRunAfterhoursScan`, `doToggleAutoPilot`, `doToggleFeature`, `loadStatus`, `loadPositionsAndOrders`. It also exports the `PipelineFeature` type (the feature-name union used by `doToggleFeature`). State, the 2-second pipeline poll and the run / toggle handlers stay in `RealAutoTrade`.
`RealAutoTrade.tsx` no longer imports `CatalystWatchlistPanel` or `PipelineLiveStatus` (only this tab and the Positions tab used them; Positions has its own import). `ManualTradeTicket` and `OrderFlowDiagram` are still used by the Overview tab, so they stay.

## How it was checked (no node_modules, no React types in the sandbox)
* the 275 JSX lines are identical to the original (`diff` empty);
* TypeScript compiler over all of `src`: no unresolved-name or import errors. One error line reads differently from before (`StatCard` `key`, `value: string | number` instead of `any`): it is the same existing `key`-prop error caused by the missing React types, and the `value` type is assignable to what `StatCard` expects;
* the 10 node test files in `npm test` pass.
**Not run here:** `npm ci`, full `tsc` with React types, `vite build`. Run them on the VM.

## Next slices
Still inside `RealAutoTrade`: board ~45, overview ~394, charges ~317, log. Next: charges (it computes totals inline, so look at what it reads before cutting), then log.
