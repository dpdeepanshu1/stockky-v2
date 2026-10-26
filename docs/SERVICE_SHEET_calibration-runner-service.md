# Service sheet - calibration-runner-service (E170)

Measured read-only in the E170 sandbox with `python3 scripts/audit_service_sheet.py calibration-runner-service` plus the repo scanners; nothing was run against a broker or a database. The ten checks are the same for every sheet (see `docs/SERVICE_SHEETS_INDEX.md`).

| # | Check | Result | Verdict |
| --- | --- | --- | --- |
| 1 | Inventory | 17 files, 4,438 lines, 311 functions; routes 17 GET + 8 mutating; 27 test files | note |
| 2 | Function size | 1 over 100 lines, 5 with branch count over 25. Longest: `autocalib.py:522 evaluate` (129 lines, cc 48) | open (F-04) |
| 3 | Silent exceptions | 60 broad `except Exception`; 17 do nothing (`pass`) | note |
| 4 | Zero as missing | 0 `int/float(x.get(k) or N)` sites | pass |
| 5 | Frontend route match | 7 routes are not referenced by any frontend file (service-to-service and workflow). | carried |
| 6 | Security | 8 mutating routes, all guarded (it uses its own service token; the gateway holds no key for it). The smallest service with the cleanest result: 0 open, 0 undocumented, 1 long function (`autocalib.py` `evaluate`, 129 lines). | pass |
| 7 | Env settings | 2 names read; 0 have no mention outside the generated `docs/ENV_REFERENCE.md` | pass |
| 8 | Money-path / critical functions | No order code. It writes tighten-only overlays and weekly profile suggestions; `MARKET_STATE_RISK_MULT` profile values are 0.4 to 0.8, so the `or 1.0` default in `scripts/regime_shadow_report.py:139` cannot misread a zero today. | note |
| 9 | Tests | 27 files, 258 test functions (not run in the sandbox) | VM run |
| 10 | Hygiene | stray files: 0; files over 1 MB: 0; same-name copies of shared/: 0 | pass |


Tests per 1,000 lines: 58.1. Silent handlers by folder: runner.py 5, weather.py 5, autocalib.py 3, scheduler.py 3, notify.py 1.

## Findings
None new.

## Not done in this sheet
- Nothing blocking.
- `pytest` for this area on Python 3.14 (no pytest / sqlalchemy in the sandbox): `./stockky_full_test.sh`.
