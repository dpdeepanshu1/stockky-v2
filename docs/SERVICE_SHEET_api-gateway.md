# Service sheet - api-gateway (E170)

Measured read-only in the E170 sandbox with `python3 scripts/audit_service_sheet.py api-gateway` plus the repo scanners; nothing was run against a broker or a database. The ten checks are the same for every sheet (see `docs/SERVICE_SHEETS_INDEX.md`).

| # | Check | Result | Verdict |
| --- | --- | --- | --- |
| 1 | Inventory | 92 files, 39,233 lines, 1,284 functions; routes 100 GET + 105 mutating; 127 test files | note |
| 2 | Function size | 67 over 100 lines, 90 with branch count over 25. Longest: `gateway_support/bound_routes_j.py:10 build_bound_routes_j` (1935 lines, cc 406); `gateway_support/bound_routes_k.py:10 build_bound_routes_k` (845 lines, cc 162); `gateway_support/bound_routes_f.py:11 build_bound_routes_f` (673 lines, cc 151); `gateway_support/bound_routes_d.py:10 build_bound_routes_d` (591 lines, cc 125) | open (F-04) |
| 3 | Silent exceptions | 950 broad `except Exception`; 195 do nothing (`pass`) | note |
| 4 | Zero as missing | 12 `int/float(x.get(k) or N)` sites | review |
| 5 | Frontend route match | Matched in E147 / E148 for the app (`audit_tabs.py`, 0 missing except 6 prefix-built calibration paths to confirm by hand). | carried |
| 6 | Security | The gateway guards routes through a central list (`admin_guard.py` `PROTECTED`, wired with `Depends(_require_admin)`), not on each decorator, so the per-route count from the generic script does not apply here. Use `python3 scripts/audit_route_auth.py`: 109 mutating routes, 27 need the admin login, 82 are open and each is triaged in `docs/ROUTE_AUTH_TRIAGE.json` (`--check`: 0 untriaged, 0 stale). `/training/{path}` is a generic proxy: train, promote, lock clear, admin and debug-abort need the login (F-12); the paper-ledger writes below do not. `GATEWAY_ADMIN_GUARD=false` switches the whole guard off; compose defaults it to true and a missing secret fails closed (503). | note |
| 7 | Env settings | 299 names read; 110 have no mention outside the generated `docs/ENV_REFERENCE.md` | note |
| 8 | Money-path / critical functions | The gateway places no broker order. The money-adjacent routes are `/ops/power-off` (guarded, E168 UI confirmation) and the alert channel config (guarded). | note |
| 9 | Tests | 127 files, 6,252 test functions (not run in the sandbox) | VM run |
| 10 | Hygiene | stray files: 0; files over 1 MB: 0; same-name copies of shared/: 0 | pass |


Tests per 1,000 lines: 159.4. Silent handlers by folder: main.py 60, gateway_support 41, data_feed.py 16, ipo_scanner.py 15, kv_cache.py 9.

Order / capital / stop-named functions that are long or complex: `gateway_support/bound_routes_f.py:425 refresh_prepare_to_buy` (108 lines, cc 32); `data_feed.py:1214 find_prepare_to_buy_candidates` (85 lines, cc 34); `stockky_ms/order_blocks.py:44 find_order_blocks` (64 lines, cc 34).

## Findings
| ID | Sev | Finding | Action |
| --- | --- | --- | --- |
| F-29 | S3 | `/training/api/portfolio/deposit`, `/training/api/trades/manual`, `/training/api/trades/clear-backup`, `/training/api/actionable/commit` and `/training/api/predictions` change the paper ledger and need no login (kept open in E148 because the UI calls them). They never touch the broker. | Decide: keep, or add them to `_TRAINING_ADMIN` and send the token from the Paper Trades screen. |

## Not done in this sheet
- Split plan for `gateway_support/bound_routes_j.py` `build_bound_routes_j` (1,935 lines, cc 406) and the other `build_bound_routes_*` builders (F-04).
- Machine-caller service key (E150 note).
- `pytest` for this area on Python 3.14 (no pytest / sqlalchemy in the sandbox): `./stockky_full_test.sh`.
