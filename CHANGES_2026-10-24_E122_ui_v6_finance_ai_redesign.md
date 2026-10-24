# E122 — UI v6: finance + AI redesign of Real Trade, Position Stocks and Real Money Calibration

Frontend only. No backend, API or trading behaviour changed. Roll back: delete the `design-v6.css` import in `frontend/src/main.tsx`
(the pages keep working, minus the new look and the Trade History tab can be hidden by removing its entry in `RealAutoTrade.tsx`).

## What is new
- **Real Trade → "Trade History" tab** (new). Closed-trade records like Position Stocks has, built on the existing read-only
  `GET /positions/{mode}/records` (backend `trade_records.py`, previously used by no screen). Period (Today/7/30/60 days), Winners/Losers
  filter, symbol search, KPI tiles (net P&L, win rate, best, worst, charges), running-P&L line, win/loss bar, an AI-styled "Pattern read"
  (plain-words statements about what already happened; needs 3+ trades; never a forecast), expandable rows (gross/charges/net, slippage,
  best run/worst dip, tier, catalyst, source) and CSV export. Auto-refreshes every 60 s while the tab is visible. Read-only: nothing here places or closes a trade.
- **Shared hero** on all three pages: gradient panel with drifting price wave, breathing candles and a small neural net, plus an
  "Animations: On/Off" switch (remembered in localStorage when available).
- **AI strip** ("what the engine is doing": working/idle line + status chips) and **KPI tiles** (Real Trade, Position Stocks), built only from
  state the pages already held — no new requests.
- Tabs get a soft glow / lift; **Calibration switches** are now sliding toggles (still `<button role="switch" aria-checked>`).

## Safety rules kept
Money numbers never animate; direction always has an arrow/sign (not colour only); motion is transform/opacity only; every loop/entrance is off
under reduced-motion, TV mode, "Animations: Off", and loops pause on a hidden tab. Entrances use fill-mode `backwards` so no leftover transform
turns the page into a containing block for fixed pop-ups.

## Files
New: `design-v6.css`, `components/ui/{PageHero,KpiStrip,AiPulse,Spark}.tsx`, `components/realAuto/history.ts`,
`components/realAuto/tabs/HistoryTab.tsx`, `design-v6.test.mjs`, `history.test.mjs`.
Changed: `main.tsx`, `realTradeApi.ts` (+`TradeRecord`, `tradeRecords`), `realAuto/types.ts`, `RealAutoTrade.tsx`, `PositionStocksTab.tsx`,
`CalibrationControlTab.tsx`, `calibration/SwitchesPanel.tsx`, `realAutoTabs.test.mjs`, `package.json` (test script).

## Verified (sandbox had no npm registry, so no `vite build`)
All 20 node test files pass (`npm test` list); all three pages bundle with esbuild; changed files type-check against stub React types;
Chromium screenshots (dark, light, 390px mobile, reduced-motion, animations-off). Please run `npm install && npm run build` once on your side.
