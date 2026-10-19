# group 466 (runner/backtest): 6 h limit for heavy tests, Resume, and the Telegram "URLError" fix

(The frontend also uses the label "group 466" for the unrelated close-all-and-disarm feature. This file is only about calibration-runner-service and the backtest scripts.)

## What went wrong

**A. A/B replay killed at 2 h.** Run `20261019-192955-ab_report-b799` (seed 7, 355 symbols, `--jobs 2`) printed `progress 290/355 symbols` and then
`step result: timeout (time limit)` at 7205 s. Heavy jobs had a 2 h (some 3 h) limit and a killed run kept nothing, so the whole replay had to start again.

**B. Telegram "URLError" on the daily close.** The 16:05 pack ended while the notification service was restarting / starved of CPU. The runner gave up after its
retries and the page showed `Telegram error: URLError`. The message was lost and did not say which step had failed.

## What changed

| Area | Change |
|---|---|
| Time limit | Heavy jobs (`calibration_full, ab_report, structure_backtest, legacy_gate, entry_rules, p8_retest, ml_v2, tsfm_probe, kronos_backtest`) get **6 h**. `RUNNER_HEAVY_TIMEOUT_S` (seconds) changes it; never below 1 h; an unreadable value falls back to 6 h. `fetch_history`, `intraday_gates`, `tsfm_nightly` stay at 1 h, short jobs unchanged. The timeout line in the run output names the variable. |
| Checkpoint / resume | `scripts/_progress.run_jobs(..., ckpt=, resume=, stamp=)` saves every finished symbol (atomic pickle). `--checkpoint DIR` / `--resume` in `ms_ab_report, p8_retest_evidence, legacy_gate_backtest, entry_rules_backtest, ml_v2_walkforward`. Without `--resume` the folder is emptied first. A changed history file or setting recomputes only that symbol; a half-written file is recomputed; results stay in job order. `vm_calibrate.sh` passes `--checkpoint $OUT_DIR/ckpt/ab_<seed>` (+ `--resume` when `RESUME=1`). |
| Resume button | Catalog gets `resumable` (true for `calibration_full, ab_report, legacy_gate, entry_rules, p8_retest, ml_v2`). **Runs and downloads** and the run window show **Resume** on a stopped (timeout / failed / cancelled / attention) run of such a test or pack. It starts the same test with `resume=true`; the market-hours confirm step still applies. The output starts with `# RESUME: ...`. |
| Process group | The child runs in its own session; timeout, cancel and service shutdown signal the whole group (SIGTERM, then SIGKILL), so pool workers no longer outlive the job. |
| Telegram outbox | An undelivered message (connection error, timeout, 5xx) is kept in `<data>/outbox/ob_*.json` and retried every `RUNNER_OUTBOX_RETRY_S` (default 60 s) until it is sent, refused for good (4xx / no channel), or older than `RUNNER_OUTBOX_MAX_AGE_S` (default 24 h). It is sent as `(delayed) ...`. The run's Telegram note says `- will be sent again when the notification service is reachable`. A failed Telegram never changes the run's status. |
| What failed | A failed / timed-out / cancelled run's message has a `What failed:` list (step title and detail). Successful steps are left out. |
| Preflight | An **automatic** run waits up to `RUNNER_PREFLIGHT_S` (default 60 s) for `/health` of gateway, real-trade, position-stocks, market-data, decision-prediction and notification, then runs anyway. Manual runs never wait. |

## Settings (docker-compose, runner service)

```
RUNNER_HEAVY_TIMEOUT_S=21600     # heavy jobs, seconds (min 3600)
RUNNER_OUTBOX_RETRY_S=60         # outbox retry interval (min 10)
RUNNER_OUTBOX_MAX_AGE_S=86400    # drop an undelivered message after this (min 600)
RUNNER_PREFLIGHT_S=60            # automatic runs wait this long for the stack (0 = off)
```

## How to continue the run that timed out

Open it in **Runs and downloads** and press **Resume**. This only works for a run started after this version is deployed (older runs saved no
checkpoint, so Resume would recompute everything; the output then says `resume asked but nothing saved yet`).

## Roll back

Set `RUNNER_HEAVY_TIMEOUT_S=7200` and `RUNNER_PREFLIGHT_S=0`; checkpoints are only extra files in `calibration_out/ckpt/` and can be deleted. The outbox
folder `/data/runner/outbox` can be emptied at any time.

## Tests

`scripts/test_resume_checkpoint.py` (7), `services/calibration-runner-service/tests/test_group466_timeout_outbox.py` (12), `frontend/src/calibration.test.mjs`
(Resume plumbing). The existing runner tests now pass `preflight_s=0` because their fake service URLs never answer `/health`.
