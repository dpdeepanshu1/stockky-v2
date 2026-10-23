# E55 - gateway_support/: moving handlers that use main.py helpers

## The problem E53 left
Every handler still in `api-gateway/main.py` uses something defined there (`_cb_get`, `all_snapshots`, `qstash_client`, `_durable_cache_configured`...), and tests replace those names on the module (`monkeypatch.setattr(gw, "name", fake)`). A router file cannot import `main`, and a copy of the helper in another module would not see the patch. E53 proposed a module that *owns* the helpers with `main` re-exporting them; that has the same flaw (a patch on `main.<name>` would not reach the moved handler).

## What this release does instead
A moved handler keeps no copy of any helper. It reads the helper from the live `main` module **at call time** through a `MainRef`:

- `gateway_support/__init__.py`: `MainRef(globals())` - `ref.name` is `globals()["name"]` when the handler runs, so a patch on `main.name` is seen exactly as before; a missing name raises `AttributeError` naming it.
- `gateway_support/bound_routes.py`: `build_bound_routes(ref)` builds the handlers and one router per route.
- `main.py` calls the factory once with its own `globals()` (so a test that loads `main.py` under another name gets its own handlers; nothing is shared between copies), re-exports each handler (`ready = _bound_routes.ready`, so `gw.ready()` and `monkeypatch.setattr(gw, "ready", ...)` still work), and includes each router at the place the route used to be.

Moved (bodies unchanged except `name` -> `ref.name`): `circuits_status` (`/circuits`), `ops_rate_limits` (3 paths), `metrics_endpoint` (`/metrics`), `ops_qstash_publish` (`/ops/qstash/publish`), `ready` (`/ready`), `audit_missing_feed_data` (3 paths; still registered before `/api/feed/{symbol}`). 10 route decorators, 6 routers.

## Checked here
- Route table: the ordered list of 164 route decorators (154 still in `main.py` + the 10 moved ones, expanded in place at their include) is identical before and after; `include_router` calls 20 -> 26.
- Behaviour: the original six handlers (from the E53 `main.py`) and the moved ones were run side by side under the same fakes, 30 scenarios (JSON/prom metrics, every QStash branch incl. bad JSON / bad delay / disabled / None client, ready true/false, audit hit/miss/no-cache, circuits gauge, and the same patches applied after the factory ran): no difference.
- `tests/test_gateway_support_split.py`: no factory module imports `main`; handlers touch main names only as `ref.<name>` and every `ref.<name>` exists in `main.py`; decorators match; `main.py` re-exports, includes in order and no longer declares the routes; `MainRef` unit tests; a live-app test (runs where fastapi is installed).
- NOT run here (no pytest / fastapi in the sandbox): the 110 existing gateway test files and the live-app test. Run them on the VM.

## What is left
109 route handlers remain in `main.py`. Of those, 1 uses a `global` statement, 7 are named in tests that read `main.py` source, 3 take a model class defined in `main.py` as a parameter, and 18 contain nested functions (a mechanical `name` -> `ref.name` rewrite must not touch a local that shadows a global). 98 have none of the first three. They can now move in batches of 5-10 per release, each checked the same way (route-order diff + side-by-side run + the VM suite).

On the VM: `cd services/api-gateway && bash run_tests.sh`, rebuild api-gateway, then `curl localhost:8000/ready`, `/circuits`, `/metrics`, `/ops/rate-limits`, `/data-feed/audit-missing?limit=5`. Roll back = redeploy the E54 zip.
