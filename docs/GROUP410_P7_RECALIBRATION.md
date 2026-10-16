# Group 410 - P7 recalibration (location block off, groups_min 3 -> 2)

## Evidence (VM run, 51 symbols, step 6, T+10 net of costs, `calibration_out/hist_7`)
* `--policy` with live defaults: 7 of 4274 replayed days survive (0.2%). `p7_location` rejects 97.5%, `p7_groups` 93.0%. Edge -1.28 pp, 90% CI [-8.2, 6.6], `insufficient_sample`.
* P7 switched off (`groups_min:0, loc_min:0`): 71 survivors (1.7%), edge +0.354 pp, CI [-1.63, 2.44], `insufficient_sample`. `p5_break` / `p4_reward` barely matter (1.6% / 3.4%).
* Calibration (`backtest.json`): `ms_loc_score` is NOT monotonic (spread -1.65 pp; the lowest bin 0-2 has the best mean, +1.78%; the 37-70 bin, where 55 sits, is
  +0.13%, below the all-days mean). `ms_tqs` is not monotonic either. So the 55 cut-off has no support.
* Live: 1 observed day, 0 BUY NOW passed - no information yet.

## Change
* `decision/structure_policy.py`: `loc_min` 55 -> **0** (P7_LOCATION off), `groups_min` 3 -> **2**. A value of 0 switches that rule off (a missing value no longer trips a rule that is off).
  Everything else (P2-P6, P8, P9, unknown-is-never-bullish for groups) is unchanged.
* `shared/market_structure/backtest.py` (`policy_flags`, re-vendored into the five services): same defaults and same "0 = off", so `ms_ab_report.py --policy` replays the live rule set.
* `scripts/structure_backtest.py`: the calibration block now also bins `ms_groups`, so the next run answers whether the group count ranks outcomes.
* Tests: `test_group353_structure_policy.py` (new defaults, env restore), `scripts/test_policy_replay.py` (live/replay parity incl. groups = 2, zero = off).

## What is and is not proven
* `loc_min = 0` is supported: the score did not rank outcomes. `groups_min = 2` is a JUDGEMENT CALL: the runs give no groups-by-return table (the solo edge of `p7_groups` is only +0.10 pp, noise level), and
  3 left almost nothing. Treat it as provisional until the `ms_groups` calibration is read.
* `p8_old_bos` drop-one edge was +0.071 pp on 71 signals: too small and too noisy to justify building a retest branch. The 10-bar limit is kept. `p8_no_bos` / `p8_failed_bos` drop-one edges were negative
  (-0.53 / -0.30) but also noise at n=71; unchanged.
* Nothing here is a verdict: every A/B above is `insufficient_sample`.

## Roll back / tune without a deploy
`STRUCTURE_POLICY_PARAMS_JSON='{"loc_min":55,"groups_min":3}'` restores the old behaviour. `STRUCTURE_POLICY_MODE=off` is still the kill switch.

## Re-check on the VM
```
python3 scripts/ms_ab_report.py calibration_out/hist_7 --out calibration_out/ab_policy_410 --step 6 --horizon 10 --jobs 4 --thresholds '{"room_atr":0.8}' --policy | head -45
python3 scripts/structure_backtest.py   # same args as before; then print calibration -> ms_groups bins
```
