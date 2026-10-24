# E87 - gateway router split, batch L: the five test-pinned handlers (2026-10-23)

5 handlers (9 route registrations) moved out of `services/api-gateway/main.py` into `gateway_support/bound_routes_l.py`: `stock_chart_context` (`GET /stock/{symbol}/chart-context`), `get_stock_decision` (`GET /stock/{symbol}`), `data_feed_structure_refresh_status` (2), `data_feed_structure_refresh` (2), `data_feed_symbol` (3, the `/data-feed/{symbol}` catch-all).

Method: an AST rewrite (every global a handler reads becomes `ref.<name>`, symtable-checked: no name is both global and local in any scope, no `global` statement, decorators `@app.x` -> `@<handler>_router.x`). Each moved handler is AST-equal to the original once `ref.` is stripped. Multi-line strings keep their exact text. Routers are included where the routes were declared, so registration order is unchanged (the catch-all stays after every `/data-feed/<action>` route).

Tests that pinned main.py text were edited, not bypassed:
| test | pin | change |
|---|---|---|
| `test_e25_chart_context_route.py` | read and exec'd the handler from main.py | reads the factory, execs the handler against a fake `ref` |
| `test_group349_ms_passthrough.py` | `@app.get("/stock/{symbol}")` position and body text | compares include order, reads the factory body |
| `test_group360_structure_refresh.py` | route scan + `/data-feed/{symbol}` position | scans `gateway_support/`, compares router includes; also 2 stale pins from E78/E80 pointed at their real files |
| `test_group378_structure_catchup.py` | `"catchup": _catchup` in main.py | reads the factory |
| `test_gateway_support_split.py` | `find('@app.get("/api/feed/{symbol}"')` (was vacuous at -1) | compares against the real include |
| `test_gateway_support_split_k.py` | STAYS list | updated; new `test_gateway_support_split_l.py` |

NOT run: gateway suites that import main.py (no pytest / fastapi in the sandbox). The plain-assert tests (~730) ran under a small stand-in runner with no new failures. On the VM: `cd services/api-gateway && bash run_tests.sh`, then compare `/openapi.json` before/after.
