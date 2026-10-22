# E40 - pattern / regime chips always visible on the stock page (E26 finish)

`frontend/src/components/PatternStrip.tsx`, placed above the chart in `DecisionCard.tsx`: market mood (trend, volatility, momentum, structure), chart shapes, candles and signals as tap-for-meaning chips on the daily chart. Before, they only appeared after switching the chart to Candles.
Reuses `loadCtx` from `StockCandleBlock.tsx` (exported), so it shares the per-symbol cache and costs no extra request when the Candles view is opened. Renders nothing while loading or when there is no data. Context only: no gate, score or size reads it, so no calibration switch applies.
Not run in the sandbox: `npm ci`, `tsc`, `vite build`, a browser (registries blocked, no node_modules).
