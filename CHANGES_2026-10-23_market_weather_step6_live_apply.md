# Market Weather, Part 1 step 6: the gated live apply path (23 Oct 2026). BUILT, OFF, and nothing has been switched on.

Defaults change nothing: `WEATHER_APPLY=false`, every trading service still has `MARKET_STATE_MODE=shadow`. No trading service code changed.

## Three locks; all must be open before one dial moves
1. **Runner switch:** `WEATHER_APPLY` / `PUT /weather/settings {apply:true}` / the "Send dials" button (confirm). Off by default.
2. **Evidence gate** (`weather_apply.evidence_gate`): a `regime_history_replay` run whose output says `VERDICT: USEFUL` (<= 45 days old, `USEFUL_NOT_BEYOND_ATR` does not count) AND a `regime_shadow_report` run saying `REGIME_SHADOW: PASS` (<= 14 days old). No result, old result, or any other verdict: no dial.
3. **The service's own switch:** the runner reads each service's `MARKET_STATE_MODE` and sends nothing unless it is `live` (you set it; it is protected from the calibrator). The service's kit then refuses anything that is not a tightening of its SAVED value, any non-overlay dial, and expires everything at 15:30 IST. The runner also clamps locally (`profiles.build_overlay`), so one loose dial cannot make a service reject the whole call.

## When it sends
At the 09:14 lock, and on each intraday state change (the state can only go up). The morning and refine stages never send. A failed stage never sends. Service down or refusing: that service gets no dial, the other still can; an overlay already set stays until 15:30 (the safe direction). Past 15:30: nothing. Calm / Normal send an empty stamp-only overlay. A service that got a dial overlay is not stamped twice.
Telegram: one "dials applied / NOT applied" message per apply with what each service received or why not. Status gains `mode` (`live` only when switched on AND an applier exists), `apply`, `last_apply`; log rows carry `shadow:false` while it is on.

## Structure
`weather.py` still CANNOT build a dial (no `build_overlay`, no `.set(`, no `/control/flags`; pinned by test). It can only call an injected applier. `weather_apply.py` is the only module that builds one, uses only `ControlClient.flags()` (read) and `.overlay()`, and never `.set()` / overlay_clear.
Stop at once: switch "Send dials" off AND set `MARKET_STATE_MODE=shadow` on the service (that clears today's overlay). Switching only the runner off leaves today's overlay until 15:30.

## Frontend
LIVE chip (red) vs SHADOW chip, "Send dials" switch with the three locks written beside it, and the last apply attempt per service.

## Tests
`tests/test_weather_apply.py` (11): gate (prefix verdict must not pass, ages 45 / 14 days), closed gate reads nothing, only tightenings vs saved values, real-trade-only dials never sent to position-stocks, service not live, refusing / unreachable service, stamp-only for calm, nothing after 15:30, hook timing (lock + change only), off by default, a crashing applier contained, static pins. Runner suite via the plain harness: 140 pass. Frontend: all 14 files pass.

## Not done / before you flip anything
- NOT run against real services. I did not run the kit against these bodies end to end here (no pytest / fastapi); the profile-vs-catalog test from step 3 is the only check that every dial exists and is overlay-capable. Do the DEMO flip first, one service, and read `GET /control/flags` (`overlay` field) afterwards.
- The two verdicts the gate needs do not exist yet (replay not run, shadow report has no shadow weeks to read). Until they do, switching "Send dials" on only produces a "NOT applied: ..." message.
- `main.py` wiring is syntax-checked only.
