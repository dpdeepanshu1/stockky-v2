# group 486 - 21 Oct 2026 loss review + two fixes

## Why real-trade lost -217 today (7 trades, 1 win)
- Charges are NOT the cause: same-day trades cost about Rs 2 each. The loss is price movement.
- Three of the seven trades entered at 09:16 (NYKAA, GMRAIRPORT, INDUSTOWER) and lost -165 of the -217. Two of them were stopped out (-3.51 %, -2.85 %).
- The entry guards for REAL have been switched off by an admin override since 19 Oct 07:44-07:45 UTC: OPENING_GATE_ENABLED, OPENING_PROTECT_ENABLED
  (half size + pause after 2 stop-outs), OPENING_ENTRY_GUARD_ENABLED, ENTRY_VWAP_GATE_ENABLED, ENTRY_DEPTH_RECHECK. position-stocks still has them on.
- The structure gate for REAL was switched off by calibration-runner at 2026-10-21 01:28 (see Fix 1).
- Payoff is poor: since 12 Oct same-day trades win 34.6 % with average win +16.9 and average loss -32.5 (payoff 0.52). Stops are software-only
  (BROKER_STOP_INTRADAY=false), polled, so stop fills land beyond the planned stop.
- Not the cause: the api-gateway circuit breaker (group 485) only wraps /stockky-hot and /surprise/ipo/list, never an exit.

## Fix 1 - autocalib no longer switches the structure gate off
G2 FAIL means "benefit not proven" (no_edge, kept share below 30 %), not "harmful". r_g2_gate used to answer `off`; it now answers no change.
Test updated. The G2 PASS -> enforce path is unchanged. If you want the gate off, switch it yourself.

## Fix 2 - structure-refresh POST returns at once
plan() read every feed row before the route answered, so the POST blocked and daily_close timed out at 60 s. Planning now runs in the worker thread;
dry_run still plans synchronously. "Nothing to do" is now a job status `done`, not a synchronous reply.

## Not changed (your call)
Re-enable the five opening/entry guards for REAL, set STRUCTURE_GATE_MODE back to enforce, and re-measure after a week. Pre-existing test failure:
test_autocalib.test_every_rule_points_at_a_real_switch_and_a_real_test (SIZE_LADDER_MAX_LEVEL) fails on the untouched zip too.
