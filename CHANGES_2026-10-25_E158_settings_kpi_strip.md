# E158 - Settings: v6 summary strip (2026-10-25)

Includes E157. Last of the ten tabs in the plan's strip order (Hot Picks, Surprise, IPO, Data Feed, Training, Dashboard, Paper Trades, Watchlist, Alerts, Settings). Status: `docs/AUDIT_STATUS_E158.md`.
Roll back: delete `frontend/src/components/settingsView.ts` and `frontend/src/settingsView.test.mjs`, remove the two imports and the one `<KpiStrip .../>` line in `components/app/SettingsPage.tsx`, and remove `settingsView.test.mjs` from `npm test` in `package.json`.

## What you will see
Four tiles under the Settings header:
| Tile | Shows |
| --- | --- |
| Backend | Connected, Unreachable (with the host that did not answer, warning sign) or Checking |
| API address | the saved host, or "Same origin" when none is set; "Edited, not saved - press Save URL" with the warning sign while the box differs from what is saved; "Not valid" when the box is not a full http(s) address |
| Theme | Dark or Light, this device |
| Databases | Not checked, Waking, Awake (with the per-target result), or Wake failed (with the detail, warning sign) |
Only the host of the address is shown, never a path, query or token (a test checks it). The Backend tile always describes the saved address, since that is what the connection check used. The sections below (connection box, appearance, databases, services, rate limits, overnight automation, deploy checklist) are unchanged. No new request; nothing animates a number.

## Files
`frontend/src/components/settingsView.ts` (new, pure), `frontend/src/settingsView.test.mjs` (new, 43rd suite), `frontend/src/components/app/SettingsPage.tsx` (+2 imports, +1 line), `frontend/package.json`.

## Tests
Run here: `npm test` (43 suites), including `settingsView.test.mjs` (host parsing, backend states, unsaved edit, bad address, same origin, theme, database states, no path / token shown); strict `tsc` on `settingsView.ts` alone. NOT run here: `tsc --noEmit` on the whole app, `vite build`, a browser, backend pytest.
