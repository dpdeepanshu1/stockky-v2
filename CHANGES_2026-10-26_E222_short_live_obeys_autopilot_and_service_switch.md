# E222 - position-stocks: live shorts now obey the module and auto-pilot switches

Includes E221 and E220.

## What I did
Read `main._run_cycle` and everything it calls (gates, filters, pullback re-arm, pick loop, trading loop), then followed the loop into `short_entry.run_short_cycle` and `short_live._live_guards`, comparing the switches each path reads.

## Finding (fixed)
`_run_cycle` stops automatic long entries on `service_enabled=false` (module off), `is_armed=false`, and `auto_pilot_enabled=false` (AUTO trigger). The live short path read ONLY `is_armed` and the kill switch. So with `SHORT_MODE=live`:
- `POST /autopilot/disable` ("stop automatic entries") still let real SELL Super Orders open shorts every tick;
- `POST /service/disable` ("screening + entries stop") did the same.
Only `/disarm` or `/kill` stopped them. It also read the gate with an unfiltered `.first()` instead of the REAL row the rest of the service uses.

Now: `short_live._switches_off(gate)` checks armed, `service_enabled`, `auto_pilot_enabled` (NULL reads as off, like `_run_cycle`); `_live_guards` uses it, and the order step and guards read `filter_by(mode="REAL")`. `short_entry.run_short_cycle` calls `short_live.automatic_entry_off` in live mode and returns `{"skipped": code}` before the scan and quality-gate calls. Leftover shadow rows are still resolved. Shadow mode is unchanged (it places nothing). Cover / square-off of existing shorts is unchanged (exits ignore these switches, as for longs).

## Reviewed, not changed
- Pullback re-arm step consumes every armed setup after a global skip (kill switch, budget) instead of stopping like the main pick loop. Loses setups only, no order risk. Left as is.
- Live shorts do not use the long loss brake or the `MARKET_CLOSED_PRICE_PROOF` verdict. Shorts have their own gates; deciding whether they should share the loss brake is your call.

## Tests
`scripts/test_short_live.py`: +15 checks (93/93 pass); the first new one fails on the E221 code (a short is placed with the module disabled). `tests/test_short_live.py`: +1 static test, run directly, not under pytest. No config, env or frontend change.

## Run on your VM
`bash scripts/docker_test.sh position-stocks-service`, then restart position-stocks-service. Only matters once `SHORT_MODE=live`.

## Next unread
`run_exit_reconciliation`, `short_entry.plan_short` gate walk (`short_gates.evaluate`).
