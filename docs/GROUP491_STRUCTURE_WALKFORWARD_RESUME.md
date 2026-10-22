# Group 491 - the walk-forward can resume (structure_backtest.py)

## Why
`structure_backtest.py` (job **Structure rules walk-forward**, step of `calibration_full`) takes about 2 hours for 400 symbols and ran in one
process with no save point, so any restart, timeout or Ctrl-C started it again from symbol 1. Group 466 gave the A/B replay, legacy gates,
entry rules, P8 and ML V2 a per-symbol checkpoint; this was the one slow job left without it. A killed walk-forward also left an empty
`backtest.json`, which is what made `calibration_pack` fail (group 490).

## What changed
- `scripts/structure_backtest.py`: the per-symbol work is now `_job` (module level), run through the same `_progress.run_jobs` as the other backtests.
  - `--checkpoint FOLDER`: every finished symbol is saved there as it completes (atomic write). Emptied at the start unless `--resume`.
  - `--resume`: reuse saved symbols, compute only the missing ones. A symbol is recomputed when its history file changed (size / mtime) or
    `--step`, `--entry`, `--cost-pct` or `--slip-pct` changed. `--horizon`, `--min-n`, `--thresholds` do NOT invalidate it (they only affect the
    cheap walk-forward / calibration at the end, which always re-run on the saved samples).
  - `--jobs N` (default 1 = the old single process): parallel workers.
  - Output JSON is unchanged. Progress line is now `[structure] n/total symbols elapsed ... about ... min left`.
  - `load()` still exists (tests use it); skipped files (unreadable, under 100 bars) are still not counted.
- `calibration-runner-service/jobs.py`: `structure_backtest` is `resumable` (the page offers Resume after a timeout), gets `--checkpoint <cal>/ckpt/structure_<seed>`,
  `--resume` on a Resume run, and the "Parallel workers" option (default 2, like the other backtests).
- `scripts/vm_calibrate.sh`: the walk-forward step passes `--checkpoint $OUT_DIR/ckpt/structure_<seed>`, `--resume` when `RESUME=1`, and `--jobs $JOBS`.

## Tests
- New `scripts/test_structure_backtest_resume.py` (7): fresh run saves, resume computes only the missing, no-resume empties, horizon/thresholds reuse vs
  step/cost/slip/entry recompute, changed file recomputed, skipped files, no usable symbol exits 1, pool == single process.
- `test_group466_timeout_outbox.py` now expects `structure_backtest` to be resumable with the checkpoint argv (it asserted the opposite before).
- Ran here (plain Python): new file 7/7, `test_ms_ab_report.py` 18/18, `test_resume_checkpoint.py`, `bash -n` and a `DRY_RUN=1 RESUME=1` pass of `vm_calibrate.sh`.
  NOT run here: pytest suites (no pytest in this sandbox) - run `calibration-runner-service` tests on the VM / CI.

## Use
After a timeout: in Runs & downloads press **Resume** on the Structure rules walk-forward run (or `RESUME=1 scripts/vm_calibrate.sh`).
By hand: `python3 scripts/structure_backtest.py DIR --checkpoint ckpt/structure_7 --resume > backtest.json`.
