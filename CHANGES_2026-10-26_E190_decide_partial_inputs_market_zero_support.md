# E190 - decision: partial-input decisions cached for full requests, a market_score of 0 read as missing, entry levels built on a support above the price

Includes E189 (and E188 - E184).

## What I read
The three items E189 listed under "not changed": `_decide_impl` in `services/decision-prediction-service/decision/main.py` (cache read, the
`skip_http` short-circuit branches, the sentiment short-circuit, the legacy entry-level block, the response, the cache write) plus the gateway's
single-stock fast path (`api-gateway/main.py`, "Decide-level cache") that reads the same cache entry.

## Findings (fixed)
1. **A `skip_http` decision was cached as if it were a full one.** With `skip_http` the events, prediction and training pillars are skipped (neutral 50 /
   empty). The result was cached under the same symbol key, so a later full request (and the gateway fast path, which reads the entry directly) could be
   served it for `DECIDE_CACHE_TTL_OPEN` (600 s) while open, 12 h when closed. The payload now carries `partial_inputs`. `_decide_impl` serves a partial
   entry only to another `skip_http` request; a full entry still serves both. The gateway fast path skips a partial entry. An entry written before this
   change has no marker and counts as full (it ages out within the TTL).
2. **`market_score = 0` was read as missing.** In the sentiment short-circuit, `prefetched.get("market_score") or prefetched.get("sentiment_score")` treated
   a real 0 as absent: it used `sentiment_score` if there was one, otherwise 50. The first present value now wins.
3. **Entry levels on a support at or above the close.** `entry_low = support * 1.01` and `stop_loss = support * 0.98` did not check that the support is below
   the close, so a stale or wrong support put `entry_low` above `entry_high` and the stop above the price. A support that is not below the close is now
   treated like a missing one (`close * 0.95`). A valid support (below the close) gives the same levels as before. Later stages that replace the levels
   (structure policy) are unchanged.

No new environment variable. Expect somewhat more full recomputes where scans and single-stock checks used to share one entry.

## Tests
- `decision/tests/test_e190_decide_partial_inputs_market_zero_support.py` (15 tests). pytest and fastapi were installed here (pypi was reachable), so these ran under
  real pytest: 15/15 pass on the new code; 8 of the 15 fail on the unchanged E189 `main.py` (the rest are "unchanged behaviour" checks and the gateway source guard).
- The whole `decision/tests` folder: 177 passed / 9 skipped on the unchanged E189 `main.py`, 192 passed / 9 skipped after (the 15 new ones), no other change.
- `api-gateway/tests/test_e189_ultra_decide_cache_not_owned.py` still passes; the gateway edit was only compile-checked beyond that (the gateway suite was not run here).
- `docs/ENV_REFERENCE.md` was out of date after E189's gateway edit (line numbers); regenerated, `gen_env_reference.py --check` passes.

    bash scripts/docker_test.sh decision-prediction-service
    bash scripts/docker_test.sh api-gateway
    python3 scripts/gen_env_reference.py --check

## Still not done
Nothing left from E189's list. Next candidate in the same series: how real-trade-service consumes `/decide` (what it does with `partial_inputs`-style payloads and `from_cache`).
