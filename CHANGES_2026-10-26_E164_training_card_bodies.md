# E164 - Training Lab: card bodies read the numbers safely, and "no result yet" is no longer drawn as a miss (2026-10-26)

Includes E163. Fifth card-body screen (plan order: Hot Picks, Surprise, IPO, Data Feed, Training, then Paper Trades, Watchlist, Alerts, Settings). Status: `docs/AUDIT_STATUS_E164.md`.
Roll back: in `frontend/src/components/Training.tsx` restore the old `formatValue` + `metrics[key]` tiles, the Sharpe `?.toFixed(3) || "—"`, and the old prediction row (`₹{p.price?.toFixed(2) || "—"}`, the two `p.tN_success === 1 ? "✅" ...` spans, the `o.return_pct?.toFixed(2) || "—"` outcome text); drop the helpers from the import and from `components/trainingView.ts`; delete the E164 block at the end of `design-v7.css`; remove the "E164" blocks from `trainingView.test.mjs`.

## Bugs fixed
- **A prediction that has not been evaluated was drawn as a failure (❌).** The backend uses 0 = pending, 1 = hit, 2 = miss (`decision-prediction-service/training/evaluate.py`); the old cell drew ✅ for 1, ⏳ for 0 and ❌ for everything else, including a missing value. Missing now shows a dash and "Not evaluated".
- **A non-number metric broke the tab.** The metric tiles and the Sharpe card called `.toFixed` on whatever the API sent; a string or NaN threw inside render. Such a value now hides its tile (Sharpe shows a dash). A profit factor of infinity (no losing trades) shows "∞"; a real 0 stays 0.
- **Prediction rows:** a missing price showed "₹—" (now "—"); a missing return showed "—%" (now "T+10: —").

## What you will see
- Hit / Miss / Pending / Not evaluated as a glyph plus a word in the T+1 and T+5 columns (no longer colour only).
- Everything else on the screen is unchanged: summary strip (E155), training controls and progress, period roll-up, insights, headings and column names.

## Files
`frontend/src/components/trainingView.ts` (+ `fmtMetric`, `fmtFixed`, `outcomeMark`, `predictionRowView`, pure), `frontend/src/components/Training.tsx` (metric tiles, Sharpe card, prediction rows), `frontend/src/design-v7.css` (static rules, no animation), `frontend/src/trainingView.test.mjs` (extended, still 44 suites).

## Tests
Run here: `npm test` passes (44 suites, exit 0). Four deliberate breakages caught (miss code removed, unknown drawn as a miss again, price 0 shown, infinite profit factor removed). esbuild bundles the screen; strict `tsc` on `trainingView.ts` alone passes. NOT run: `tsc --noEmit` on the whole app, `vite build`, a browser, backend pytest.
