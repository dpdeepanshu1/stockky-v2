# Market Weather, Part 1 step 7: the weekly learner `regime_learner` (23 Oct 2026). BUILT as a PROPOSAL-ONLY job. Nothing applied, nothing switched.

Defaults change nothing: no trading service, switch, dial, gate or catalog changed; `WEATHER_APPLY` and `MARKET_STATE_MODE` untouched. The learner cannot apply anything: it has no control client, no HTTP, and a test pins that (`ControlClient`, `.overlay(`, `.set(`, `/control/`, `requests`, `httpx`, `urllib` must not appear in the script).

## What it does (`scripts/regime_learner.py`, job `regime_learner`, group weekly, needs `export_records`)
Joins closed real-trade records with the shadow log (the SAME state lookup as `regime_shadow_report`, imported from it so the two can never disagree), then groups by market state x time of day (open < 09:45, mid, late >= 13:00).
- **Forgetting:** each trade weighted `0.5 ** (age_days / 45)`; effective sample size is Kish's.
- **Shrinkage:** each group's weighted mean pulled toward the all-trade mean with 15 trades' worth of weight.
- **Minimums:** 20 logged days, 90 joined trades, and 30 raw trades in a group before that group can say anything. Otherwise `NOT_ENOUGH_DATA` (exit 1).
- **Trial counter:** every group that reaches 30 trades is one trial; the confidence bar is the normal quantile of `1 - 0.05 / trials`, so more groups tested means a higher bar (Bonferroni-style deflation, the idea of the Deflated Sharpe paper; not the full formula).
- **One dial per run:** strongest evidence first; the other candidates are only counted (`other_candidates`).
- **LOOSEN:** the profile holds the bucket's dial tighter than the all-states default AND the group's lower bound is above zero -> one 30-minute step, never beyond the default (09:20 / 14:30). Not confirmed until the previous run on a DIFFERENT day proposed the identical change (history in `<data>/weather/learner_history.jsonl`; the same day twice does not count).
- **TIGHTEN:** the group's upper bound is below zero and the dial does not already close that bucket -> move it to close the bucket. The safe direction, so it is confirmed at once.
Verdicts `PROPOSE_TIGHTEN | PROPOSE_LOOSEN | NO_CHANGE | NOT_ENOUGH_DATA`; output `regime/regime_learner.json` in the run folder.

## Safety
None of the dials it can name is a calibrator rule (test), so a proposal is never picked up by autocalib. A confirmed proposal is for you to read and change by hand.

## Tests
`tests/test_regime_learner.py` (13): not-enough-data; a steady winner proposes one capped loosening; never beyond the default; noise proposes nothing; a small group cannot speak; more trials raise z; old trades fade; two-run confirmation (same day does not count); a changed proposal resets it; a losing bucket proposes tightening; the script cannot apply; job wiring; no learner dial is a calibrator rule. Plain harness: all calibration-runner test files pass except those needing pytest fixtures / fastapi (`test_main_routes` skipped: no pytest here; 7 fixture-based tests in older files cannot run in this harness and are not touched by this change).

## Not done / honest limits
- **Never run on a real record:** the shadow log has no weeks yet and the records carry no `market_state` stamp, so today it returns `NOT_ENOUGH_DATA`. Its thresholds (45-day half-life, shrink 15, 30 per group, 30-minute step) are a first draft, set before seeing any of your trades, and are not tuned.
- It covers two dials only (entry-not-before and last-entry time). Stop width, targets and trailing from the plan are NOT built: your own review said stop overshoot must be fixed first.
- The groups are not independent (the same trade sits in a state and a bucket), so the trial correction is approximate, not exact.
- Run it only after `regime_shadow_report` has PASSed. The job text says so, but nothing enforces the order.
- Not run against real services or in the real job runner page (job registration is tested; the page was not opened).
