# E48 - gateway router split, first small group (2026-10-23)

`services/api-gateway/main.py` is ~14.7 k lines with 205 routes. This is the first step of splitting it, kept deliberately tiny so it can be proven safe: **3 routes (6 paths) moved, code unchanged**.

## What moved
`GET/POST /api/price-alerts` (+ `/price-alerts`) and `DELETE /api/price-alerts/{alert_id}` (+ `/price-alerts/{alert_id}`) -> `services/api-gateway/gateway_routers/price_alerts.py`. `POST .../evaluate` stays in `main.py`.

## Why this group first
A static scan of all 205 routes (which module-level names of `main.py` each handler uses) found it among the few with **zero** `main.py` helper dependencies. Its tests patch `data_feed.<name>` (imported inside the handler, so any module sees the patch) and call `gw.api_*` directly (re-exported). It is not in `admin_guard.PROTECTED`, so no guard moves.

## Rules every later group must follow (written in `gateway_routers/__init__.py`)
1. Handlers use no helper defined in `main.py`; 2. anything from another module is imported inside the handler; 3. `main.py` re-exports the handler names; 4. the router is included at the same place the routes were, so registration order is unchanged.
`tests/test_gateway_routers_split.py` enforces 1-4 from the source (no fastapi needed) and, when fastapi is present, that each moved path is registered exactly once.

## Next groups (one per release)
Routes with no `main.py` helper dependency (from the scan): `/circuits`, `/metrics`, `/ops/loop-lag`, `/ops/db-latency`, `/ops/circuit-status`, `/ops/rate-limits`, `/ipo/status|stop|audit|list`, `/surprise/stop`, `/data-feed/refill-additional/status`, `/stockky-hot/audit|table`. Routes that DO use helpers (most of the file) need those helpers moved to their own module first, with `main.<helper>` aliases kept so existing patches keep working; do those one helper at a time.

## Check on the VM
`cd services/api-gateway && bash run_tests.sh` (the whole gateway suite; `test_main_ops_alerts_hardreset.py` covers the 3 routes), rebuild api-gateway, then `curl <gateway>/price-alerts` -> `{"ok":true,...}`. Not run here: pytest / fastapi are not installed (the 3 source-level guard tests and `py_compile` were).
