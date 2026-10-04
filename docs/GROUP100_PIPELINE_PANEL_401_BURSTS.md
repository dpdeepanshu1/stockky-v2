# group100 (2026-10-04) - item 29: admin-only calls made without a login session (401 bursts)

Cumulative on group99. Frontend-only change. Rebuild with `docker compose build frontend && docker compose up -d` (or `cd frontend && npm run build`).

## Cause
`RealAutoTrade.tsx::CatalystWatchlistPanel` (Pipeline tab) polls every 30 s and called `realTradeApi.resilienceStatus()` unconditionally. `GET /resilience/status` is `require_admin` on the server (not `require_admin_if_real`), so with no session token every poll was a 401. `rtRequest` sends the request without an `Authorization` header when there is no token, so the call reached the server each time. The REAL watchlist (`/watchlist-entries/REAL`, `require_admin_if_real`) was requested the same way when logged out.

The two calls shared one `Promise.all`, so the 401 on the admin-only call also rejected the whole batch and the DEMO watchlist never loaded for a logged-out user.

## Fix (`frontend/src/components/RealAutoTrade.tsx`)
- The panel now reads the session token before each poll. `/resilience/status` is requested only with a token. The watchlist is requested for DEMO always, and for REAL only with a token. REAL with no token makes no request.
- The two requests use `Promise.allSettled`, so each result lands on its own and a failure or skip leaves that part's last-known data on screen.
- The panel takes a `loggedIn` prop (passed from the dashboard) so it reloads right after login rather than waiting up to 30 s.
- `rtRequest`, the backend routes and every other caller are unchanged.

## Not changed / not verified
- **Other callers of `rtRequest` that use the default `requireAuth = true`** were not changed. I did not make `rtRequest` throw locally when there is no token, because several routes are `require_admin_if_real` and work without a token in DEMO mode, so a blanket rule could break them. The other pollers I read (pipeline, candidates, positions, orders, live Dhan data) are already gated on `loggedIn`/`armed`/mode.
- **Armed but logged out:** the Pipeline, Watchlist and Positions pollers still run when the gate is armed with no session (by design, for auto-pilot). `positions/REAL` and `candidates` would 401 there if the server requires admin for them; I did not check each route's server-side gate.
- **Other frontends/services:** the 401 burst in your log may also come from somewhere else (position-stocks calls already refuse locally without a token). I have not seen your VM's log, so I cannot confirm this panel was the only source.
- **Tests:** the frontend has no test runner, so there is no unit test. I ran `tsc --noEmit` on the frontend: no errors. Python suites are untouched by this change and were not re-run.
