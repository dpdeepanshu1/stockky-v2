# E153 - IPO Tracker: v6 summary strip (2026-10-25)

Includes E152. Third screen of the plan's restyle order. Status: `docs/AUDIT_STATUS_E153.md`.
Roll back: delete `frontend/src/components/ipoView.ts` and `frontend/src/ipoView.test.mjs`, remove the two imports and the one `<KpiStrip .../>` line in `IpoTracker.tsx`, and remove `ipoView.test.mjs` from `npm test` in `package.json`.

## What you will see
Five tiles under the IPO lifecycle overview, shown once the list has rows:
| Tile | Shows |
| --- | --- |
| Tracked | rows in this window, and "of N scanned" when the scan saw more |
| Upcoming | IPOs not yet listed (upcoming or pre-listing) and the earliest listing date |
| Avg listing gain | average listing gain of listed IPOs, with a sign and an arrow (+4.2% / -3.3%); a flat 0.0% is a reading; a dash when no listed IPO has a figure |
| Buy signals | rows marked BUY NOW or PREPARE TO BUY |
| Awaiting data | rows with no price data yet or a data error; warning sign and words when above zero |
No new request. Nothing animates a number. Direction is in the sign and the words, not only colour. The list, scan buttons and health panel are unchanged.

## Files
`frontend/src/components/ipoView.ts` (new, pure), `frontend/src/ipoView.test.mjs` (new, 38th suite), `frontend/src/components/IpoTracker.tsx` (+2 imports, +1 line), `frontend/package.json`.

## Tests
Run here: `npm test` (38 suites, exit 0) including `ipoView.test.mjs` (stage groups, signed average, flat 0%, missing gain, bad date, no-warning case). NOT run here: `tsc --noEmit`, `vite build`, a browser, backend pytest.
