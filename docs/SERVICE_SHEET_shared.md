# Service sheet - shared (E170)

Measured read-only in the E170 sandbox with `python3 scripts/audit_service_sheet.py shared` plus the repo scanners; nothing was run against a broker or a database. The ten checks are the same for every sheet (see `docs/SERVICE_SHEETS_INDEX.md`).

| # | Check | Result | Verdict |
| --- | --- | --- | --- |
| 1 | Inventory | 34 files, 4,108 lines, 182 functions; routes 0 GET + 0 mutating; 21 test files | note |
| 2 | Function size | 2 over 100 lines, 16 with branch count over 25. Longest: `chart_context.py:223 chart_context` (121 lines, cc 60); `patterns.py:228 chart_patterns` (110 lines, cc 75) | open (F-04) |
| 3 | Silent exceptions | 18 broad `except Exception`; 2 do nothing (`pass`) | note |
| 4 | Zero as missing | 0 `int/float(x.get(k) or N)` sites | pass |
| 5 | Frontend route match | n/a | carried |
| 6 | Security | No routes. Library code copied into services. | pass |
| 7 | Env settings | 1 names read; 1 have no mention outside the generated `docs/ENV_REFERENCE.md` | note |
| 8 | Money-path / critical functions | `shared/order_blocks.py` `find_order_blocks` (64 lines, cc 34) is tier A by name only. | note |
| 9 | Tests | 21 files, 212 test functions (not run in the sandbox) | VM run |
| 10 | Hygiene | stray files: 0; files over 1 MB: 0; same-name copies of shared/: 0 | pass |


Tests per 1,000 lines: 51.6. Silent handlers by folder: ml_features.py 1, scoring.py 1.

Order / capital / stop-named functions that are long or complex: `order_blocks.py:44 find_order_blocks` (64 lines, cc 34).

## Findings
| ID | Sev | Finding | Action |
| --- | --- | --- | --- |
| F-01 | S3 | Known and open: 148 recorded differences between shared copies (`scripts/shared_copies_baseline.json`); `check_shared_copies.py` passes with no new drift. Review stays report-only (E150 decision). | Left. |

## Not done in this sheet
- Review of the 148 known differences (F-02).
- `pytest` for this area on Python 3.14 (no pytest / sqlalchemy in the sandbox): `./stockky_full_test.sh`.
