# E112 - Market Weather card: saved / today / expiry per dial, a revert button, corrected help text (24 Oct 2026)

Closes open item 2 of the plan ("Market Weather card gaps"). Nothing here changes what a trading service does at the shipped settings; the card only shows
more, and gains one button that a person clicks.

## What changed
- **Saved vs today** (new card on the Market Weather tab). For Real Trade and Position Stocks separately: every Market Weather dial the service owns, its **saved**
  value (the Switches-page value), the value running **today**, and when today's value **ends** (HH:MM IST, always 15:30 while an overlay exists). A line above each
  table says why there is no overlay (service not `live`, nothing tightened, service unreachable) or how many dials are tightened and by which state.
- **Revert to saved** (one button per service, asks first). Clears that service's overlay now and its state stamp. It does **not** switch "Send dials" off, so a
  later 09:14 lock or a state change can tighten the service again while the gate is open; the confirm box and the card say so. To stop for the day: Send dials off
  as well.
- **Help text**: the line under "Dials for this state" no longer says going live "needs the shadow report to pass" (stale since E98, the gate is `replay_only`). It now
  follows the real mode and gate (`dialsHelp`).

## Where
- `services/calibration-runner-service/weather_overlay.py` (new, copied by the Dockerfile): `overlay_view` (reads each service's flag list through the existing
  `ControlClient.flags`), `revert` (one service; unknown service name refused; failures reported, never raised).
- `autocalib.py`: `ControlClient.overlay_clear` (DELETE /control/overlay on the service; the service's own kit already did this and audits it).
- `main.py`: `GET /weather/overlay` (read only) and `POST /weather/overlay/revert {service}`; both need the admin session like every runner route.
- Frontend: helpers in `weatherView.ts`, `runnerApi.weatherOverlay` / `weatherRevert`, the card in `WeatherPanel.tsx`.
- The card still never calls a trading service directly; it asks the runner.

## Rails kept
- `weather.py` and `weather_apply.py` are untouched; their static pins (no `.set(`, no `overlay_clear`, no `/control/flags`) still hold. The revert lives in its own module, has one call site and is reachable only from the button route.
- The old node test "shadow card has no revert button" was written for the shadow-only card; it is replaced on purpose by: the revert asks first, goes only to the runner's own route, and the card never touches a service's overlay endpoint.
- Reverting loosens a service back to its SAVED values (never beyond them); the kit allows this for a person (admin session).

## Proof
- `services/calibration-runner-service/tests/test_weather_overlay.py` (8): dial ownership per service, today = saved with no overlay, overlay shows saved / today / expiry / state, a down service is reported not raised, revert clears only the named service (both services tested), failure reported, unknown service refused, static rails.
- `frontend/src/weather.test.mjs` extended (rows, order, expiry, summary lines for live / shadow / unreadable, confirm and result text, help text for shadow / live-open / live-closed, stale sentence gone from the source).
- Deliberate breakages, all caught: expiry shown on an untouched dial, revert without confirm, stale help sentence back, summary ignoring shadow mode, revert always clearing Real Trade, today ignoring the overlay, unknown service allowed, down service raising.
- Run here: runner tests with the plain harness (all files that need only the standard library: green), `npm test` list (all 17 node files green), `tsc --strict` on `weatherView.ts` (clean).

## Not run here (no pytest, fastapi, node_modules in this workspace)
- `npm run build` and the type-check of `WeatherPanel.tsx` / `calibrationApi.ts` (the panel code was written against types checked in `weatherView.ts`).
- The pytest-only runner files (`test_main_routes.py`, `test_group466...`, `test_group478...` as pytest; the Dockerfile pin was called directly and passes) and the two new routes through FastAPI.
- Against a live service: do T3 first (a live morning), then read the new card; the numbers come from `GET /control/flags` on each service.
