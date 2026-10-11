# Group 318 - `@app.on_event` replaced by a lifespan in all seven FastAPI apps

Behaviour is meant to be unchanged; this removes the FastAPI deprecation (on_event is slated for removal) before an upgrade turns it into a boot failure.

## What changed
Each app (api-gateway, market-data, real-trade, decision `decision/main.py`, training `training/app.py`, notification, scheduler) gets, just above `app = FastAPI(...)`:
`_STARTUP_HOOKS`, `_SHUTDOWN_HOOKS`, decorators `@_on_startup` / `@_on_shutdown`, and `_lifespan`, which `app` now receives as `lifespan=`.
26 handlers converted (api-gateway 6+2, market-data 8, real-trade 2+2, decision 1+1, training 1, notification 1, scheduler 1).
What on_event guaranteed is kept: handlers run in declaration order; an async handler is awaited, a sync one is called inline; a failing startup handler stops boot; shutdown handlers run after the app stops serving.
The decorated function is returned unchanged, so tests that call a handler directly still work. position-stocks already used a lifespan and is untouched.

## Tests
- api-gateway `test_lifecycle_hooks_are_registered_on_the_app` now reads the new hook lists (the old one inspected `router.on_startup`); new test checks sync/async order: startup hooks, serving, shutdown hooks.
- Full suites after the change: api-gateway 8607, market-data 1598, real-trade 4202 (+1 skipped), notification-scheduler 183, decision-prediction 50 + 62 + 42. All passed.
- `docs/ENV_REFERENCE.md` regenerated (line numbers moved); shared-copy and yfinance checks still pass.

## Not verified
A real boot of each container (uvicorn start-up with the live env) - run `docker compose up` once and confirm each service logs its usual startup lines.
