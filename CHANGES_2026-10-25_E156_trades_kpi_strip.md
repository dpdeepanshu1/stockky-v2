# E156 - Paper Trades: v6 summary strip (2026-10-25)

Includes E155. Plan order: after the five discover screens comes the Dashboard, then Paper Trades. Status: `docs/AUDIT_STATUS_E156.md`.
Dashboard decision: no strip. Its AI Insights deck (design v5) already shows the scan counts and top picks from the same state, so a strip would repeat it. What is left there is the hero decision (shared hero or keep `dash-hero`), which stays open for you.
Roll back: delete `frontend/src/components/tradesView.ts` and `frontend/src/tradesView.test.mjs`, remove the two imports in `Trades.tsx`, put back the `StatCard` function and the six-card grid (Cash Balance, In Positions, Open P&L, Win Rate, Avg R, Max DD) where the one `<KpiStrip .../>` line now sits, and remove `tradesView.test.mjs` from `npm test` in `package.json`.

## What you will see
Under the big Total Equity number, the six plain cards are now one v6 strip with the same six readings:
| Tile | Shows |
| --- | --- |
| Cash | dummy cash not invested |
| In positions | value of open positions and how many are open |
| Open P&L | signed (`+₹1,235` / `-₹300`) with "up / down / unchanged on open positions"; a sub-rupee wiggle rounds to `₹0`, flat |
| Win rate | the reported percent and the closed-trade count; a dash when the backend gives none |
| Avg R | average R multiple; falls back to "Avg trade" (average P&L %), then to a dash |
| Max drawdown | as the backend reports it, not re-computed; a dash when not reported |
Fixes on the way: the old Max DD card was always red, even for a dash or 0; it is now plain. Missing values show a dash instead of an invented number, and a real 0 stays 0. Profit and loss are carried by the sign and words as well as colour. The Total Equity number, the open / closed / realized / after-charges line, the tabs, tables and buttons are unchanged. No new request; nothing animates a number.

## Files
`frontend/src/components/tradesView.ts` (new, pure), `frontend/src/tradesView.test.mjs` (new, 41st suite), `frontend/src/components/Trades.tsx` (+2 imports, six-card grid replaced by one line, unused `StatCard` removed), `frontend/package.json`.

## Tests
Run here: `npm test` (41 suites), including `tradesView.test.mjs` (nothing reported, normal portfolio, loss sign, zeros, sub-rupee rounding, NaN, Avg R fallbacks); strict `tsc` on `tradesView.ts` alone. NOT run here: `tsc --noEmit` on the whole app, `vite build`, a browser, backend pytest.
