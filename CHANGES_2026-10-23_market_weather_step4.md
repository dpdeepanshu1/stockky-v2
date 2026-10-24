# Market Weather, Part 1 step 4: morning / refine / lock / tick in SHADOW (23 Oct 2026)

calibration-runner-service only. No trading service is touched; no switch, size or gate changes. `MARKET_STATE_MODE` stays `shadow`.

## What runs (new `weather.py`, started in `main.py`, Mon-Fri, India time)
- 08:00 `morning`: daily index + VIX history up to yesterday -> state -> Telegram brief.
- 08:50 `refine`: same, re-read, with the live VIX quote.
- 09:14 `lock`: adds the opening gap (pre-open price against yesterday's close). A lock missed by more than 5 min is skipped (it must end before the first tick).
- 09:20-15:25 `tick` every 5 min: move on the day, fall from the day's high, range in ATRs (from `/quote/^NSEI`: price, previous close, day high / low). The state can only go UP during the day. Telegram only when the state changes.
- Each stage writes a row to `<data>/weather/log.jsonl` (state, score, reasons, floors, gap, `live_would` = the dials the profile WOULD tighten). Last good state is kept in `<data>/weather/state.json` and survives a restart.

## Shadow guarantees (tested)
- `weather.py` has no way to send a dial: the only overlay body it can build is `_stamp_body` (`entries` is the literal `{}`); it never imports `build_overlay`, never calls `/control/flags`. A static test pins that.
- Optional `WEATHER_STAMP` (off): posts a stamp-only overlay at the lock and on each state change, so trades carry `market_state` / `profile_version` (the step-2 columns). A service that is down or has the mode off only misses its stamp.
- A quote counts as today's only if its previous close equals the last completed daily close (holiday / frozen feed fails): the state is then not moved from it.
- A stage that cannot get its data logs `ok=false`, keeps the last good state and never raises. Failed morning / lock are messaged; failed ticks are not.

## Switches and routes
- Config: `WEATHER_ENABLED` (default false: off until you switch it on), `WEATHER_STAMP` (false), `WEATHER_NOTIFY` (true). `PUT /weather/settings` stores the first and wins afterwards.
- Admin routes: `GET /weather/status`, `GET /weather/log`, `PUT /weather/settings`, `POST /weather/run {stage}` (runs one stage now and sends its message).
- `autocalib.ControlClient.overlay()` added (used only for the stamp). Dockerfile copies `weather.py`.

## Tests
`tests/test_weather.py` (16): stage timing / weekdays / no catch-up, holiday and stale-quote guard, calm and stressed mornings, gap-down floor at the lock, tick ratchet and message-on-change only, data failure, restart and new day, enabled / stamp / notify switches, bounded log, static "no dial can be sent". Whole runner suite through a plain harness: all pass (except `test_main_routes.py`, which needs pytest at import).

## Not done / limits
- `main.py` routes and startup wiring were syntax-checked only: fastapi / pytest are not installed here. Run the runner suite in CI.
- Not run against the real market-data service. Check once on the VM: `POST /weather/run {"stage":"morning"}` then `GET /weather/status`. `/history/^INDIAVIX` and `/quote/INDIAVIX` must answer (the replay job needs the first too).
- No `regime_shadow_report` yet (stamped trades joined with states). The log is what it will read. Holidays: a weekday holiday still gets a morning brief; ticks then skip on the stale-quote test.
- Needs the 5y replay verdict (step 3) before the shadow weeks mean anything.
