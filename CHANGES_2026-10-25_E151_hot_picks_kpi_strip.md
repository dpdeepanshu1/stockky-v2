# E151 - Hot Picks: v6 summary strip (2026-10-25)

Includes E150. First screen of the plan's restyle order (Hot Picks, Surprise, IPO Tracker, Data Feed, Training, ...). Status: `docs/AUDIT_STATUS_E151.md`.
Roll back: delete `frontend/src/components/hotView.ts` and `frontend/src/hotView.test.mjs`, remove the two imports and the one `<KpiStrip .../>` line in `HotStocks.tsx`, and remove `hotView.test.mjs` from `npm test` in `package.json`.

## What you will see
Under the Hot Picks header, once picks are loaded, a row of four tiles (the same KPI strip Auto Trade and Position Stocks use):
| Tile | Shows |
| --- | --- |
| Picks | total picks, with bulk / insider, results and news counts underneath |
| Top score | highest score among the picks (a real 0 shows as 0; "-" only when nothing is scored) and its symbol |
| Source | "Live scan", or "Stored feed" with its age; a stale stored feed shows the warning sign and the words "Stale - ... run a fresh scan" |
| Universe | universe size and scan seeds; a stopped scan shows the warning sign and "Partial - stopped after N symbols" |
No new request: every value comes from the payload the tab already holds. Nothing animates a number; the warning is spelled in words, not only colour. The existing "Updated ..." line and the pick sections below are unchanged. The pick cards themselves are still the old look (next step).

## Files
`frontend/src/components/hotView.ts` (new, pure), `frontend/src/hotView.test.mjs` (new, 36th suite in `npm test`), `frontend/src/components/HotStocks.tsx` (+2 imports, +1 line), `frontend/package.json`.

## Tests
Run here: `npm test` (36 suites, exit 0) including `hotView.test.mjs` (zero score, missing score, stale stored feed, partial scan, missing universe), `audit_ui_coverage.py` (Hot Picks now lists KpiStrip).
NOT run here: `tsc --noEmit`, `vite build`, a browser. The component types line up by reading (tone strings match `KpiTone`, values are strings), but only `tsc` and a look at the screen prove it.
