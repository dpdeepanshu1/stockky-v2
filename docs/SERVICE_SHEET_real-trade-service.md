# Service sheet - real-trade-service (E170)

E170 note: this sheet replaces the E169 version and uses the shared script. Corrected against E169: mutating routes in `main.py` are 23 `require_admin_if_real` + 10 `require_admin` (E169 said 31 + 11), the service has 97 route decorators in `main.py` (E169 said 95), and the silent-handler count is 92 in the whole service, of which 28 sit in order / capital / exit / reconcile code (E169 gave 29 as the total); zero-as-missing is 13 sites. No finding changed.

Measured read-only in the E170 sandbox with `python3 scripts/audit_service_sheet.py real-trade-service` plus the repo scanners; nothing was run against a broker or a database. The ten checks are the same for every sheet (see `docs/SERVICE_SHEETS_INDEX.md`).

| # | Check | Result | Verdict |
| --- | --- | --- | --- |
| 1 | Inventory | 163 files, 52,083 lines, 1,782 functions; routes 60 GET + 37 mutating; 247 test files | note |
| 2 | Function size | 46 over 100 lines, 55 with branch count over 25. Longest: `entry_engine/entry.py:607 evaluate_mode` (1318 lines, cc 192); `exit_engine/exit.py:462 _send_real_sell` (801 lines, cc 83); `exit_engine/exit.py:1307 evaluate_mode` (557 lines, cc 90); `portfolio/portfolio.py:352 import_broker_holdings` (476 lines, cc 63) | open (F-04) |
| 3 | Silent exceptions | 772 broad `except Exception`; 92 do nothing (`pass`) | note |
| 4 | Zero as missing | 13 `int/float(x.get(k) or N)` sites | review |
| 5 | Frontend route match | 30 routes in this service are not referenced by any frontend file (called by other services, scripts or the workflow); every call the app makes has a route (E148). | carried |
| 6 | Security | Mutating routes in `main.py`: 23 use `require_admin_if_real` (REAL needs the login, DEMO is open by design), 10 use `require_admin`, `POST /auth/login` is the login, `POST /emergency-pause` is open on purpose (it only disarms both modes, never sells). The `/control/*` routes (flags, overlay, audit) all take the admin session. 46 GET routes need the login, 11 are open: `/health`, `/ops/db-latency`, `/preopen/status`, `/pullback-rearm`, `/status/{mode}`, `/auth/session`, `/auth/config-check`, `/risk-config/{mode}`, `/adaptive/status`, `/adaptive/market-params/status`, `/calendar/today`. `/auth/config-check` shows secret length and an 8-character fingerprint, not the secret. | note |
| 7 | Env settings | 607 names read; 165 have no mention outside the generated `docs/ENV_REFERENCE.md` | note |
| 8 | Money-path / critical functions | Entry gates, sizing, stop placement, sell path, reconcile and close-all are separate modules. Close-all is confirm-gated (`CLOSE ALL`), disarms first, then sells. Money-safety defaults: `BROKER_STOP_ENABLED=true`, `BROKER_STOP_INTRADAY=false`, `SWING_BROKER_STOP_ENABLED=false`, `ENTRY_PRODUCT_MODE=cnc`, `OVERNIGHT_HOLD_ENABLED=false`, `STOPOUT_GUARD_ENABLED=true`, `ENTRY_MIN_QTY=3`, `KRONOS_GATE_MODE=off`. Line-by-line read of the long functions is NOT done. | partly done |
| 9 | Tests | 247 files, 4,597 test functions (not run in the sandbox) | VM run |
| 10 | Hygiene | stray files: 0; files over 1 MB: 0; same-name copies of shared/: 2 | pass |


Tests per 1,000 lines: 88.3. Silent handlers by folder: execution 16, entry_engine 10, candidate_engine 9, cycle_runner.py 8, boot_forensics.py 6.

Order / capital / stop-named functions that are long or complex: `exit_engine/exit.py:462 _send_real_sell` (801 lines, cc 83); `execution/reconcile.py:376 reconcile_real_orders` (347 lines, cc 49); `portfolio/portfolio.py:830 holdings_sync_reconcile` (343 lines, cc 52); `manual_engine.py:178 evaluate_manual_order` (314 lines, cc 62); `execution/dhan_client.py:1204 place_order` (219 lines, cc 28); `entry_engine/entry.py:1951 expire_stale_orders` (134 lines, cc 22).

## Findings
| ID | Sev | Finding | Action |
| --- | --- | --- | --- |
| F-24 | S4 | Three stale `*.py,cover` files (236 KB of coverage output) were shipped inside the service. | Removed in E169. |
| F-25 | S4 | `swing/live.py:403` `qty = int(t.get("qty") or 0) or 1` turns a stored quantity of 0 into 1, only to avoid dividing by zero in the swing journal percent (never sent to the broker). | Left. |
| F-26 | S4 | `/auth/config-check` is open and shows secret length plus an 8-character fingerprint. Safe while the secret is long and random (the service logs an error under the minimum length). | Left; put it behind the login if the port is ever exposed beyond the VM. |

## Not done in this sheet
- Line-by-line read of `entry_engine/entry.py` `evaluate_mode` (1,318 lines), `exit_engine/exit.py` `_send_real_sell` (801), `exit.py` `evaluate_mode` (557), `execution/reconcile.py` `reconcile_real_orders`, `portfolio/portfolio.py` `holdings_sync_reconcile`, `manual_engine.py` `evaluate_manual_order`.
- `pytest` for this area on Python 3.14 (no pytest / sqlalchemy in the sandbox): `./stockky_full_test.sh`.
