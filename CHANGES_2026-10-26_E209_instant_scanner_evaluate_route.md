# E209 - instant_scanner posts to a route that exists, without live fan-out

**Where:** api-gateway/instant_scanner.py (`_decision_evaluate_url`, `process_single_stock` payload). Open item from E191.
**Bug:** it posted to `<DECISION_URL>/evaluate`; the decision app's route is `/decide/evaluate` (mounted under `/decision`), so every call would 404 and fall back to local scores. The payload also lacked `close` and `skip_http`.
**Fix:** URL is `.../decision/decide/evaluate` (also accepts a bare root, `.../decide`, or the full path). Payload adds `close` and `skip_http: true` so the call scores strictly off the payload (missing pillars neutral) instead of fanning out to live news/event/prediction fetches inside its 3 s budget - the same flag the gateway scan path sets.
**Still true:** nothing calls `process_single_stock` outside tests (dead code today). Its engine result is returned as the raw decision JSON, not passed through main.py's `_normalize_decision_response`; if you wire it in, check that shape.
**Tests:** tests/test_e209_instant_scanner_evaluate_route_exists.py (6, includes a check that the route exists in decision/main.py); test_instant_scanner.py URL/payload expectations updated. NOT RUN; py_compile only.
