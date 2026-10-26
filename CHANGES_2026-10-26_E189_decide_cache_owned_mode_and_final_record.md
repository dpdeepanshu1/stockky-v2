# E189 - decision: a held stock's SELL/HOLD was served to candidate scans from the decide cache, and final BUYs from the multi-horizon scorer were never recorded for training

Includes E188 (and E187 - E184).

## What I read
`services/decision-prediction-service/decision/main.py::_decide_impl` from the top to the error fallback (cache read, pillar short-circuits, technical /
fundamental fallbacks, `_combined_score`, `_decide`, data-quality gate, catalyst floor, levels, response, multi-horizon override, post-horizon gate,
structure policy, training record, observation hook, cache write), plus `_decide`, `_cache_get_decide` / `_cache_set_decide` / `_is_weak_decide_payload`,
`_queue_training_record`, `_has_usable_prefetched`, the `/decide` and `/decide/evaluate` routes, `/decide/batch`, and the two gateway readers of the same cache.

## Findings (fixed)
1. **The decide cache ignored `already_owned`.** It is keyed by symbol alone, but the mode changes the answer: owned -> SELL below 35 and HOLD 35-60, no
   data-quality gate, no P9 upgrade; not owned -> BUY NOW / PREPARE TO BUY / DO NOT BUY. A decision computed for a held stock (an Analyse of a position)
   was served to the next candidate scan of that symbol, and a not-owned BUY NOW to the next owned-stock check, for `DECIDE_CACHE_TTL_OPEN` (600 s) while
   open and 12 h when closed. The payload now carries `already_owned` and `_decide_impl` serves a cached entry only in the same mode (an entry written before
   this change has no marker and counts as "not owned", so the first owned request after the deploy recomputes). The gateway's single-stock fast path
   (`api-gateway/main.py`, "Decide-level cache") reads the same entry and always asks as "not owned"; it now skips an owned-mode entry. One slot per symbol, so
   alternating modes recompute (as they should) instead of returning the wrong answer.
2. **The training record skipped final BUYs the legacy scorer had not flagged.** E168-era group368 moved the record to the FINAL decision (multi-horizon is the primary
   decision, then the quality gate and structure policy), but its arguments were still only built when the legacy `_decide()` result was BUY NOW / PREPARE TO BUY.
   A final BUY that came from the multi-horizon scorer while the legacy scorer said DO NOT BUY / WAIT was never queued, so the closed loop (`live_win_rate`, its n, the
   classifier) never saw those picks, and a thin n keeps data quality "provisional", which blocks BUY NOW. The arguments are now always built when there is a close;
   `_queue_training_record` still records only a final BUY NOW / PREPARE TO BUY (and a structure-demoted pick only with STRUCTURE_RECORD_DEMOTED=1).
   `DECIDE_RECORD_ON_FINAL_DECISION=0` restores the old gate. Expect more training rows from now on.

## Read and found fine (no change)
- Order of the stages; the multi-horizon override followed by a second data-quality gate; the structure policy as the last step with a recorded original decision; the error
  fallback is not cached; weak payloads are never cached.
- Not changed (needs your call): a decision computed from a `skip_http` payload (events, prediction and training pillars skipped) is cached under the same key and can be served
  to a later full request within the TTL; the legacy entry levels use `support * 1.01` / `support * 0.98` without checking that support is below the close;
  `market_score = 0` in a payload that also has `sentiment_score` is read as missing (the `or` chain) and becomes 50.

## Tests
- `decision/tests/test_e189_decide_cache_owned_and_final_record.py` (12 tests, runs `_decide_impl` on a `skip_http` payload) and
  `api-gateway/tests/test_e189_ultra_decide_cache_not_owned.py` (1 source-level guard).
- pytest is not installed in the sandbox: run through a stand-in runner with stub modules for fastapi / pydantic / httpx. 12/12 pass on the changed code; 9 of the 12 fail on the unchanged E188
  `main.py` (the 3 that pass there are the "unchanged behaviour" checks). Existing group353 / 368 / 374 / 422 / 434 and e148 decision tests give identical results before and after
  (group353's wiring test needs `TestClient` and was not run). The gateway guard was run directly, not through pytest.

    bash scripts/docker_test.sh decision-prediction-service
    bash scripts/docker_test.sh api-gateway
    python3 scripts/gen_env_reference.py --check
