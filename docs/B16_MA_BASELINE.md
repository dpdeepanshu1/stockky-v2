# B16: MA-crossover dumb baseline (offline, never touches live trading)

`python3 scripts/ma_baseline.py HIST_DIR [--fast 20] [--slow 50] [--horizon 10] [--boot 500] [--jobs N] [--sample 400 --seed 7]`

Buys the next open after SMA(fast) closes above SMA(slow), holds T+h, same costs and slippage as `ms_ab_report.py`. Compared on the same history with the A/B baseline and the gate's candidate days; the edge of the gate over the dumb baseline gets a 90 % cluster bootstrap (symbol x month).

Verdict: `insufficient_sample` (under 15 candidate or crossover entries, the B15 floor) / `beats_dumb` (CI lower bound > 0 and candidate mean net > 0) / `worse_than_dumb` / `no_better`. A rule that is `no_better` has not earned its complexity.

Limits: unpaired comparison (the groups trade different days); only the structure gate is covered; the crossover has no stop or target, only the fixed T+h exit. Output: `HIST_DIR/ma_out/ma_baseline.{json,md}`. No existing file's behaviour changed (only the CI file list).
