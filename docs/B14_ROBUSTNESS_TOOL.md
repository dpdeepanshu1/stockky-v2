# B14: robustness tool (offline, never touches live trading)

`python3 scripts/robustness.py HIST_DIR [--perms 1000] [--sweep-pct 20] [--position-pct 10] [--horizon 10] [--sample 400 --seed 7] [--jobs N]`

Replays the history exactly like `ms_ab_report.py` (same rows, costs, next-open entry) and tests the structure gate three ways. Output: `HIST_DIR/robust_out/robustness.{json,md}`.

1. **Random-entry benchmark.** Same number of entries as the gate keeps, drawn at random from the baseline days with the same count per month (so a lucky market phase cannot fake an edge). `p_random` = share of draws that did as well as the gate.
2. **Trade-order shuffle.** The gate's net returns compounded at `--position-pct` of equity per trade, real order vs shuffled orders. Final equity is order-free; the drawdown is not. Plan for the 95th-percentile drawdown.
3. **Parameter sweep.** chase, stretch, room, R:R and bearish-BOS-bars nudged -/+20 % one at a time, then all tighter and all looser. `stable` only if every variant keeps at least half the base edge, stays positive and keeps 15+ signals.

Verdict: `insufficient_sample` (under 15 candidate or baseline signals, the B15 floor), `robust` (p_random < 0.05 AND sweep stable AND candidate mean net > 0), else `not_robust`. `robust` is necessary, not sufficient.

Limits: the baseline filter (stretch 0 to 3 ATR) is fixed in the replay, so stretch cannot be loosened past 3.0 in the sweep; trades are treated as sequential in the shuffle (max 5000, seeded sample); the DB-backed rules (52-week, Kronos, TimesFM) are not covered because the history replay does not contain them.

Code change outside the new files: `ms_ab_report._rows` keeps the nine rule inputs on each row (`row["s"]`) only when `th["_keep_summary"]` is set; the A/B report is unchanged without it.
