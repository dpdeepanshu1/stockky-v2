# E191 - gateway: E190 made the decide cache useless for feed-based calls; a real sentiment score of 0 read as missing

Includes E190 (and E189 - E184).

## What I read
Every reader of `/decide` outside the decision service: the api-gateway single-stock / scan path (`main.py`, "Decide-level cache" through the cache write),
`_normalize_decision_response`, `gateway_support/bound_routes_l.py` (Analyse: `GET /decide/{symbol}?already_owned=..&force=true`), `hotpicks_store.py`, `instant_scanner.py`.
real-trade-service does not call `/decide` at all (nothing to change there).

## Findings
1. **E190 side effect (fixed).** The gateway writes the decide cache itself (same key as the decision service), and the entries it writes come from
   `POST /decide/evaluate` with `skip_http` whenever the feed row has data - so they carry `partial_inputs`. E190 made the gateway fast path skip every such
   entry, so a feed-based call never hit its own cache and went to the decision service each time (that service's own cache still answered, but the Redis /
   KV hit was lost). The gateway now takes a partial entry when this call is itself skip_http (feed row has data, checked before the cache read) and still
   skips it when the call would fetch everything live. Owned-mode entries are still skipped (E189).
2. **Sentiment 0 (fixed).** The `/decide/evaluate` payload built from the feed row used `feed_row.get("sentiment_score") or feed_row.get("market_score")`, so a real
   0 (most negative) became `market_score` or was dropped (neutral 50 in the engine). First present value now wins (same class as E148 and E190 #2).
3. **Found, not changed: `instant_scanner.process_single_stock` posts to `{decision}/evaluate`, a route that does not exist** (the service has `/decide/evaluate`;
   the payload also has `price` but no `close` / `skip_http`). It would always 404 and fall back to the local scores. Nothing calls `process_single_stock`
   (only its tests do), so this is dead code today; fixing the URL alone would make a future caller hit live HTTP fan-out. Left for your call.

No new environment variable. Redeploy gateway and decision service together.

## Tests
- `api-gateway/tests/test_e191_gateway_partial_cache_and_sentiment_zero.py` (4 tests, source guards plus the sentiment expression); the E189 and E190 gateway source
  guards were widened to the new text. Run under real pytest: pass. The gateway suite that imports `main.py` could not run here (numpy mismatch after installing its
  requirements in the sandbox), so run it on the VM. Decision tests: 192 passed / 9 skipped, unchanged.
- `docs/ENV_REFERENCE.md` regenerated (line numbers moved); `gen_env_reference.py --check` passes.

    bash scripts/docker_test.sh api-gateway
    bash scripts/docker_test.sh decision-prediction-service
    python3 scripts/gen_env_reference.py --check
