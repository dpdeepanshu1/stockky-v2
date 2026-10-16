# Group 414 - P8 retest / CHOCH-confirmed branch (plan item, default OFF)

Cumulative on group 413.

## Why
BUY NOW only accepted a bullish BOS at most 10 bars old, so a pullback entry into an old FVG / order block / golden pocket could never be BUY NOW
(it stayed `P8_OLD_BOS` -> PREPARE TO BUY). The plan's "retest and CHOCH-confirmed" branch was not built.

## Rule (decision-prediction-service `decision/structure_policy.py`, `retest_confirmed`)
`P8_OLD_BOS` is excused only when ALL hold:
- the bullish BOS is older than `bos_max_bars` but NOT failed (a failed BOS = `P8_FAILED_BOS`, no BOS = `P8_NO_BOS`: never excused);
- daily trend is not bearish;
- a bullish CHOCH is at most `retest_confirm_bars` (5) bars old (a bearish CHOCH is not a confirmation; a fresh one is still `P5_BREAK` -> WAIT);
- price (live price when given) is inside a live bullish zone: FVG or order block (0.5% tolerance, not filled / broken / expired) or the golden pocket.
Every other rule (P2 chase, P3 stretch, P4 R:R / reward, P5, P6 room, P7 groups) still applies, so this can only turn PREPARE into BUY NOW when everything else is clean.
The `structure` block of the response carries `retest_entry`: the zone names that excused the old BOS, else `null`.

## Switch (default OFF - the A/B verdict is still `no_edge`)
- `STRUCTURE_RETEST_ENTRY=1` or `STRUCTURE_POLICY_PARAMS_JSON={"retest_entry":1,"retest_confirm_bars":5}`.

## Replay (so it can be tested before it goes live)
`shared/market_structure/backtest.py` `policy_flags` takes the same two thresholds (`retest_entry`, `retest_confirm_bars`; vendored into the 5 `stockky_ms` copies).
Compare on history, e.g. `scripts/ms_ab_report.py --policy --thresholds '{"retest_entry":0}'` against `'{"retest_entry":1}'` on >= 2 seeds, and turn the
live switch on only if the retest days do not lose to the days already passing.
`scripts/test_policy_replay.py` now fails if the live policy and the replay disagree on the new branch (3,000+ summaries x on/off).

## Tests
- `decision/tests/test_group413_retest_entry.py` (new, 10 tests, stdlib only) - run here under plain asserts: pass.
- `scripts/test_policy_replay.py` (+3 tests, all 8 pass) and `shared/market_structure/tests/run_plain.py` (126 pass) run here.
- Existing `test_group353_structure_policy.py` re-run under a small monkeypatch stand-in: all pass (`prepare_triggers` refactored into `_zone_hits`; same output).
- Not run: real pytest, the rest of the decision suite, intraday (15m/5m) structure (still not built).
