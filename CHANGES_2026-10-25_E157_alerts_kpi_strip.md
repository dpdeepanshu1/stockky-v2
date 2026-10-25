# E157 - Alerts: v6 summary strip (2026-10-25)

Includes E156. Plan order after Paper Trades: Watchlist, Alerts, Settings. Status: `docs/AUDIT_STATUS_E157.md`.
Watchlist decision: no strip. Its v5 pulse (tracking count, up / down since added, best mover, weakest) already summarises the list, so a strip would repeat it.
Roll back: delete `frontend/src/components/alertsView.ts` and `frontend/src/alertsView.test.mjs`, remove the two imports and the one `<KpiStrip .../>` line in `NotificationsPanel.tsx`, and remove `alertsView.test.mjs` from `npm test` in `package.json`.

## What you will see
Four tiles above the Telegram card, shown once the settings have loaded:
| Tile | Shows |
| --- | --- |
| Channels | connected out of 4, with their names; "No channel connected - no alerts will be sent" with the warning sign at 0 |
| Switched on | channels both connected and switched on; "Connected but switched off: ..." with the warning sign when something is connected and nothing is on |
| Voice call | CallMeBot: Not set up, Off, or On with the recipient count (a missing count is "not reported", not 0) |
| Settings stored | Saved (survives restarts), or "Memory only - lost on restart - set CACHE_DATABASE_URL" with the warning sign; a dash when the backend does not say |
Only yes / no and counts are shown. No token, webhook, chat id or masked value is ever put in the strip (a test checks it). The channel cards, test button and the existing storage note are unchanged. No new request; nothing animates a number.

## Files
`frontend/src/components/alertsView.ts` (new, pure), `frontend/src/alertsView.test.mjs` (new, 42nd suite), `frontend/src/components/NotificationsPanel.tsx` (+2 imports, +1 line), `frontend/package.json`.

## Tests
Run here: `npm test` (42 suites), including `alertsView.test.mjs` (nothing connected, healthy, connected but off, mixed, enabled-but-not-configured, voice states, storage states, no secret leak); strict `tsc` on `alertsView.ts` alone. NOT run here: `tsc --noEmit` on the whole app, `vite build`, a browser, backend pytest.
