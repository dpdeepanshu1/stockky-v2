# E84 - gateway router split, batch I: `proxy_quote` (2026-10-23)

`GET /quote/{symbol}` (`proxy_quote`) moved out of `services/api-gateway/main.py` into its own factory `gateway_support/bound_routes_i.py`, same MainRef method as E55 / E76-E83. 25 handlers remain in `main.py` (26 after E83).

## Why its own file, and the yfinance decision
E83 left this handler out because its last fallback calls `yf.Ticker`, and `tests/test_yfinance_callers_guard.py` only allows direct yfinance use in listed files. Decision taken (price source order stays Dhan -> AngelOne first):
- The handler body is **unchanged**: it asks market-data-service first (`/quote/{sym}`, which itself runs `QUOTE_PROVIDER_ORDER`, default `dhan,angelone,yfinance`). Direct yfinance runs only if market-data gives no price (non-200, exception, or 200 with price 0/None) AND `GATEWAY_DIRECT_YFINANCE_FALLBACK` is on (`_direct_yf_ok()`).
- Only `api-gateway/gateway_support/bound_routes_i.py` was added to `ALLOWED` in the guard. `bound_routes_h.py` and every other factory file stay off the list, so no other moved handler can start using yfinance unnoticed. (`scripts/price_source_allowlist.txt` is import-based and the factory has no `import yfinance`, so it needs no change; `check_price_source_imports.py` exits 0.)
- No new Dhan / AngelOne client was added to the gateway: those providers are only reached through market-data-service. Making the gateway call them directly would be a behaviour change, not part of this split.

## Checks done here
- AST of the moved function equals the E83 original once `ref.` is removed and `proxy_quote_router` is read as `app` (True).
- Route registration sequence (147 decorators / includes) identical to E83 with the one include substituted in place; the include sits right after the batch H `GET /` router and before the E55 circuits router, as before.
- New `tests/test_gateway_support_split_i.py` (6 tests; 5 pass here, the live-app one skips without fastapi). It fails on a copy where the yfinance call is changed or an `import yfinance` is added.
- `test_yfinance_callers_guard.py` and the E55 / E83 split guards pass under a stand-in runner. `main.py` and `gateway_support/*.py` compile.
- The existing 11 `proxy_quote` tests in `tests/test_main_ops_routes.py` patch `gw.httpx.get`, `gw.resolve_ns_ticker` and `gw.yf.Ticker`; the handler reads all three through `ref` at call time, so the patches still reach it. They were NOT run (no pytest / fastapi in the sandbox).

## On the VM
`cd services/api-gateway && bash run_tests.sh`; rebuild; compare `/openapi.json` before and after (`GET /quote/{symbol}` should be unchanged); try `GET /quote/TCS` (source should be the market-data provider, e.g. dhan) and, with market-data stopped, check the 502 / yfinance fallback. Roll back = redeploy the E83 zip.

## Still in `main.py` (25)
5 test-pinned handlers (`stock_chart_context`, `get_stock_decision`, `data_feed_structure_refresh`, `data_feed_structure_refresh_status`, `data_feed_symbol`), 3 framework hooks (middleware, exception handler, websocket), and handlers with nested functions or `global`. These need hand moves (and their pinning tests adjusted), so they were not done here.
