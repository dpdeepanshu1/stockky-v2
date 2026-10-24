# Market Weather, Part 1 step 6 prerequisites (23 Oct 2026): shadow report + protected list. NOT the live switch.

Nothing goes live here. `MARKET_STATE_MODE` stays `shadow`; no trading service, switch, size or gate changes. Step 6 (live in DEMO) is blocked on evidence that does not exist yet:
the step 3 replay verdict, and several weeks of shadow log. This step builds the two things step 6 needs before it can be considered.

## 1. Protected list (`autocalib.py`)
Added 15 keys the weekly calibrator can never give a rule: `STRUCTURE_BEARISH_BLOCK`, `STRUCTURE_BREAKOUT_VOLUME_MODE`, `STRUCTURE_BREAKOUT_MIN_VOL_RATIO`, `STRUCTURE_MAX_AGE_DAYS`,
`STRUCTURE_REJECT_STOP_WIDE`, `STRUCTURE_TRAIL_ENABLED`, the `PS_` copies of the first three, `PS_STRUCTURE_MAX_CHASE_ATR / MAX_STRETCH_ATR / MIN_ROOM_ATR`, and `MARKET_STATE_MODE / RISK_MULT / CONVICTION_ADD`.
Not blanket prefixes: the 8 `STRUCTURE_*` keys that already have rules (gate/stop/exit modes, chase, stretch, min room, min RR) stay calibrated, as before.
New drift test: every `STRUCTURE_*`, `PS_STRUCTURE_*` and `MARKET_STATE_*` switch in either catalog is either a rule or protected, so a future switch cannot be left loose by accident.

## 2. `regime_shadow_report` (new job, `scripts/regime_shadow_report.py`)
Joins closed real-trade records (`export_records`) with `<data>/weather/log.jsonl`. The state in force at each entry is the last good lock/tick row of the SAME day (never yesterday's, nothing before the 09:14 lock), so it needs no stamp on the trade.
Replays the profile: earliest entry, last entry, entries per day cap, risk multiplier. Not modelled and said so: conviction add (no score on the record), Position Stocks dials, charges on scaled size.
**The gate compares the skipped trades with the average trade of the same tight days.** A base strategy with a net loss "improves" under any rule that trades less, so PASS needs total change > 0 AND skip excess > 0 (the skipped trades were worse than average). Variants tried is logged (1: fixed profile).
Verdict `PASS | FAIL | NOT_ENOUGH_DATA` (needs 20 logged days and 30 trades on choppy/stressed/panic days). Job needs `export_records`; output `regime/regime_shadow.json` in the run folder.

## Tests
`tests/test_regime_shadow_report.py` (8): passes when late trades lose; fails when it only trades less; not-enough-data; cap and multiplier arithmetic; state lookup; script exit codes; job wiring; protected-list drift. Runner suite through the plain harness: 129 pass. Frontend tests unchanged and passing.

## Not done / still blocking step 6
- The step 3 `regime_history_replay` verdict has not been read; the shadow log has no weeks in it yet; the report has never seen a real record.
- Records do not carry `market_state` yet (the export omits it), so the report uses the log. Adding the stamp to `trade_records` would let it cross-check; left alone to avoid touching real-trade without running its suite.
- The live apply path (`weather.py` sending a dial) is deliberately not written. Do it only after a PASS you have read.
