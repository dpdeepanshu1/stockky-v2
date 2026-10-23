# E53 - gateway router split, group 4: Buy Sniper edge report + Surprise static peek

Three handlers moved from `api-gateway/main.py` to `gateway_routers/scan_extras.py`, code unchanged:
`find_buys_edge` (`GET /scan/find-buys/edge`), `find_buys_edge_evaluate` (`POST .../edge/evaluate`) and `api_surprise_static` (`GET /surprise/static`), each with its `/api/...` alias.
Ordered route table before vs after: 214 routes, identical. `tests/test_group361_sniper_observation.py` (route wiring) now also reads the router file.

## Why the split stops being mechanical here
I checked every handler that is left (about 120). The ones that remain call helpers that live in `main.py` and are shared with other code or patched by tests, for example:
- `find_actionable_buys` -> `_sniper_attach_structure` -> `_cb_get`, `TECHNICAL_URL`, `ms_passthrough`, `_structure_metric` (tests `test_group357_*` load the helper out of `main.py` source);
- `/data-feed/batch` and four other routes share `_row_price_over_cap`; the refill routes share `_REFILL_ALL_JOB`; watchlist/searched/momentum routes share `_load_watchlist`, `_load_searched`, `_get_momentum_movers` with the scan universe code.
A router file may not import `main` (circular, and tests patch names on `main`). The next step is therefore a design change, not another move: a `gateway_support/` module that owns `_cb_get`, the service URLs, `metrics` and `_structure_metric`, with `main` re-exporting them. Do that as its own release with its own deploy-and-watch cycle.

On the VM: `cd services/api-gateway && bash run_tests.sh`, rebuild api-gateway, `curl localhost:8000/scan/find-buys/edge`.
