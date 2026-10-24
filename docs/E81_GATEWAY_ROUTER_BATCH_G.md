# E81 - gateway router split, batch G (2026-10-23)

11 handlers (14 route registrations) - the admin-guarded `dependencies=[Depends(_require_admin)]` routes - moved out of `services/api-gateway/main.py` into `gateway_support/bound_routes_g.py`, same MainRef method as E55 / E76-E80. 37 handlers remain in `main.py`.

This finishes the work the previous session (E80 -> E81) started and did not complete: it had selected these handlers and extended the mover for decorator / signature names, but never wrote the move, the guard-test rewrite or the zip.

Moved (bodies unchanged except `name` -> `ref.name`): `set_notification_config`, `delete_notification_channel`, `notifications_call_me` (GET + POST, one `api_route`), `test_notification_channels`, `trigger_training`, `ops_circuit_reset` (POST guarded, GET open as before), `ops_overnight_set_config`, `ops_overnight_run`, `ops_power_off`, `ops_resume_activity`, `hard_reset_database` (3 routes).

## What is different from batches B-F
- Decorators and signatures now use main.py names, so they are written `dependencies=[ref.Depends(ref._require_admin)]`, `update: ref.NotificationChannelUpdate`, `request: ref.Request`, `ref.Optional[bool]`, `background_tasks: ref.BackgroundTasks`. They are evaluated when the factory runs, exactly as the original decorators were when main.py loaded. `bound_routes_g.py` therefore has NO `from __future__ import annotations` (as batch D). Every such name is bound in main.py before the factory is built (line ~5365): checked.
- `tests/test_admin_guard.py::test_main_protects_exactly_the_documented_routes` scanned `main.py` decorators for `_require_admin`; it now scans main.py plus `gateway_support/bound_routes*.py` (decorators on `<handler>_router`). It still must equal `admin_guard.PROTECTED` (14 paths: checked statically, 14 == 14). `test_every_protected_route_is_registered_on_the_app` is unchanged and still checks the live routes.
- New `tests/test_gateway_support_split_g.py`: decorators, guard on every route (GET `/ops/circuit-reset` deliberately unguarded), router order, no `__future__` import, annotations, `send_picks_to_telegram` still guarded in main.py, plus a live-app test (needs fastapi) that checks each route registered once with/without `require_admin` in its dependencies.

## Left out on purpose
- `send_picks_to_telegram` (`/notifications/send-picks`, guarded): it has a nested function. Stays in main.py; `test_admin_guard` still sees its decorator there.
- `data_feed_structure_refresh_status` and `data_feed_symbol`: still pinned by `test_group360_structure_refresh.py` (needs a test rewrite first).

## Checked here
- Route table: statically derived registration sequence (method, path, kwargs incl. `dependencies`, decorators applied bottom-up, routers expanded at their include sites) is identical to E80: 163 route registrations + 21 other `include_router` calls, same order. The comparator fails when an include is removed (tested).
- Bodies: AST of each moved function (decorators, signature, docstring, body) equals the E80 original once `ref.` is removed and `<handler>_router` is read as `app`. 11/11.
- Name completeness (`symtable`): in every moved handler each free/global name is `ref` or a builtin; decorator / default / annotation names are `ref`, a router or a builtin. Fails on 5 deliberately broken copies (bare `_require_admin` in a decorator, bare `NotificationChannelUpdate`, bare `NOTIFICATION_URL`, changed `ref.logger.warning`, changed `timeout=70`) and on a renamed route path.
- Factory executes: run against a stub `fastapi.APIRouter` and a fake `ref`, it builds without error and registers the 14 routes in the same bottom-up order as the original decorators; `ops_resume_activity` and `ops_circuit_reset` ran through MainRef.
- Guard tests (stand-in runner, no pytest): `test_gateway_support_split*.py` 25 passed, 7 skipped (live-app tests, need fastapi). The new test fails when a guard is removed from a decorator and when `from __future__` is added (tested). `py_compile` ok.
- Handler counts: the E80 doc said 49 remained; the same count (top-level functions with an `@app.<method>` decorator) on the E80 main.py gives 48, so that figure was one too high. Now 37.

## NOT run here
pytest / fastapi / httpx / pyjwt are not installable in the sandbox, so the gateway suites and the live-app tests did not run - including `test_admin_guard.py` (needs `jwt`, `fastapi`), `test_main_ops_alerts_hardreset.py` and `test_main_notification_training_proxy.py`, which exercise these handlers. On the VM:
1. `cd services/api-gateway && bash run_tests.sh`
2. Rebuild api-gateway; compare `/openapi.json` before and after (the `update` body schema of `POST /notifications/config`, the `enabled` / `datafeed_time` / `premarket_time` / `rest_between_steps_sec` / `phase` / `preserve_days` / `message` query params, and the `security`-less `dependencies` on the 14 routes).
3. Without a token: `POST /ops/power-off` and `POST /notifications/test` must return 401 (or 503 when no secret is configured); with the admin token they must work; `GET /ops/circuit-reset` must still answer without a token.

Roll back = redeploy the E80 zip.

## Follow-up done (E82, same zip)
`tests/test_group387_no_loop_db_calls.py` now also scans every `gateway_support/bound_routes*.py` (moved async handlers calling `ref.<db helper>()` on the loop are flagged, same taint rule as main.py). Passes on the current code (no offenders); fails when a `ref.hotpicks_db_upsert(1)` is planted in an async handler (tested). Run by stand-in runner only.

## Next
37 remain: `send_picks_to_telegram` + about 17 other nested-function handlers, the two held back above, and handlers that take a pydantic model (`WatchlistUpdate`...). All need hand-written moves or test rewrites.
