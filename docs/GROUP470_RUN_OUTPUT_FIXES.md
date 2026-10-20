# Group 470: fixes found by reading the 19-20 Oct calibration-runner outputs

Nothing here changes what a trading gate decides. Every change is in the runner, the report scripts or their tests.

## What the saved outputs showed, and what was done
| Finding in the outputs | Cause in the code | Fix |
|---|---|---|
| `calibration_full` output held a full admin JWT after `--token` (and in the `set -x` trace line) | `Redactor` only knew the runner's own token, `SESSION_SECRET` and `Bearer x`; `vm_calibrate.sh` passes its own token as `--token VALUE` | `runner.py`: also redacts any JWT, `--token VALUE` / `--token=VALUE`, and `TOKEN=` / `PS_TOKEN=` values |
| `gate_check` step 2 and `calibration_pack` failed with `unrecognized arguments` (exit 2) | the job gave `--ab a b c`; both scripts declare `--ab` as append (one flag per file) | new argv token `globflag:--ab:<pattern>` expands to `--ab f1 --ab f2`; both jobs use it. No match is still "input missing" |
| `calibration_snapshot`, `ab_report`, `legacy_gate`, `entry_rules`, `ml_v2`, `p8_retest` said `input missing` for a folder they create themselves (`snapshots/`, `ckpt/<name>_<seed>`) | `--store` and `--checkpoint` were not in `OUT_FLAGS`, so they were checked as inputs | both added to `OUT_FLAGS`. The history folder (`hist_<seed>`) is still checked as a real input |
| `monday_check` 10:15 FAIL when run at 16:11 | `check_session` ignored the clock | outside Mon-Fri 09:15-15:30 IST it returns INFO "not checked", never FAIL |
| `tsfm_nightly` 754/754 failed every day (240 s) and `tsfm_probe` made hundreds of failing calls | the worker is in compose profile `timesfm`, not started, so its name does not resolve | both scripts probe `/health` first; a NAME that does not resolve = "SKIPPED" and exit 0 with how to start it. Any other failure (refused, timeout, HTTP error) keeps its old failure path. `--require-service` turns the skip into a failure |
| G1 said "future data / split" for a latency-only FAIL | one fixed footer | footer now says which check failed; latency is described as a speed finding |
| G1 budget was 15 ms in `vm_calibrate.sh` / the script and 50 ms in the runner job | two defaults | one default, 15 ms (the script's documented value), overridable by `G1_BUDGET_MS` or `--budget-ms` / the job's Budget field. **This makes the monthly G1 job stricter than before: the 20 Oct p95 of 57 ms was WARN at 50 and is FAIL at 15.** Set `G1_BUDGET_MS=50` if 50 was intended |
| entry slippage line read "paid above the signal in total: -195.63 rupees" | sign convention not stated | now "net rupees against the signal: -195.63 (+ paid above it, - paid below it, i.e. a gain)" |

## Tests
`services/calibration-runner-service/tests/test_group470_fixes.py` (redaction, `--ab` repetition, missing-input rules), `scripts/test_group470_tooling.py`
(TimesFM skip, G1 footer, slippage wording), `scripts/test_vm_ops.py` (session check closed / open). `test_runner_engine.py` now also checks the flag inside a `globflag:` token against the script's `--help`.

## Not changed (your decision, see REMAINING_TASKS.md)
General late-entry cutoff, structure gate mode while G2 / G3 fail, the 15:00 square-off misses, gap-down stop overshoot.
