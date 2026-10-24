# Market Weather, Part 1 step 5: the Market Weather card (23 Oct 2026)

Frontend only. No trading service and no runner code changed; no switch, size or gate changes.

## What you see
New section **Market Weather** on Real Money Calibration (next to Auto-calibration). It reads `GET /calibration/weather/status` every 30 s (nginx already proxies `/calibration/`).
- **Three states:** OFF (nothing computed), WAITING (on, no state yet today, or the last stage got no data), ACTIVE (state, score, open gap, the reasons and floor reasons).
- **Dials it would tighten:** the profile row for the current state. Calm and Normal list none (stamp only). Labelled "not applied".
- **Today's stages:** each logged row (time, stage, state, "changed from", note or error) and four "Run <stage> now" buttons (confirm first; each logs a row and sends its Telegram message).
- **Settings:** Switch ON/OFF, Telegram ON/off, Trade stamp ON/off (stamp-only overlay, no dials).
- **All states table** (collapsed): every dial across the five states, current column bold.
- A state left over from an earlier day is shown as "Last state was ... Today has not started yet".

## Differences from the plan's card (deliberate, because step 4 is shadow)
- No "saved value / today's value / expiry" columns and no mode switch or revert button: the runner sends no overlay and does not know the saved values, so there is nothing applied to show or revert. They come with step 6 (live), when the services' `overlay` field exists to read.
- The Run-all bar is hidden on this section (it is per service; the weather is not).

## Files
`weatherView.ts` (pure helpers, no imports), `components/calibration/WeatherPanel.tsx`, `calibrationApi.ts` (+4 `runnerApi.weather*` calls), `CalibrationControlTab.tsx` (section), `weather.test.mjs` + `weather.fixtures.json` (real status payloads from the runner's own `weather.py`), `calibration.test.mjs` (Run-all assertion now also excludes weather), `package.json` (test script).

## Tests
`weather.test.mjs` covers the three states, stale-day, label coverage for every profile dial, and that the card has no revert button and calls no overlay or `/control` endpoint. All 14 frontend test files pass under node 22.

## Not done
- `npm run build` / full `tsc` not run (no node_modules or network here); the new files were type-checked with only the react type errors left. Run `npm run build` in CI.
- Not seen in a browser.
