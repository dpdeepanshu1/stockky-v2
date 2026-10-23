# E54 - `gateway_support` hooks + the Buy Sniper find-buys route moved

## The problem E53 stopped at
A router file may not import `main` (circular; tests patch names on `main`), but the remaining handlers call helpers owned by `main` (`_cb_get`, `_structure_metric`, `TECHNICAL_URL`).

## The mechanism
`gateway_support.py` holds three hooks (`cb_get`, `structure_metric`, `technical_url`). `main.py` calls `gateway_support.bind(...)` once, before importing a router that needs them. The bound `cb_get` / `structure_metric` are **trampolines defined in main** that look the real function up in main's globals at call time, so `monkeypatch.setattr(main, "_cb_get", fake)` still reaches routes that now live in `gateway_routers/`. Unbound defaults fail loudly (`cb_get`) or do nothing (`structure_metric`); `bind()` rejects unknown names. Pattern for the next groups: add a hook, never import main.

## What moved
`gateway_routers/sniper.py`: `_sniper_attach_structure`, `_sniper_observe` and `find_actionable_buys` (`POST /scan/find-buys`, `/api/scan/find-buys`), code unchanged; `main` re-exports them. The router module keeps the same names inside the function bodies (`_cb_get`, `_structure_metric`, `TECHNICAL_URL`, `logger`), which are thin module-level wrappers over the hooks.
Ordered route table before vs after: 214 routes, identical. A real call to `/scan/find-buys` through the app returns 200 as before. Tests that read these functions out of `main.py` source (groups 357 and 361) now read `sniper.py`; 6 new tests in `tests/test_e54_gateway_support.py` (hook contract, patch-through, bind-before-import).

## Next groups using the same pattern
`/data-feed/batch` and four siblings (`_row_price_over_cap`), the refill routes (`_REFILL_ALL_JOB`), watchlist / searched / momentum routes (`_load_watchlist`, `_load_searched`, `_get_momentum_movers`). Each is a separate release: add the hooks it needs, move, compare the route table, run the suite.

On the VM: `cd services/api-gateway && bash run_tests.sh`, rebuild api-gateway (the image now has `gateway_support.py` at the service root), then `curl -X POST localhost:8000/scan/find-buys -H 'content-type: application/json' -d '{"stocks":[]}'`.


## Merge note (E55)
This release was merged with E55, which also created a `gateway_support/` *package*. A package and a `gateway_support.py` module cannot both exist under one name (the package wins), so the hooks described above (`bind`, `cb_get`, `structure_metric`, `technical_url`) were moved into `gateway_support/__init__.py`, beside `MainRef`. Behaviour and the import line in `main.py` / `gateway_routers/sniper.py` are unchanged. `test_unbound_defaults_fail_loudly_or_do_nothing` no longer uses `importlib.reload` (a reload would now reset the binding for later tests too).
