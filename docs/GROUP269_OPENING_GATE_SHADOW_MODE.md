# Group 269 - shadow mode for the opening gate

Lets you watch the 09:15 entries for a week or two before real money uses them.

## How to switch it on
- position-stocks-service: `OPENING_GATE_SHADOW=1`
- real-trade-service: `OPENING_GATE_SHADOW=true`
Default is off (the group 268 gate is live). Restart the service after changing it.

## What it does (only between 09:15 and `OPENING_GATE_SETTLE_IST`, default 10:00)
- No entry is placed in either service, like the old 09:30 start.
- Scalper: a symbol that passes every gate check gets ONE candidate-log row per day: decision SKIPPED, reason
  `OPENING_SHADOW:WOULD_ENTER ltp=<price> gap=.. vs_open=.. range_pos=.. or_pos=.. nifty_prev=..`. Find them with
  `GET /candidates/log?reason_prefix=OPENING_SHADOW`. Symbols the gate rejects are logged as before (`OPENING_GATE:<CODE>`).
- Real-trade: one INFO log line per symbol per mode per day, `OPENING_SHADOW would enter <SYMBOL> price=.. prev_close=..
  day_range=..`; the candidate stays queued and is evaluated by the normal rules after the settle time.
- After the settle time nothing changes.

## Judging it
Compare each `WOULD_ENTER` price with where the stock traded afterwards (target / stop levels the service would have used
are not logged). Nothing is simulated automatically.

## Tests
Scalper: 2 helper tests in tests/test_group268_opening_gate.py, 2 wiring tests in tests/test_entry.py. Real-trade: 3 tests in
tests/test_group268_opening_gate.py. Suites: position-stocks 2957 passed + the known `group210` failure (time-based, flaky:
failed 3 times in one run); real-trade 3824 passed, 1 skipped, 1 known error.
