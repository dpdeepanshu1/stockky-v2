# GROUP368 - training record uses the final decision; classic /analyze baseline (items 7, 8)

## Item 8 - decision-prediction-service/decision/main.py
- The pre-policy `background_tasks.add_task(record_prediction_for_training, ...)` is replaced by `_train_args = dict(...)` (initialised to `None`).
- New helpers above `_assess_data_quality`: `_structure_snapshot(response)` and `_queue_training_record(background_tasks, args, response)`.
- The record is queued right before the decide-cache write, i.e. AFTER multi-horizon, the data-quality re-gate and the structure policy, with the FINAL
  decision / confidence / entry range / target / stop and a compact `structure` block in `feature_snapshot`.
- Only a final BUY NOW / PREPARE TO BUY is recorded. A pick demoted to WAIT by the structure policy is recorded only with `STRUCTURE_RECORD_DEMOTED=1`.
- Failure to record never changes a decision (try/except, warning only). `args` is never mutated.
- Test: `decision/tests/test_group368_training_record_final_decision.py` (7 tests; helpers extracted with `ast`, fastapi not needed).

## Item 7 - classic /analyze baseline (technical service)
- `scripts/gen_technical_baseline.py` (check by default, `--update` to rewrite) -> `services/analysis-intelligence-service/tests/golden/technical_baseline.json` (35 cases).
- `services/analysis-intelligence-service/tests/_analyze_harness.py`: offline loader (stubs fastapi/httpx only when absent; patches history, cache, quote).
- `tests/test_group368_analyze_baseline.py` (7 tests): legacy fields == golden; structure on/off identical legacy fields; only the three structure keys are added;
  lite never sends the heavy block; overrides bypass the shared cache; thin-history paths carry no structure keys.
- Verified: injecting `score += 1` into the "Above 20-day SMA" branch makes the generator print DRIFT; restored and clean.
- `scripts/gen_ms_golden.py` (legacy Market-Board golden) still passes (41 series).

## Not done in this package
- `GROUP339_BASELINE.md` (item 7 leftover) - not written.
- Items 9-11: the zip already has the real-trade-service observation wiring (model, config, boot loop, route). The position-stocks `ScalpStructureObservation`
  and api-gateway `sniper_observation.py` modules exist but are not hooked into any route/loop; the handoff log contains no code for that, so it was left alone.
- Local runs used a pytest stand-in (no pytest/fastapi/PyPI in the sandbox); run the suites once in your CI.
