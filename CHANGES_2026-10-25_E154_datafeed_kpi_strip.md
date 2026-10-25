# E154 - Data Feed: v6 summary strip (2026-10-25)

Includes E153. Fourth screen of the plan's restyle order. Status: `docs/AUDIT_STATUS_E154.md`.
Roll back: delete `frontend/src/components/feedView.ts` and `frontend/src/feedView.test.mjs`, remove the two imports in `DataFeed.tsx`, put back the four-tile `<div className="grid grid-cols-2 sm:grid-cols-4 ...">` block (Stocks in feed, Last success, Job, Progress) where the one `<KpiStrip .../>` line now sits, and remove `feedView.test.mjs` from `npm test` in `package.json`.

## What you will see
The four hand-made tiles under the buttons are now one v6 strip with the same four readings, plus a fifth when the live quote socket reports:
| Tile | Shows |
| --- | --- |
| Stocks in feed | symbols stored, with "of N in the universe" when known; a real 0 shows as 0, a dash only when no count exists |
| Last success | age (`1.5h ago`) and the IST time; "Never" with a warning when no feed finished; older than 24h shows "Stale - older than 24h - run a feed"; an unreadable date shows a dash and says so |
| Job | idle, running, done, stopped, error, stalled, with the job message; stopped, error and stalled carry the warning sign |
| Progress | `processed/total` and the percent; "partial, press Resume" with the warning sign when a stopped run is part-way; a dash and "no run yet" when there is no total |
| Live quotes | only when the socket state is known: `N ticking` of M subscribed, or "Reconnecting - falling back to REST" with the warning sign |
No new request: every value comes from state the tab already holds. Nothing animates a number; every warning is in words as well as colour. The buttons, progress bar, checkpoint notes, banners and price alerts are unchanged.
One behaviour change: the old Last success tile showed the local-format time only; it now leads with the age, and the IST time moves to the sub-line.

## Files
`frontend/src/components/feedView.ts` (new, pure), `frontend/src/feedView.test.mjs` (new, 39th suite), `frontend/src/components/DataFeed.tsx` (+2 imports, old tile grid replaced by one line), `frontend/package.json`.

## Tests
Run here: `npm test` (39 suites, exit 0) including `feedView.test.mjs` (nothing loaded, zero count, count fallbacks, fresh / stale / unreadable / future dates, job states, partial vs running vs complete progress, socket up / unknown / down); strict `tsc` on `feedView.ts` alone, exit 0. NOT run here: `tsc --noEmit` on the whole app (no `node_modules`), `vite build`, a browser, backend pytest.
