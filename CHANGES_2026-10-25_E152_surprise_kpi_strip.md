# E152 - Surprise Momentum: v6 summary strip (2026-10-25)

Includes E151. Second screen of the plan's restyle order. Status: `docs/AUDIT_STATUS_E152.md`.
Roll back: delete `frontend/src/components/surpriseView.ts` and `frontend/src/surpriseView.test.mjs`, remove the two imports and the one `<KpiStrip .../>` line in `SurpriseStocks.tsx`, and remove `surpriseView.test.mjs` from `npm test` in `package.json`.

## What you will see
A row of four tiles above the results table:
| Tile | Shows |
| --- | --- |
| Hits | number of rows, with breakout and building counts |
| Top score | highest score and its symbol (a real 0 shows as 0; "-" only when nothing is numeric) |
| Top RVOL | highest relative volume as `5.0x` and its symbol |
| Scan | quotes ok / symbols scanned, and baselines loaded; if the scan got zero live quotes it shows the warning sign and "No live quotes - prices unreliable" |
Shown once there are rows or a scan summary. No new request. Nothing animates a number; the warning is spelled in words. The table, buttons and progress panels are unchanged.

## Files
`frontend/src/components/surpriseView.ts` (new, pure), `frontend/src/surpriseView.test.mjs` (new, 37th suite), `frontend/src/components/SurpriseStocks.tsx` (+2 imports, +1 line), `frontend/package.json`.

## Tests
Run here: `npm test` (37 suites, exit 0) including `surpriseView.test.mjs` (zero values, non-numeric rows, no live quotes, missing quote count is not "no quotes"). NOT run here: `tsc --noEmit`, `vite build`, a browser, backend pytest.
