# E76 - gateway router split, batch B (2026-10-23)

11 more handlers moved out of `services/api-gateway/main.py` into `gateway_support/bound_routes_b.py`, with the same MainRef method as E55 (docs/E55_GATEWAY_SUPPORT.md): a moved handler reads every main.py helper as `ref.<name>` at call time, so tests that patch `main.<name>` (`monkeypatch.setattr(gw, "datetime", ...)`) still reach it. 98 handlers remain in `main.py` (was 109).

Moved (bodies unchanged except `name` -> `ref.name`): `ops_neon_keepalive`, `market_session`, `notifications_health`, `get_notification_config`, `training_status`, `get_training_score`, `ops_overnight_status`, `ops_overnight_get_config`, `ops_activity_status`, `repair_all_status`, `repair_all_stop` (17 route decorators). Each has its own router, included in `main.py` at the place the route used to be, and `main.py` keeps `<handler> = _bound_routes_b.<handler>` so `gw.<handler>()` and `monkeypatch.setattr(gw, "<handler>", ...)` still work.

How they were chosen: only handlers with plain `@app.<verb>("literal")` decorators, no `global`, no nested function / lambda, no main.py name in the signature, no local that shadows a main.py global, and not named in any test that reads `main.py` source. 27 of the 103 route handlers qualified; these 11 are the simplest.

## Checked here
- Route table: the ordered list of 153 route registrations (method, path, handler) is identical before and after, with each moved router expanded at its include site.
- Bodies: every moved body is identical to the original once `ref.` is removed; decorators identical apart from the router name.
- Names: the only free names in the factory are builtins, `APIRouter`, `SimpleNamespace`; all 16 `ref.<name>` names exist in `main.py`; no moved handler is still declared there; the factory is built once, before the first include.
- Behaviour: original and moved handlers were run side by side under fakes (httpx up / down / bad JSON, training client ok / 404 / down, 4 market phases, refill job running / idle): 1,056 comparisons, 0 differences; a patch applied after the factory ran is seen.
- `tests/test_gateway_support_split_b.py` (new, source checks + a live-app test) and the rules in `test_gateway_support_split.py` (which now also cover the new module) pass in a stand-in runner; `py_compile` ok.

## NOT run here
pytest / fastapi / httpx are not installable in the sandbox: the 110 existing gateway test files and the live-app tests did not run. On the VM: `cd services/api-gateway && bash run_tests.sh`, rebuild api-gateway, then `curl /market/session`, `/ops/activity`, `/ops/neon-keepalive`, `/notifications/health`, `/training/status`, `/api/feed/repair-all/status`. Roll back = redeploy the E75 zip.

## Next
Batch C: the next 16 qualifying handlers (e.g. `cancel_scan`, `scan_stop_all`, `stock_structure`, `api_surprise_audit`, `repair_single_stock`, `api_evaluate_price_alerts`). Handlers with nested functions, a `global`, a model class in the signature or a main.py-source test need a hand-written move and stay for later.
