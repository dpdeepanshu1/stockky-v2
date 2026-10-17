# Group 423 - strictly independent evidence groups

## Problem
TQS has five groups (structure, zones, liquidity, location, risk). They are not independent: location is built from zone / fib / support /
room, which zones already read, and risk is derived from the stop / target that come from the same swings as room. P7 "needs 2 independent groups"
could therefore be met by one piece of evidence counted twice.

## Change (additive; defaults unchanged)
- Engine: `independent_groups_strict` (0..3) = positives among structure, entry zone (= max of zones and location) and liquidity. Risk is dropped
  (P4 gates R:R on its own). Summary key `ms_groups_strict`. `independent_groups_positive`, `ms_groups` and the TQS are untouched.
- Policy: rule `P7X_INDEP` (BUY NOW -> PREPARE TO BUY) when `groups_strict_min` > 0. Default 0 = off. Missing value trips it when on.
- Observation: `features.groups_strict`; with group 422 shadowing, `shadow:P7X_INDEP` and `features.would_be`.

## How to evaluate
1. On decision-prediction-service: `STRUCTURE_POLICY_PARAMS_JSON={"groups_strict_min":2}` and `STRUCTURE_POLICY_SHADOW_RULES=P7X`.
2. Wait for observations, compare outcomes of days with groups_strict < 2 against the rest.
3. Go live only if the difference is clear (and per the evidence gates). `P7` shadowing does not cover `P7X`.

Note: the technical service must run the new engine copy for `ms_groups_strict` to exist; until then a missing value counts as unknown.

## Verification
shared/market_structure tests (129, golden regenerated, only the new key differs), decision tests (156), 5 vendored copies in sync,
api-gateway full suite (8867), real-trade and position-stocks suites (only environment failures: oracledb, dhan SDK).
