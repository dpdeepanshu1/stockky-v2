# E69 - E31 step 13: App.tsx dashboard view as its own component

`frontend/src/App.tsx` 1,477 -> 1,163 lines. The dashboard branch of the tab switch (298 JSX lines) moved to `frontend/src/components/app/DashboardView.tsx` (345 lines), unchanged: the "Analyse any stock" hero with the market clock, symbol search, Quick scan and Full market scan buttons, the market sentiment header, market movers, the watchlist chips, and the idle / loading (progress card) / error / stock (`Pipeline` + `DecisionCard`) / scan (`ScanPanel` + `SignalStream`) states.

`DashboardView` takes 23 props: the view state and setter, the query and setter, the watchlist, the live-scan state (`liveScanRows`, `scanTransport`, `scanTaskId`, `isRetrying`, `cancelRequested`, `stoppingScan`, `wsLive`), the signal filter and setter, `setShowSettings` and eight handlers (`handleSearch`, `handleScan`, `handleStopScan`, `handleRetry`, the two watchlist adders and the two "send to Telegram" buttons). All state, effects and handlers stay in `App`.

Moved with it: `formatTime` (pure; only this view used it) and the `ViewState` type (now in `components/app/appTypes.ts`, both files need it). `App.tsx` dropped its imports of `Pipeline`, `DecisionCard`, `SignalStream`, `ScanPanel`, `MarketMovers`, `MarketSentimentHeader` and `MarketClock` (the unused `MultiHorizonScanLists` import went with the `ScanPanel` line).

## Test
`frontend/src/appHelpers.test.mjs` (in `npm test`) now also checks that `DashboardView` keeps the hero, the symbol-search label, both scan buttons, the sentiment header, movers, the five view states and all the cards, and that `App.tsx` no longer holds them; `App` passes every prop the view declares; and it runs the moved `formatTime` on 0, 59, 61, 3661, negative, NaN and a fractional value. No calibration switch: a refactor has no behaviour to switch.

## How it was checked (no node_modules, no React types in the sandbox)
* the 298 JSX lines are identical to the old `App.tsx`, and `formatTime` is identical apart from indentation and `export`;
* TypeScript compiler over all of `src` (temporary config, no type packages): the first run found two real mismatches (the two "send to Telegram" handlers return a Promise, the props said "anything"); fixed, and the error total is now equal to the one before the move (9,129), with no unresolved names and no wrong-type props;
* all 12 node test files in `npm test` pass.
**Not run here:** `npm ci`, full `tsc` with React types, `vite build`. On the VM, on the dashboard: search a symbol (e.g. TCS), run a Quick scan and watch the progress card, press Stop, run a Full market scan, trigger an error (wrong API URL) and use Retry, and click a watchlist chip.

## Next slice
`App` still holds about 1,013 lines of state, effects and handlers (scan runner, watchlist, realtime socket, backend checks) and the tab switch (~100 lines). The tab switch is the cheap cut; the handlers are best moved into hooks (`useScanRunner`, `useWatchlist`, `useBackendStatus`) one at a time, each with its own test.
