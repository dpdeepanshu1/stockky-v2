# E61 - E31 step 5: RealAutoTrade.tsx "live" (Live Dhan) tab as its own component

`frontend/src/components/RealAutoTrade.tsx` 2,313 -> 2038 lines.

| What | Where now |
| --- | --- |
| Live Dhan tab JSX (207 lines) | `realAuto/tabs/LiveTab.tsx` (239 lines), unchanged |
| `LivePositionRow`, `openLivePositions`, `BalanceAllocation` | `realAuto/liveParts.tsx` (79 lines), unchanged except `export` on `BalanceAllocation` |

`BalanceAllocation` had to move too: the tab uses it, and importing it back from `RealAutoTrade.tsx` would make a circular import. `RealAutoTrade.tsx` still re-exports `openLivePositions` and `LivePositionRow`, so its public surface is the same.

`LiveTab` takes 15 props: `mode`, `loggedIn`, `status`, `dhanAccount`, `livePositions`, `liveHoldings`, `liveDhanOrders`, `liveLoading`, `liveError`, `loadLiveDhanData`, `psSplitPct`, `psAllocated`, `psAvailable`, `psError`, `soldTodayBySymbol`. State, the Dhan and Position Stocks loaders and the effect that loads them when the tab opens stay in `RealAutoTrade`.
`RealAutoTrade.tsx` no longer imports `CapitalSplitCard`, `StockCandleBlock`, `GlintBox`, `PriceRoll`, `groupDhanOrdersBySymbol` or `positionLevels` (only this tab used them).

## How it was checked (no node_modules, no React types in the sandbox)
* the 207 JSX lines and the moved helper blocks are identical to the originals (diff / exact-substring check);
* TypeScript compiler over all of `src`: no new error kinds, no unresolved-name errors; total 9,128 -> 9,126 (errors that left with the code). `recharts` is not installed here, so its import error is the same as before;
* the 10 node test files in `npm test` pass.
**Not run here:** `npm ci`, full `tsc` with React types, `vite build`. Run them on the VM.

## Next slices
Still inside `RealAutoTrade`: board ~45, overview ~394, pipeline ~281, charges ~317, log. Next: pipeline, then charges.
