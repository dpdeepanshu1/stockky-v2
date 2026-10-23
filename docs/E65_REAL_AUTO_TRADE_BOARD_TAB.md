# E65 - E31 step 9: RealAutoTrade.tsx "board" tab as its own component

`frontend/src/components/RealAutoTrade.tsx` 1,429 -> 1,385 lines. The Board tab (an inline function in the JSX, 42 lines) moved to `frontend/src/components/realAuto/tabs/BoardTab.tsx` (60 lines).

`BoardTab` takes 4 props: `mode`, `status`, `positions`, `candidates`. Everything else it uses (the risk-cap check, the held / candidate sets, `realTradeApi.topPicks` / `shortlist`) is derived inside it, exactly as before. `RealAutoTrade` no longer imports `MarketBoard`, `ShortlistPanel`, `TopPicks` or `riskCap`.

One text change: the three panels' `active={activeTab === "board"}` became `active`. The tab is only mounted while it is the open one, so the value is the same.

## Test (new)
`frontend/src/realAutoTabs.test.mjs`, added to `npm test`: every tab file exists, is imported and rendered by `RealAutoTrade`; `BoardTab` keeps the three panels, the risk-cap hatching and the held / candidate chips and does not read `activeTab`; `RealAutoTrade` no longer holds the board code or its imports. No calibration switch: this is a refactor, no behaviour to switch.

## How it was checked (no node_modules, no React types in the sandbox)
* the 42 moved lines are identical to the original except the three `active` props (`diff` empty);
* TypeScript compiler over all of `src` (temporary config, no type packages): no new error kinds, nothing unresolved, nothing from `BoardTab` beyond the missing-React-types noise; total 9,123 -> 9,119;
* the 11 node test files in `npm test` pass.
**Not run here:** `npm ci`, full `tsc` with React types, `vite build`. Run them on the VM, then open Real Trade -> Board and check Top picks, Best picks, the market board (hatched tiles = over the risk cap) and the "positions and candidates" chips.

## Next slices
Only the overview tab (~394 lines, the most state) is left inside `RealAutoTrade`; likely best cut into 3-4 cards (arming sequence, Dhan account, risk configuration, account snapshot). `App.tsx` (2,147 lines) is separate.
