# E57 - E31 step 1: RealAutoTrade.tsx split (helpers + leaf components)

`frontend/src/components/RealAutoTrade.tsx` 3,324 -> 2,658 lines. Moved to `frontend/src/components/realAuto/`, code unchanged (only `export` added):

| File | Lines | Holds |
| --- | --- | --- |
| `types.ts` | 3 | `Mode`, `Tab` |
| `format.ts` | 158 | `fmtInr`, `fmtHms`, `fmtTime`, `fmtDate`, `fmtDateTimeIst`, `fmtChargeDay`, `msFmt`, `pickNum`, `pnlColor`, `positionLevels`, `groupDhanOrdersBySymbol` |
| `parts.tsx` | 139 | `PositionCard`, `Dot`, `StatCard`, `SectionHdr` |
| `panels.tsx` | 391 | `PipelineLiveStatus`, `CatalystWatchlistPanel`, `OrderFlowDiagram`, `BreakerDot` and their label tables |

Stays in `RealAutoTrade.tsx`: the `RealAutoTrade` component (state, effects, JSX), `LivePositionRow` + `openLivePositions` (exported), `BalanceAllocation`, `PortfolioSummary`, `GateStep`. The route/default export and `data-mode={mode}` frame are untouched, so `design-v4.test.mjs` still finds them.

## How it was checked (the sandbox has no node_modules)
* the 638 moved lines are identical to the original lines (sorted comparison; only `export ` prefixes differ);
* TypeScript compiler API, every file: no "Cannot find name" / unresolved-import errors; the only extra diagnostics are "module react not found" for the new files, because React's type packages are not installed here;
* the 10 node test files in `npm test` that run without node_modules all pass.
**Not run here:** `npm ci`, `tsc` (full), `vite build`. Run them on the VM before deploying.

## Next slices
The `RealAutoTrade` component itself is still ~2,100 lines. The next safe cut is by tab (overview / board / live / positions / orders / watchlist / pipeline / charges / log), each as a component that takes the data it renders as props; `App.tsx` (2,147 lines) is a separate step.
