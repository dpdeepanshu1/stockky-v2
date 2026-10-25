# E159 - Service Manager: v6 summary strip, and four fixed-text tiles removed (2026-10-25)

Includes E158. Plan order after the data tabs: login / admin gate / system-check / service-manager. Status: `docs/AUDIT_STATUS_E159.md`.
Roll back: delete `frontend/src/components/serviceView.ts` and `frontend/src/serviceView.test.mjs`, remove the two imports in `ServiceManager.tsx`, put back the four-tile `topo-grid` block (Render microservices, Upstash Redis cache "Connected", Supabase Postgres "Online (Free Tier)", GitHub Actions cron "09:00 - 15:30 IST") where the one `<KpiStrip .../>` line now sits, undo the `failed` / `checkedAt` state lines, restore the old pill text, and remove `serviceView.test.mjs` from `npm test` in `package.json`.

## Why this one is a fix, not only a look
Three of the four old tiles were fixed text: "Upstash Redis cache: Connected", "Supabase Postgres: Online (Free Tier)" and a cron window. They were never read from anything, so they said "Connected" even when it was not, and they name hosts that are not your Oracle setup. The fourth showed `okCount / max(total, 5)`, which read "0 / 5 Healthy" when the health check itself had failed. All four are replaced by tiles read from the last real health answer.

## What you will see
| Tile | Shows |
| --- | --- |
| Services | healthy / total, with the names that are down (first three, then "+N more"); a dash and "waiting for the first check" or "the health check did not answer" before any answer; never "0/5" |
| Required | required services answering, and which required one is down; a dash when the backend does not flag any |
| Slowest | the highest reported latency and its service; a real 0ms stays 0ms; a dash when none is reported (the per-service rows still show "<100ms" for a healthy service with no figure; left unchanged) |
| Last check | age of the last good answer; if the latest refresh failed it says so and that the earlier answer is shown |
The cluster-status pill now says "No answer from the health check" when the list is empty, instead of "0/5 healthy". The buttons, per-service rows, wake and reset logic are unchanged. No new request (the page already polls every 45 s); nothing animates a number.

## Not changed (your call)
- The sheet title ("5 Microservices Topology") and the line under it ("Zero-cost architecture across Render microservices, Upstash Redis, and Supabase Postgres") still describe an older hosting setup. Left as text; say if you want it reworded to the Oracle docker-compose setup.
- Login / admin gate (`AdminGate.tsx`): no strip. It is a sign-in form with no readings to summarise; its look belongs to the card-body pass.
- System check (`SystemCheck.tsx`): no strip. It is the start-up screen that walks through connecting, waking and ready, each phase already spelled out; its service list is the same data the Service Manager now summarises.

## Files
`frontend/src/components/serviceView.ts` (new, pure), `frontend/src/serviceView.test.mjs` (new, 44th suite), `frontend/src/components/ServiceManager.tsx` (+2 imports, +2 state lines, fetch sets them, grid replaced by one line, pill text), `frontend/package.json`.

## Tests
Run here: `npm test` (44 suites), including `serviceView.test.mjs` (first load, failed check with nothing known, empty answer, all healthy, down and required-down named, null entry counts as down, latency 0 / missing / NaN, failed refresh after a good one); strict `tsc` on `serviceView.ts` alone. NOT run here: `tsc --noEmit` on the whole app, `vite build`, a browser, backend pytest.
