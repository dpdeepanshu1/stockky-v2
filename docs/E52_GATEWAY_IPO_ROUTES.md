# E52 - gateway router split, group 3: the IPO routes

Six handlers (and the `IpoAddRequest` model) moved from `api-gateway/main.py` to `gateway_routers/ipo_routes.py`, code unchanged (`main.py` 14,593 -> 14,434 lines):
`api_ipo_scan` (`POST/GET /ipo/scan`), `api_ipo_list`, `api_ipo_audit`, `api_ipo_add`, `api_ipo_repair_batch`, `api_ipo_purge_non_equity` - each with its `/surprise/ipo/...` alias.
They only needed standard imports plus the gateway logger, so the router file defines `logger = logging.getLogger("api-gateway")` (same logger name, same log lines). Handlers and `IpoAddRequest` are re-exported from `main`; each router is included where its routes used to sit.

Proof: ordered route table (method, path, handler, guards) before vs after: 214 routes, identical. Whole api-gateway suite passes file by file (0 failures); `tests/test_gateway_routers_split.py` now also accepts a module-level `logger` and the new router names.

Next candidates (each needs its helper moved with it, so one group per release): `/data-feed/batch` + `_row_price_over_cap`, `/api/scan/find-buys*` + `_sniper_*`, `/api/surprise/static|repair-batch` (needs `MARKET_DATA_URL`).

On the VM: `cd services/api-gateway && bash run_tests.sh`, `docker compose up -d --build api-gateway`, then `curl localhost:8000/ipo/list`.
