# Group 409 - replay the decision policy's BUY NOW rules (A/B `--policy`)

Why: `scripts/ms_ab_report.py` replays only the six GATE rules (chase / stretch / room / rr / stop_wide / bearish). The decision policy
(`decision/structure_policy.py`) has its own, stricter BUY NOW rules that never went through any backtest: `P7` (>= 3 positive groups, location >= 55),
`P8` (bullish BOS, not failed, <= 10 bars old) and, since group 407, `P5_BREAK` and `P4_REWARD`. Changing them (the P8 retest branch, the group
independence count) was held back because nothing could say whether such a change helps. This makes them testable.

* `shared/market_structure/backtest.py`: `policy_flags(summary, th)` + `POLICY_RULES` (p4_reward, p5_break, p7_groups, p7_location, p8_failed_bos,
  p8_no_bos, p8_old_bos). Same meaning as the policy; unlike the gate rules a MISSING metric trips them (policy: unknown is never bullish). Evaluated at the
  structure close (no live-price shift). Vendored into the five services (`sync_market_structure.py`).
* `scripts/ms_ab_report.py --policy`: candidate = gate AND policy rules; the per-rule table gains the seven rows. Run it next to a plain run on the SAME
  history folder: the difference in edge / kept % is what the policy adds; the **drop-one edge of `p8_old_bos`** is the evidence for or against the 10-bar
  age limit (positive = the limit earns its place; near zero or negative = a retest branch is worth building). `p7_groups` / `p7_location` show how much
  of the baseline the independence count removes.
* `scripts/test_policy_replay.py`: the replay and the live policy are compared over >5000 summaries; if either changes, CI fails.

```
python3 scripts/ms_ab_report.py calibration_out/hist_7 --out calibration_out/ab_policy_7 --step 6 --horizon 10 --jobs 4 --policy
```
The gate's room threshold is 0.5 ATR and the policy's is 0.8; pass `--thresholds '{"room_atr":0.8}'` to compare like with like.
Same caveats as every A/B here: technical-only baseline, thresholds untuned, one seed is not a verdict.
