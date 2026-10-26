# E168 - Settings: Power Off asks first and reports the truth; Save URL refuses junk (2026-10-26)

Includes E167. Ninth card-body screen and the last tab in the plan's order (Settings). Status: `docs/AUDIT_STATUS_E168.md`.
Roll back: in `frontend/src/components/app/SettingsPage.tsx` restore the old `powerOffAll` (an informational `window.alert`, then three raw `fetch(`${base}/ops/power-off` ...)` calls with `base = import.meta.env.VITE_API_URL || ""`, then "Ready for fresh start. Refreshing…" and a reload in every case), restore `save()` to `setApiUrl(url); onSaved(); ...` without the check, put `url` back (not `normalizeGatewayUrl(url)`) in the `settingsKpis` call and the green-only saved message; restore the import line; delete `checkApiUrl`, `powerOffReport` and `UrlCheck` from `components/settingsView.ts`; remove the "E168" block from `settingsView.test.mjs`. No CSS change.

## Bugs fixed
- **Power Off had no confirmation.** One tap (easy on a phone) force-stopped scan, data feed, hot picks and training and reloaded the page; the only prompt was an informational alert. It now asks "Switch OFF all processes?" first.
- **Power Off went to the wrong address.** It used only the build-time `VITE_API_URL`, ignoring the API address saved in Settings (separate deploys) and the shared request code. It now uses the same `api.powerOff()`, `api.stopAllScans()` and `api.stopDataFeed()` as the rest of the app.
- **Power Off said "Processes stopped. Ready for fresh start" and reloaded even when the request failed** (for example a 403 from the admin guard added in E149/E150, or the backend being down). It now shows "Power-off did not go through: <reason>. Nothing was confirmed stopped." and leaves the page and the local scan state alone. The local clean-up (keep-alive, stored scan task) now runs only after a confirmed power-off.
- **Save URL stored any text** ("hello world", "ftp://x"), which then broke every request. A usable http(s) address (a missing scheme is still added as before, so "my-vm.duckdns.org" works) is required; an empty box still clears the override. The refusal shows in red; the success message is still green.
- **The summary strip called a scheme-less address "Not valid"** while Save accepted it; it now reads the same normalised address Save would store (this also stops a trailing slash counting as an "Edited, not saved" change).

## Unchanged
The four summary tiles and their wording (E158), the Wake DB flow, Service Manager button, rate-limit dashboard, overnight automation, deploy checklist, theme switch, layout and styling.

## Files
`frontend/src/components/settingsView.ts` (+ `checkApiUrl`, `powerOffReport`, pure), `frontend/src/components/app/SettingsPage.tsx`, `frontend/src/settingsView.test.mjs` (extended, still 44 suites).

## Tests
Run here: `npm test` passes (44 suites, exit 0). Three deliberate breakages in the helpers and three in the component caught (URL check removed, failure branch removed, failure wording removed, confirm bypassed, reload-on-failure restored, Save check bypassed; the confirm guard was too weak at first and is now tightened). esbuild bundles the screen; strict `tsc` on `settingsView.ts` alone passes. NOT run: `tsc --noEmit` on the whole app, `vite build`, a browser, backend pytest, a real Power Off. Not checked: whether `api.powerOff()` carries your admin credentials the way the raw fetch did not; the shared request code is what every other protected call uses.
