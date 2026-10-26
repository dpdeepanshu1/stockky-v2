# E192 - the decision service's error fallback was cached and served as a real decision, and written into Hot Picks rows

Includes E191 (and E190 - E184).

## What I read
The remaining `/decide` readers E191 had not covered: `hotpicks_store.hotpicks_repair_scores`, the decision service's error fallback at the end of `_decide_impl`,
`_is_weak_decide_payload` / `_cache_set_decide` / `_cache_get_decide`, and the gateway's cache write after `POST /decide/evaluate` / `GET /decide/{symbol}`.

## Finding (fixed)
When `_decide_impl` fails it returns HTTP 200 with a neutral answer: DO NOT BUY, combined 45, technical / fundamental 50, a close if one could be found, and
`error_detail`. The service never caches it itself, but:
1. the gateway cached any non-empty body under the shared key `stockky:decide_cache:<SYM>`, and the service's weak-payload check did not recognise the fallback (score 45,
   close present, no "error processing" text), so one outage answer was served as a real decision for `DECIDE_CACHE_TTL_OPEN` (600 s), 12 h when closed;
2. `hotpicks_repair_scores` copied the fallback into stored Hot Picks rows (decision DO NOT BUY, score 45, tech 50). Those fields are non-blank, so `_row_needs_scores`
   then stopped the row from ever being repaired, although the docstring says nothing is fabricated.

Fixes: `_is_weak_decide_payload` returns True for any payload with `error_detail` (never cached, never served, whoever wrote it); the gateway does not cache a body with `error_detail`;
`hotpicks_repair_scores` skips a body with `error_detail`, or `data_insufficient` with no close. Also in that function: a dict `reasons` (what the engine sends) no longer writes
`reasons = None` into a row that had none.

Not changed: Analyse (`bound_routes_l`) still shows the fallback to the user on purpose (it labels it "Partial response - retry Analyse"). A Hot Pick row already stamped with the
fallback is not detected (no marker on it); clear it by re-running the repair after removing its scores, or wait for the next scan to rewrite the row.

No new environment variable. Redeploy gateway and decision service together.

## Tests
- `decision/tests/test_e192_error_fallback_not_cached.py` (4 tests) and 3 new tests in `api-gateway/tests/test_hotpicks_store.py` (behavioural, the suite's fake DB / HTTP).
  All pass; 2 of the 4 decision tests and all 3 hotpicks tests fail on the unchanged E191 files (the gateway source guard passes there only because it reads the new gateway file).
- `test_hotpicks_store.py` 313 passed. Decision tests (without `test_env_url_blank_fallback`, which cannot import numpy in this sandbox, same on the original) pass.
  The gateway suite that imports `main.py` could not run here; run it on the VM.
- `docs/ENV_REFERENCE.md` regenerated; `gen_env_reference.py --check` passes.

    bash scripts/docker_test.sh decision-prediction-service
    bash scripts/docker_test.sh api-gateway
    python3 scripts/gen_env_reference.py --check
