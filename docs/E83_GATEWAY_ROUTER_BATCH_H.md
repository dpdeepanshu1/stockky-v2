# E83 - gateway router split, batch H (2026-10-23)

11 handlers (12 route registrations) moved out of `services/api-gateway/main.py` into `gateway_support/bound_routes_h.py`, same MainRef method as E55 / E76-E81. 26 handlers remain in `main.py` (37 before; same counting rule as E81).

Moved (bodies unchanged except `name` -> `ref.name`): `root` (`GET /`), `ops_rate_limits_event`, `ops_check_alert`, `ops_refresh_static_params`, `market_history`, `ops_db_status`, `set_watchlist`, `add_to_watchlist`, `run_scan_post` (`POST /scan`), `training_other_proxy` (`/training/{path:path}`, all 6 methods), `repair_all_missing` (2 routes). These are the handlers that were left once nested functions, `global`, pinned-by-test and special decorators were excluded; their signatures use `WatchlistUpdate`, `Request`, `BackgroundTasks`, `Optional` (so, as in G, no `from __future__ import annotations`).

## What is different from earlier batches
- Most of these handlers sit ABOVE the old build line (`root` is at line ~4916, the earlier factories are built at ~5017 / ~5362), so the factory is built right before the first include of this batch (with its own `from gateway_support import MainRef as _MainRef`). `WatchlistUpdate` (class, line ~4901) and the imported framework names are bound before that point: checked statically (`test_every_name_the_decorators_and_signatures_need_is_bound_in_main_before_the_factory_is_built`).
- `training_other_proxy` is a catch-all (`/training/{path:path}`): its router is included at the same place as before, so it still registers after `/training/status`, `/training/train` etc.

## Left out on purpose
- `proxy_quote` (`GET /quote/{symbol}`): its body calls `yf.Ticker` directly, and `tests/test_yfinance_callers_guard.py` allows direct yfinance use only in listed files. Moving it would either fail that guard or require adding `gateway_support/bound_routes_h.py` to the allowlist, which would widen it for the whole factory file. Not done without your decision.
- `stock_chart_context` (pinned by `test_e25_chart_context_route.py`), `get_stock_decision`, `data_feed_structure_refresh` / `_status`, `data_feed_symbol` (pinned by `test_group360_structure_refresh.py`), the middleware / exception handler / websocket, and every handler with nested functions or `global`.

## Checked here
- Route table: statically derived registration sequence identical to E82 (163 route registrations + 21 other `include_router` calls, same order).
- Bodies: AST of each moved function (decorators, signature, docstring, body) equals the E82 original once `ref.` is removed and `<handler>_router` is read as `app`. 11/11. Name completeness (`symtable`): every free/global name in a handler is `ref` or a builtin.
- Regression sweep: every test function / `Test*` method that can run without fixtures or fastapi (about 520) was run under a stand-in runner on E82 and on this tree: no result changed except the 5 new tests of `test_gateway_support_split_h.py` (4 pass, the live-app one skips). The sweep is what found the `proxy_quote` / yfinance guard problem above (first attempt included it and one test went from pass to fail).
- Mutation: removing `ref.` from `WatchlistUpdate` in a signature fails 3 tests (`test_gateway_support_split.py` and two in the new file).

## NOT run here
pytest / fastapi are not installable in the sandbox, so the gateway suites (including `test_main_core.py`, `test_main_ops_alerts_hardreset.py`, `test_main_universe.py` and the watchlist / scan tests that exercise these handlers) and the live-app tests did not run. On the VM: `cd services/api-gateway && bash run_tests.sh`; rebuild; compare `/openapi.json` before and after (the `WatchlistUpdate` body schema of `POST /watchlist` and `/watchlist/add`, query params of `POST /scan`, `/api/feed/repair-all`, `/ops/refresh-static-params`, `/market/history/{symbol}`); try `GET /`, `GET /ops/db-status`, `POST /scan`, `GET /training/status`. Roll back = redeploy the E82 zip.

## Next
26 remain: `proxy_quote` (needs a decision on the yfinance allowlist), the 5 pinned ones, 3 framework hooks (middleware, exception handler, websocket) and about 17 handlers with nested functions (one of them with `global`). Those need hand-written moves.
