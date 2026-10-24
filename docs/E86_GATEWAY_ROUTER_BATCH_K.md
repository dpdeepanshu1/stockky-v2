# E86 - gateway router split, batch K: the five handlers E83-E85 had to leave (2026-10-23)

5 handlers (12 route registrations) moved out of `services/api-gateway/main.py` into `gateway_support/bound_routes_k.py`: `market_trending`, `api_surprise_premarket_proxy` (GET+POST `/surprise/premarket`), `start_bulk_feed` (3 routes), `data_feed_refill_additional` (2), `data_feed_run` (2). 8 handlers remain in `main.py` (13 after E85). Each had been held back because a test or a guard pinned it; each pin was resolved rather than bypassed.

| handler | why it stayed | what was done |
|---|---|---|
| `market_trending` | nested helper called `yf.Ticker`; the yfinance guard allows it only in listed files | the direct-yfinance last resort is now the module-level `_trending_yf_row(sym)` in main.py (already allowed there). Same order as before: market-data `/quotes/bulk` (Dhan -> AngelOne -> yfinance) first, direct yfinance only when it has no price and `GATEWAY_DIRECT_YFINANCE_FALLBACK` is on. Returns `None` where the handler used to `continue`. Exercised in isolation: fallback off / unresolvable / empty history -> None, normal bar -> (103.46, 3.46, 3.46), Open 0 -> change_pct None |
| `start_bulk_feed` | docstring said `yf.download`; the guard matches text | docstring reworded ("chunked bulk download (run_bulk)"). Visible only as the route description in `/openapi.json` |
| `data_feed_refill_additional` | `test_refill_additional.py` pinned its `from refill_additional import ...` in main.py | that test now reads main.py and `gateway_support/bound_routes*.py` |
| `data_feed_run` | `test_env_flag_sweep.py` pinned `DATA_FEED_SKIP_FUNDAMENTALS_AFTER_BULK` to main.py | the pin now also reads the factories (`ref.os.` normalised to `os.`). The sweep's default-on-flag scan also recognises `ref.os.getenv` / `ref.os.environ.get`, so moved code (E85 too) stays scanned |
| `api_surprise_premarket_proxy` | `global _SURPRISE_SCHEMA_CONFIRMED` | the flag is defined in main.py (`= False`) and read/written as `ref._SURPRISE_SCHEMA_CONFIRMED` (MainRef writes the module global). The first-use probe now catches `AttributeError` (MainRef's error for an undefined main.py global) instead of `NameError`; the existing test that deletes the flag still covers it |

## Checks done here
- AST of 4 of the 5 moved functions equals the E85 original once `ref.` is removed and `<handler>_router` is read as `app`. The fifth (`api_surprise_premarket_proxy`) differs by exactly two lines, the removed `global` statement and `NameError` -> `AttributeError` (diff checked).
- `symtable`: no moved handler reads a main.py global without `ref.`. Route registration order identical to E85 (143 entries). `build_bound_routes_k` is built once before the first include (line ~5946); `BackgroundTasks` and `Request` are bound well above it.
- Before/after sweep of every test that can run without fixtures or fastapi (about 625): no result changed except the intended ones (E85's "left in main" test moved into the new K test; 8 new K tests, 7 pass, live-app one skips). The 4 pinned tests that were edited were also run by hand: `test_known_flags_use_the_strip_first_form` for both main.py flags, the default-on-flag offender scan, `TestDrift.test_names_main_imports_from_this_module_exist`.
- `tests/test_yfinance_callers_guard.py` and `scripts/check_price_source_imports.py`: no new allowlist entry; `bound_routes_k.py` contains no yfinance text.

## NOT run
pytest / fastapi are not installable in the sandbox: the gateway suites did not run, notably `test_main_market_universe_routes.py` (`market_trending`, 9 tests), `test_main_ipo_premarket_routes.py` (premarket schema gate), `test_main_feed_control_audit.py` (bulk feed / refill), `test_main_data_feed_run.py`, and the live-app tests.

## On the VM
`cd services/api-gateway && bash run_tests.sh`; rebuild; compare `/openapi.json` (the only expected diff is the `start_bulk_feed` description text); try `GET /market/trending`, `POST /surprise/premarket?background=false` twice (second answer should say `cached`), `POST /data-feed/start-bulk-feed`, `POST /data-feed/refill-additional`, `POST /data-feed/run`. Roll back = redeploy the E85 zip.

## Still in `main.py` (8)
5 handlers whose tests read their source (`stock_chart_context`, `get_stock_decision`, `data_feed_structure_refresh`, `data_feed_structure_refresh_status`, `data_feed_symbol`) and 3 framework hooks (`add_cors_header` middleware, `universal_exception_handler`, `websocket_endpoint`). Moving the five means rewriting the tests that pin them to follow the code; the hooks are registered on `app` directly (not through a router), so a move would change how they are attached.
