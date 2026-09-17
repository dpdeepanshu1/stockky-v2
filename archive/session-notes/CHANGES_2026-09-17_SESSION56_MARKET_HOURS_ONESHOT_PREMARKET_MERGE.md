# Session 56 (2026-09-17) — market-hours workflows migrated as ONE-SHOT premarket steps

## Request, in two parts
1. First pass: migrate the 3 remaining market-hours GitHub Actions
   workflows (surprise-scanner, catalyst-alert, evaluate-outcomes) into
   in-process scheduling, same pattern as the 2026-09-11
   `overnight_orchestrator.py` migration. `retrain-model.yml` explicitly
   OUT of scope (commits the retrained model to git, can't safely do that
   from a running container) — left untouched throughout.
2. **Correction (same session)**: the first pass ran these 3 jobs
   repeatedly THROUGHOUT market hours (hourly / 4x-day / 2x-day), same
   cadence as the original workflows. User clarified that was wrong:
   real-trade-service's and position-stocks-service's auto_pilot loops
   are already the only things that should run during live market hours
   — nothing else should add load or any chance of interference while
   they're live. The actual ask is simpler: get everything (data feed,
   repairs, surprise scan, catalyst alert, T+1/T+5 evaluation) fresh and
   healthy ONCE before market open, then leave the system alone for the
   rest of the day.

## What's actually shipped (final state)
- **Deleted** `scheduler/market_hours_jobs.py` (the hourly/multi-times-a-day
  version from the first pass) — removed entirely, along with its
  `/market-jobs/*` routes and its own startup scheduling loop in
  `scheduler/main.py`.
- **Folded the 3 jobs into `overnight_orchestrator.py`'s existing
  "premarket" phase** (already runs once/day, default 07:00 IST) as 3 new
  one-shot steps appended after the existing Hot Picks/Surprise/IPO
  premarket+repair steps:
  - `_run_surprise_scan_step()` — GET api-gateway `/api/surprise/scan`
  - `_run_catalyst_alert_step()` — POST api-gateway `/catalysts/alert`,
    polled via its own `/catalysts/alert/status`
  - `_run_evaluate_outcomes_step()` — POST decision-prediction-service
    `/training/api/evaluate/t1` then `/t5` (sync=true server-side, so no
    polling needed) — decision-prediction-service is a different
    container from api-gateway, so `_post`/`_get` in
    `overnight_orchestrator.py` were extended with an optional `base=`
    param (defaults to `API_GATEWAY_URL`, evaluate_outcomes passes
    `DECISION_PREDICTION_URL` instead).
  Each new step still gets the same `_rest()` pause between it and the
  next, matching every other step in this phase.
- **`docker-compose.yml`**: kept `DECISION_PREDICTION_URL` on
  notification-scheduler-service (still needed, just consumed by
  `overnight_orchestrator.py` now instead of the deleted module); updated
  its comment accordingly.
- The 3 original workflow files (`surprise-scanner.yml`, `catalyst-alert.yml`,
  `evaluate-outcomes.yml`) stay archived in `archive/removed-workflows/`
  from the first pass — no change needed there, they're genuinely retired
  either way. `retrain-model.yml` is still untouched in `.github/workflows/`.

## End result
One daily premarket run (default 07:00 IST, configurable via the existing
`/overnight/config`) does: Hot Picks feed+repair → Surprise premarket+repair
→ IPO scan+repair → Surprise scan (one-shot) → Catalyst alert (one-shot) →
Evaluate T+1/T+5 (one-shot) — then nothing else runs until tomorrow's
premarket. Zero new background activity during market hours.

## Verification (sandbox — no live Oracle/GitHub access)
- `py_compile` clean on `overnight_orchestrator.py` and `main.py`.
- Real import of `main.py`'s FastAPI app confirms all `/market-jobs/*`
  routes are gone and `/overnight/*` routes are unchanged.
- Functional test with mocked HTTP: ran `_run_premarket_phase()` end to
  end and captured every call in order — confirmed the 3 new steps fire
  exactly once each, last, after all the pre-existing steps, and that
  `evaluate_outcomes`'s 2 calls (t1, t5) correctly hit
  `decision-prediction-service`, not `api-gateway`.
- `docker-compose.yml` re-parsed with PyYAML — still valid.

## NOT done / not verified
- Not live-tested against the real Oracle deployment or a real premarket
  window. Recommend watching `/overnight/status` through one real
  premarket run (`POST /overnight/run?phase=premarket` for a manual
  trigger) to confirm all 6 steps report `done` before relying on it.
- `retrain-model.yml` and anything that reads/writes its model file
  remain untouched, as instructed.
