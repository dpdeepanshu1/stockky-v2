# group491 - an interrupted walk-forward no longer empties backtest.json

## What the 22 Oct results showed
`calibration_full` (21 Oct 19:20) was cut off after 10 minutes, at 15 of 355 symbols of the walk-forward, when the calibration-runner
container was restarted (the runner marks runs active at a restart as interrupted). Its walk-forward step is `structure_backtest.py ... > backtest.json`,
and the shell empties the target file when the command STARTS. The good `backtest.json` from the 20 Oct walk-forward was therefore replaced
by an empty file, and `calibration_pack` had nothing to read (group 490 made that a clear "needs data first" instead of a traceback).

## Change
- `scripts/vm_calibrate.sh`: the walk-forward writes `backtest.json.part` and moves it over `backtest.json` only when it exits 0; on failure the `.part`
  is removed and the previous file stays.
- `services/calibration-runner-service/runner.py`: a job with `out_file` (only the walk-forward job) writes `<out_file>.part` and replaces the file
  only when the command finished (exit code it counts as ok / attention, no timeout, no cancel; empty output keeps an existing file).
  The run folder still gets a copy of what the run produced, as before.
- Tests: 3 in `tests/test_runner_engine.py`.

## To get the pack result
Run `calibration_full` again in a quiet window (about 2 hours for the walk-forward at ~20 s per symbol; seeds already complete are kept), or the
single `Structure rules walk-forward` job, then `calibration_pack`.
