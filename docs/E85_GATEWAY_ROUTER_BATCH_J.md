# E85 - gateway router split, batch J: handlers with nested functions (2026-10-23)

12 handlers (17 route registrations) moved out of `services/api-gateway/main.py` into `gateway_support/bound_routes_j.py`, same MainRef method as E55 / E76-E84. 13 handlers remain in `main.py` (25 after E84). These are the first handlers that contain nested functions, lambdas and comprehensions, which the earlier batches had to skip.

Moved (bodies unchanged except `name` -> `ref.name` where the name is a main.py global): `ops_wake_db_all` (GET+POST `/ops/wake-db-all`), `system_health`, `start_scan` (`POST /scan/start`), `stream_market_scan` (`/api/scan/stream`, `/scan/stream`), `scan_watchlist`, `send_picks_to_telegram` (admin-guarded), `api_surprise_scan_stream` (2 routes), `api_surprise_notify_top_picks`, `catalyst_alert_scan` (GET+POST), `stockky_hot_premarket`, `stockky_hot_run`, `api_hotpicks_notify_top_picks`.

## How nested functions were handled
A scope-aware rewrite: a name that is a parameter / local of the handler or of any nested function, lambda or comprehension is left alone; only names that resolve to main.py's module globals become `ref.<name>` (read at call time, so `monkeypatch.setattr(gw, "name", fake)` still reaches them). Multi-line string literals (docstrings, messages) are not re-indented, so their text is byte-identical. Checks:
- AST of each moved function (decorators, signature, docstring, body, nested defs) equals the E84 original once `ref.` is removed and `<handler>_router` is read as `app`: 12/12. (The first attempt re-indented multi-line strings and this check caught it; fixed before the zip.)
- `symtable` over each moved handler: no global is read except `ref`, a builtin or its own router; the set of names rewritten equals the set of globals the original read.
- Route table: statically derived registration sequence identical to E84 (147 route decorators/includes, same order). The factory is built once, before the first include (line ~5051); `BackgroundTasks`, `Depends`, `_require_admin`, `Query` (the only names decorators / defaults / annotations evaluate at build time) are bound at lines 53-54.
- Admin guard: `send_picks_to_telegram` keeps `dependencies=[ref.Depends(ref._require_admin)]`.
- Regression sweep: every test function / `Test*` method that can run without fixtures or fastapi (about 560) ran on the E84 tree and on this tree: no result changed. The sweep found two handlers that must stay (below) before the zip was built.
- New `tests/test_gateway_support_split_j.py` (8 tests; 7 pass here, the live-app one skips). It fails on a copy where a main name is read without `ref.`.

## Test changes (2, both to follow the move, nothing weakened)
- `test_gateway_support_split_g.py::test_the_one_guarded_route_left_in_main_is_still_guarded`: its comment said the route stays until a hand-written move. It now asserts the `_require_admin` guard exists in exactly one place, main.py or the new factory.
- `test_group303_loop_offload_ipo_filter.py::test_no_direct_store_calls_remain_in_async_functions`: scans `gateway_support/bound_routes*.py` as well as main.py, so the moved async handlers stay covered (same as `test_group387` did in E82).

## Left in main.py on purpose (13)
- `market_trending`: its nested helper calls `yf`; `tests/test_yfinance_callers_guard.py` allows direct yfinance only in listed files. Not allowlisted here. A separate decision: switching that helper to market-data (Dhan -> AngelOne) would remove the yfinance use and let it move.
- `start_bulk_feed`: its docstring mentions `yf.download`, which the guard matches as text.
- `data_feed_refill_additional`: `test_refill_additional.py` pins its `from refill_additional import ...` in main.py.
- `data_feed_run`: `test_env_flag_sweep.py` pins `DATA_FEED_SKIP_FUNDAMENTALS_AFTER_BULK` to main.py.
- `api_surprise_premarket_proxy`: uses `global` (writes a main.py variable); needs a hand move.
- 5 handlers pinned by tests: `stock_chart_context`, `get_stock_decision`, `data_feed_structure_refresh`, `data_feed_structure_refresh_status`, `data_feed_symbol`.
- 3 framework hooks: middleware `add_cors_header`, `universal_exception_handler`, websocket `websocket_endpoint`.

## NOT run
pytest / fastapi are not installable in the sandbox, so the gateway suites (including `test_main_scan_runner.py`, `test_main_stock_scan_routes.py`, `test_main_surprise_routes.py`, `test_main_hot_premarket.py`, `test_main_hot_run_tail.py`, `test_main_catalyst_ws_quotes.py`, `test_main_ops_alerts_hardreset.py`, `test_main_notification_training_proxy.py`) and the live-app tests did not run.

## On the VM
`cd services/api-gateway && bash run_tests.sh`; rebuild; compare `/openapi.json` before and after (query params of `/scan/start`, `/api/scan/stream`, `/catalysts/alert`, `/stockky-hot/run`, `/surprise/notify-top-picks`, `/stockky-hot/notify-top-picks`; the admin guard on `/notifications/send-picks` must still answer 401/403 without the key). Try `GET /system/health`, `GET /scan/watchlist`, `POST /scan/start`, `GET /api/scan/stream` (the stream must still emit lines) and `POST /stockky-hot/run`. Roll back = redeploy the E84 zip.
