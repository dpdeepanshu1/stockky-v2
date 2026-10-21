# group 485 - real-trade "api-gateway probe failed again" Telegram flood

## What the alert actually was
"api-gateway probe failed again (58 total consecutive failures)" does NOT come from a gateway health check. It comes from
real-trade-service's own circuit breaker (`resilience/circuit_breaker.py`, `api_gateway_breaker`), which wraps two gateway
calls: `GET /stockky-hot` and `GET /surprise/ipo/list` (watchlist Tier 1, and the after-hours bulk-deal fetch).
On 2026-10-21 the gateway's `/health` answered 200 (services_health 16:03 IST) while those heavier calls kept failing.

## Defect fixed
After the breaker opened, every failed half-open probe (one per 120 s) sent a Telegram message: 58 messages in one outage.
Also the message never said what failed.

## Change (real-trade-service only; no trading logic touched)
- First failed probe still alerts at once; repeats are throttled to one per `RT_BREAKER_PROBE_ALERT_REPEAT_S` (default 1800 s;
  `0` = the old every-probe behaviour). The DOWN and "recovered" alerts are unchanged. Recovery resets the throttle.
- DOWN / probe alerts and `/resilience/status` now carry `last_error` (e.g. `RuntimeError: /stockky-hot ReadTimeout`), so the next
  alert says which endpoint failed and how.
- Tests: `tests/test_rt_circuit_breaker.py` 26 pass (5 new). `docs/ENV_REFERENCE.md` regenerated (was already out of date).

## Not changed / still open
- Root cause of the gateway calls failing is not provable from the uploaded packs (no real-trade container log). The next alert,
  or `docker compose logs real-trade-service | grep "circuit_breaker\[api-gateway\]"`, will show the error text.
