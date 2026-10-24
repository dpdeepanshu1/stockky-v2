# E88 - gateway router split, batch M: the three framework hooks (2026-10-23)

`main.py` now declares no `@app.<...>` function. Moved to `gateway_support/bound_routes_m.py`:
- `add_cors_header` (was `@app.middleware("http")`): main.py registers it with `app.add_middleware(BaseHTTPMiddleware, dispatch=add_cors_header)`, which is exactly what that decorator does, at the same place (after the `CORSMiddleware` setup, which stays in main.py - it is configuration, not a handler).
- `universal_exception_handler` (was `@app.exception_handler(Exception)`): `app.add_exception_handler(Exception, ...)`, same place.
- `websocket_endpoint` (`/ws`): its own router, included where the route was.

Bodies AST-equal to the originals once `ref.` is stripped; same symtable proof as E87. The factory is built at the CORS block (earlier than batches A-L) - `WebSocket` and `_cors_policy` are bound above it; `test_gateway_support_split_m.py` checks that and the middleware order (CORS config, wildcard stamp, error handler).

Risk to confirm on the VM: the live app registers one `/ws` route, the exception handler is the moved one, and the wildcard CORS headers still appear on a normal response and on a 500. `test_cors_policy.py`, `test_main_core.py` and `test_main_ws_loops_startup.py` call these through `gw.<name>` and need fastapi.

## Docker `pip install ./shared` - deliberately NOT changed
DEPLOY_GUIDE.md deploys each service on Render with "Root Directory: services/<name>", so the build context is the service folder. A root-context Dockerfile (`COPY shared`) would fail there until each Render service's root directory and Dockerfile path are changed in the dashboard, and the per-service `stockky_ms/` copies that the tests import would have to go too. The vendored copies are kept in step by `scripts/sync_market_structure.py` and checked by `scripts/check_ms_package.py` in CI, so there is no drift to fix. Revisit only if you move off Render root directories (the calibration-runner image already uses a repo-root context, because it runs on the VM via compose).
