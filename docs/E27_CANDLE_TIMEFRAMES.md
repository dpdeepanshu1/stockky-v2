# E27 - candles on every timeframe: 1m, 5m, 15m, 30m, 1h and daily

Context only, like E25 / E26: nothing here feeds a decision, gate, score or ML feature.

## What changed for you
- Every candle chart (Real Trade and Position Stocks blocks, and the Candles view of the stock chart) has a timeframe selector: **1m 5m 15m 30m 1h 1D**.
  The choice is remembered. Each timeframe has its own zoom buttons (for example 5m: 1D / 2D / 5D; 1m: 1H / 2H / 1D / 2D).
- Intraday charts show the clock time on the axis, a dotted separator where a new trading day starts, and the candle that is still forming as a
  hollow bar (labelled "forming"). The change % on intraday charts is against the previous session's close.
- Patterns, badges and the regime are read on the chosen timeframe, from closed candles only. The forming candle is never used for a pattern.
  Wording follows the timeframe ("latest candle", "20-candle average" instead of "today", "20-day average").
- A two- or three-candle pattern that would span the overnight gap (for example a bullish engulfing made of yesterday's last candle and today's
  first) is not shown on an intraday chart. Daily charts are unchanged.
- 1m and 5m charts carry a plain caution that they are noisy. If the data source is more than 3 minutes behind during market hours, the chart says so.
- While the market is open (Mon-Fri 09:15-15:35 IST) and the browser tab is visible, an open intraday chart re-reads on its own
  (1m every 30 s, 5m 60 s, 15m / 30m 120 s, 1h 180 s). Closed market or hidden tab: no requests.
- When a timeframe cannot be drawn (too few candles, data service busy) the card says why and offers Retry.

## Backend
- `shared/market_structure/timeframes.py` (new, pure stdlib): timeframe table, row parsing (a date without an offset is IST), session-aligned buckets
  (09:15-15:30; the last 30m / 1h bucket ends at 15:30, not at its nominal width), closed vs forming split, data lag, session-break rule.
  30m is built from 15m candles (Dhan has no 30m). 1m asks for 5 sessions, 5m / 15m for a month, 1h for 3 months.
- `chart_context(..., tf, live, live_date, lag_s)`: bars carry `"YYYY-MM-DD HH:MM"` labels on intraday, plus `tf`, `unit`, `note`, `live`, `data_lag_s`.
- technical service `GET /chart-context/{symbol}?tf=&tail=`: default `tf=1d` is byte-for-byte the old path and cache key. Unknown tf = 400.
  Intraday that cannot be drawn answers **200 with `ok:false` and a `reason`** (never a 5xx), cached for 30 s. Cache TTL while open: 1m 20 s, 5m 45 s,
  15m / 30m 90 s, 1h 180 s; 600 s when the market is closed.
- api-gateway `GET /stock/{symbol}/chart-context?tf=&tail=&force=`: always 200. Intraday uses its own breaker name `technical_chart`, so a slow 1-minute
  history can never open the breaker that the analysis features share. Tail cap 1500 intraday, 500 daily.
- market-data-service: `/history` truncated every answer to `MAX_HISTORY_ROWS` (260), which cut a 1-minute chart to 260 minutes. Intraday intervals now keep up to
  `MAX_HISTORY_ROWS_INTRADAY` (default 2600, never below the daily cap; blank or non-numeric = 2600). Daily keeps 260.
- Vendored `stockky_ms/` copies re-synced (`scripts/sync_market_structure.py --check` ok); `docs/ENV_REFERENCE.md` regenerated.

## Frontend
- `chartContext.ts`: `TIMEFRAMES`, `windowsFor`, `agoText`, `barLabel`, `axisLabel`, `dayBreaks`, `sessionChangePct`, `marketHoursIST`, `missingText`, saved choice.
- `CandleChart.tsx` rewritten for time labels, forming bar, session separators, dense 1m charts (one stroke per candle when bars are under ~2 px).
- `StockCandleBlock.tsx`: selector, per-timeframe cache and request sharing, silent auto-refresh (`setVisibleInterval`), Retry now really forces a fresh read.
- `api.getChartContext(symbol, tail, tf, force)`.

## Limits to know
- Free data: Yahoo keeps about 7 days of 1-minute data and 60 days of 5m / 15m. Dhan is tried first when enabled. A symbol listed days ago has few candles.
- Holidays: the browser only knows weekdays and hours; on a holiday it may re-read once per interval and get the cached answer.
- The structure badge on intraday charts is the DAILY structure read, labelled "Daily structure".

## Tests
- `shared/market_structure/tests/test_timeframes.py` (14), `test_chart_context_intraday.py` (9); service copies in analysis-intelligence-service;
  `test_e27_intraday_chart_payload.py` (9), gateway route tests (+4), market-data `test_e27_intraday_row_cap.py` (4), `chartContext.test.mjs` (E27 block).
- Not run here: pytest (not installed; each file was run with a plain runner), `npm ci`, full `tsc` / `vite build` (no node_modules, no network).
  The TSX files were type-checked against stubs for React and the API module.
