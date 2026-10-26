# Service sheet - kronos-forecast-service (E170)

Measured read-only in the E170 sandbox with `python3 scripts/audit_service_sheet.py kronos-forecast-service` plus the repo scanners; nothing was run against a broker or a database. The ten checks are the same for every sheet (see `docs/SERVICE_SHEETS_INDEX.md`).

| # | Check | Result | Verdict |
| --- | --- | --- | --- |
| 1 | Inventory | 7 files, 1,577 lines, 80 functions; routes 1 GET + 1 mutating; 3 test files | note |
| 2 | Function size | 0 over 100 lines, 0 with branch count over 25. Longest: none | pass |
| 3 | Silent exceptions | 2 broad `except Exception`; 1 do nothing (`pass`) | note |
| 4 | Zero as missing | 0 `int/float(x.get(k) or N)` sites | pass |
| 5 | Frontend route match | Called by real-trade `entry_engine/kronos_gate.py`. | carried |
| 6 | Security | One mutating route (`POST /forecast`, a read-style call), reached over the compose network only (port 8010, no host port). | note |
| 7 | Env settings | 13 names read; 10 have no mention outside the generated `docs/ENV_REFERENCE.md` | note |
| 8 | Money-path / critical functions | No order code. The Kronos gate is `off` by default (verdict NOT PROVEN, 18 Oct). | note |
| 9 | Tests | 3 files, 11 test functions (not run in the sandbox) | VM run |
| 10 | Hygiene | stray files: 0; files over 1 MB: 0; same-name copies of shared/: 0 | pass |


Tests per 1,000 lines: 7.0. Silent handlers by folder: forecast_stats.py 1.

## Findings
| ID | Sev | Finding | Action |
| --- | --- | --- | --- |
| F-28 | S3 | 11 test functions for 1,577 lines (7 per 1,000 lines), the thinnest in the repo. The gate is off, so this is low risk today. | Add a request / response test before the gate is ever switched on. |

## Not done in this sheet
- Nothing blocking while the gate is off.
- `pytest` for this area on Python 3.14 (no pytest / sqlalchemy in the sandbox): `./stockky_full_test.sh`.
