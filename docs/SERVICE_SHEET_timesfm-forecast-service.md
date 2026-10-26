# Service sheet - timesfm-forecast-service (E170)

Measured read-only in the E170 sandbox with `python3 scripts/audit_service_sheet.py timesfm-forecast-service` plus the repo scanners; nothing was run against a broker or a database. The ten checks are the same for every sheet (see `docs/SERVICE_SHEETS_INDEX.md`).

| # | Check | Result | Verdict |
| --- | --- | --- | --- |
| 1 | Inventory | 5 files, 680 lines, 38 functions; routes 3 GET + 2 mutating; 4 test files | note |
| 2 | Function size | 0 over 100 lines, 1 with branch count over 25. Longest: none | pass |
| 3 | Silent exceptions | 9 broad `except Exception`; 0 do nothing (`pass`) | pass |
| 4 | Zero as missing | 0 `int/float(x.get(k) or N)` sites | pass |
| 5 | Frontend route match | Called by real-trade `entry_engine/timesfm_gate.py`. | carried |
| 6 | Security | Two mutating routes (`/forecast`, `/batch`), both read-style, internal port 127.0.0.1:8011. | note |
| 7 | Env settings | 14 names read; 3 have no mention outside the generated `docs/ENV_REFERENCE.md` | note |
| 8 | Money-path / critical functions | No order code. | note |
| 9 | Tests | 4 files, 27 test functions (not run in the sandbox) | VM run |
| 10 | Hygiene | stray files: 0; files over 1 MB: 0; same-name copies of shared/: 0 | pass |


Tests per 1,000 lines: 39.7. Silent handlers by folder: .

## Findings
None new.

## Not done in this sheet
- Nothing blocking.
- `pytest` for this area on Python 3.14 (no pytest / sqlalchemy in the sandbox): `./stockky_full_test.sh`.
