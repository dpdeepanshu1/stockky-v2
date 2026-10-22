# E31 - route splitting (2026-10-23)

`frontend/src/App.tsx`: the eleven tab pages (Alerts, Training Lab, Paper Trades, Data Feed, Data Health, Hot, Surprise, IPO, Real Trade, Position Stocks, Calibration) are `React.lazy` chunks, loaded when their tab is first opened. Dashboard, watchlist, settings, the stock page and the sidebar stay in the main bundle. The tab content sits inside the existing per-tab `ErrorBoundary` (kept as the direct child of `<main>`, as `design-v4.test.mjs` requires) with a `Suspense` spinner. Vite splits on dynamic `import()`, so no `vite.config.ts` change.
The low-balance "open deposit panel" flow waits for the Trades chunk before it fires its event (`loadTrades()`), otherwise the 150 ms timer could fire before the page exists.
Standing rule: new heavy pages get `lazy(() => import(...))` too.
Checked: `design-v4.test.mjs` passes (it reads App.tsx). Not run: `tsc`, `vite build` (no node_modules, registry blocked) - so the real chunk sizes are not measured; on the VM `npm run build` lists them (the main `index-*.js` should shrink and one `Trades-*.js` etc. appear).
