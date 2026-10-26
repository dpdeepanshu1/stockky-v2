# Service sheet - analysis-intelligence-service (E170)

Measured read-only in the E170 sandbox with `python3 scripts/audit_service_sheet.py analysis-intelligence-service` plus the repo scanners; nothing was run against a broker or a database. The ten checks are the same for every sheet (see `docs/SERVICE_SHEETS_INDEX.md`).

| # | Check | Result | Verdict |
| --- | --- | --- | --- |
| 1 | Inventory | 53 files, 14,388 lines, 561 functions; routes 26 GET + 2 mutating; 54 test files | note |
| 2 | Function size | 11 over 100 lines, 29 with branch count over 25. Longest: `fundamental/main.py:309 analyze` (426 lines, cc 125); `technical/main.py:1036 analyze` (389 lines, cc 84); `fundamental/kv_cache.py:788 hard_reset_stockky_kv` (196 lines, cc 34); `event/main.py:950 _fetch_events_impl` (179 lines, cc 48) | open (F-04) |
| 3 | Silent exceptions | 246 broad `except Exception`; 59 do nothing (`pass`) | note |
| 4 | Zero as missing | 0 `int/float(x.get(k) or N)` sites | pass |
| 5 | Frontend route match | Called by the other services over the compose network. | carried |
| 6 | Security | Two mutating routes without a login (`/subscribe`, `/unsubscribe`), internal port. No order or money route. | note |
| 7 | Env settings | 84 names read; 8 have no mention outside the generated `docs/ENV_REFERENCE.md` | note |
| 8 | Money-path / critical functions | No order code. `fundamental/main.py` `analyze` (426 lines, cc 125) and `technical/main.py` `analyze` (389, cc 84) feed the scores real-trade ranks on. | note |
| 9 | Tests | 54 files, 2,175 test functions (not run in the sandbox) | VM run |
| 10 | Hygiene | stray files: 0; files over 1 MB: 0; same-name copies of shared/: 0 | pass |


Tests per 1,000 lines: 151.2. Silent handlers by folder: fundamental 19, technical 15, event 12, news 8, rate_limit_report.py 3.

Order / capital / stop-named functions that are long or complex: `stockky_ms/order_blocks.py:44 find_order_blocks` (64 lines, cc 34).

## Findings
None new.

## Not done in this sheet
- Line-by-line read of the two `analyze` functions; `fundamental/kv_cache.py` is a copy of the notification one (F-01).
- `pytest` for this area on Python 3.14 (no pytest / sqlalchemy in the sandbox): `./stockky_full_test.sh`.
