# E32 - candlestick chart + pattern / regime badges (E25, E26) and a chart block per position

Context only: nothing here feeds a decision, gate, score or ML feature (C21 wiring is still pending the B-tools).

## Backend
- `shared/market_structure/patterns.py`: +5 candles from the whyXShubh guide (dragonfly doji, gravestone doji, spinning top, tweezer bottom / top) and
  cup and handle, so all 16 candle + 12 chart patterns of the guide exist (21 candles, 13 chart patterns in total). Chart patterns now carry
  `start` (+ `upper0` / `lower0`, `flag_high` / `flag_low`) so they can be drawn.
- `shared/market_structure/chart_context.py` (new): bars + candle markers + drawable pattern lines + support/resistance flips + plain-English badges + regime
  (trend, volatility, momentum, structure) in one call. Dates come from the bar index, never from epoch seconds.
- technical service `GET /chart-context/{symbol}?tail=` (503 when off: `CHART_CONTEXT_ENABLED=0`); api-gateway `GET /stock/{symbol}/chart-context` (always 200,
  `ok:false` when unavailable). Vendored copies re-synced (`sync_market_structure.py --check` ok).
- Tests: `shared/market_structure/tests/test_patterns.py` (+5), `test_chart_context.py` (new), generated service copies, gateway route test.

## Frontend
- `StockChart.tsx`: Line / Candles toggle on the full-size chart (DecisionCard). The small trade-card chart is unchanged.
- `StockCandleBlock.tsx`: per-stock block (candles, 1M/3M/6M/1Y, pattern lines, chips with tap-for-meaning, entry / stop / target lines). One request per symbol, 5 min cache.
  Placed in Real Trade (tracked positions open, live Dhan positions collapsed) and Position Stocks (open rows open, closed rows collapsed).
- `CandleChart.tsx`, `PatternBadges.tsx`, `chartContext.ts` (+ `chartContext.test.mjs`, added to `npm test`).

## Checked here
Shared suite 175 passed; service copies 31 passed; `tsc --noEmit` clean; `npm test` passes; `vite build` ok; server-render smoke of the chart (no NaN / undefined attributes).
Not run: pytest (not installable in the sandbox), a browser, real market data.
