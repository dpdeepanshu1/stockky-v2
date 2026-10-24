# E77 - gateway router split, batch C (2026-10-23)

11 more handlers (20 routes) moved out of `services/api-gateway/main.py` into `gateway_support/bound_routes_c.py`, with the same MainRef method as E55 and E76: a moved handler reads every main.py helper as `ref.<name>` at call time, so tests that patch `main.<name>` still reach it. 87 handlers remain in `main.py` (98 before this batch).

Moved (bodies unchanged except `name` -> `ref.name`): `stock_structure`, `cancel_scan`, `scan_stop_all`, `get_scan_universe`, `get_universe`, `api_surprise_audit`, `api_surprise_scan`, `api_evaluate_price_alerts`, `repair_single_stock`, `repair_batch_missing`, `data_feed_stop`. Each has its own router, included in `main.py` at the place the route used to be (the factory is built once, right after the batch B include), and `main.py` keeps `<handler> = _bound_routes_c.<handler>` so `gw.<handler>()` and `monkeypatch.setattr(gw, "<handler>", ...)` still work.

`get_universe` has a local variable `root` that has the same name as a main.py global; it is a plain local in the original (assigned before use), so it is left as `root`, not `ref.root`.

## One existing test had to change
`tests/test_group349_ms_passthrough.py::test_main_wiring_present` read main.py for `"/stock/{symbol}/structure"`. The route now lives in `bound_routes_c.py`, so the test checks the factory source for the path, that main.py no longer declares it, and that main.py still includes `stock_structure_router` before `@app.get("/stock/{symbol}")` (same ordering rule as before). No other source-reading test names a moved handler or path (checked by scanning every test that reads main.py).

## Checked here
- Route table: the ordered list of 139 route registrations (method, decorator, handler) is identical before and after, with each moved router expanded at its include site.
- Bodies: every moved body (and signature) is identical to the original once `ref.` is removed; docstrings compared after `inspect.cleandoc` (the factory indents them one level, which FastAPI's OpenAPI description already normalises). No triple-quoted string other than a docstring is inside a moved body, so re-indenting changes no string value.
- Names: the 41 `ref.<name>` names all exist in main.py; no free names in the factory beyond builtins, `APIRouter`, `SimpleNamespace`.
- Behaviour: original (exec'd from the E76 main.py source) and moved handlers run side by side under fakes: 11 handlers x their argument sets x 50 world variants (Redis up / empty / bad data / write failing, kv caches warm / stale / cold, build ok / failing / slower than its deadline, upstream up / down / bad JSON / 500, surprise scan ok / slow / failing / import failing, alerts triggered / none / failing, feed job running / idle / stopped, checkpoint commit failing...), each called twice with a main.py helper and logger patched between the calls (a patch after startup is seen). 1,550 comparisons, 0 differences, identical over three repeated runs. Exceptions are compared by type and args (comparing the objects themselves is what produced a false "DIFF" in the unfinished first attempt at this batch). A deliberately broken copy of the factory is caught by the same harness.
- `tests/test_gateway_support_split_c.py` (new: decorators, own router, re-export, include order, live-app test) and the existing guard rules in `test_gateway_support_split.py` (which cover every module in `gateway_support/`) pass in a stand-in runner; `py_compile` ok.

## NOT run here
pytest / fastapi / httpx are not installable in the sandbox: the existing gateway test files and the live-app tests did not run. In particular `tests/test_main_scan_stream_control.py`, `test_main_market_universe_routes.py`, `test_main_surprise_routes.py`, `test_main_repair_routes_stop_resume.py`, `test_main_ops_alerts_hardreset.py` and `test_main_core.py` call these handlers as `gw.<name>` and are the ones most worth watching. On the VM: `cd services/api-gateway && bash run_tests.sh`, rebuild api-gateway, then `curl` `/stock/RELIANCE/structure`, `/scan/universe?cached=true`, `/universe`, `/surprise/audit`, `/surprise/scan?cached=true`, `POST /scan/stop-all`, `POST /data-feed/stop`. Roll back = redeploy the E76 zip.

## Next
The other 5 handlers that had no blocker but a main.py name in the signature, then the ones that need a hand-written move (nested functions, `global`, model classes in the signature). Test each batch on the VM before the next.
