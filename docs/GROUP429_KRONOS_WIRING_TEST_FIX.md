# Group 429 - `test_group425_kronos_entry_wiring::test_gate_5_6_enforce_waits_on_cost_model` (test only, no service code changed)

## What was failing
In group 425 this test (and its log-mode twin) fed Gate 5.6 a forecast EV of 0.4 % and a cost-to-edge floor of 3x, expecting a WAIT with `gate_tag="cost_model"`. It failed on the group 426 zip (the test was never green after the cost model changed), so every real-trade run ended with 1 failure.

## Cause (checked, not guessed)
Not the clock, not the code. The test's premise went stale: group 266 (verified on the Dhan contract note of 07-Oct-2026) prices a same-day CNC round trip like MIS (no delivery STT on both legs). With that, a 369-share, Rs 36,918 trade costs about Rs 39 (0.107 %), so a 0.4 % target is 3.75x the cost - above the 3x floor, and Gate 5.6 correctly lets it through. The gate behaved as designed; the fixture assumed older, higher charges. Ran the file with the clock frozen at 10:30 and 16:00 IST: same failure both times, so entry_product's 14:45 IST switch is not involved.

## Fix
Both Gate 5.6 tests now set `MIN_EDGE_TO_COST_RATIO` to 10x (a 0.4 % EV is about 3.7x, so it WAITs; the fixed ~6 % target is about 55x, so log mode still enters). The result no longer depends on the exact charge constants. `tests/test_group425_kronos_entry_wiring.py` only.

## Your VM
`git status` there showed this file as locally modified. If your local edit is the same idea, discard yours before pulling this one: `git checkout -- services/real-trade-service/tests/test_group425_kronos_entry_wiring.py`. If it is something else, keep yours and tell me what it does.

## Verified
real-trade 4939 passed, 1 skipped, 0 failed (previously 5 failed in the sandbox; the 4 `test_oracle_compat` failures there were the missing `oracledb` driver, not code).
