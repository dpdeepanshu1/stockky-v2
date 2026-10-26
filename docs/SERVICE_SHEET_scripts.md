# Service sheet - scripts (E170)

Measured read-only in the E170 sandbox with `python3 scripts/audit_service_sheet.py scripts` plus the repo scanners; nothing was run against a broker or a database. The ten checks are the same for every sheet (see `docs/SERVICE_SHEETS_INDEX.md`).

| # | Check | Result | Verdict |
| --- | --- | --- | --- |
| 1 | Inventory | 86 files, 14,668 lines, 641 functions; routes 0 GET + 0 mutating; 63 test files | note |
| 2 | Function size | 4 over 100 lines, 21 with branch count over 25. Longest: `check_session21_fixes.py:78 main` (131 lines, cc 33); `fetch_history_for_backtest.py:116 main` (109 lines, cc 34); `audit_service_sheet.py:51 measure` (102 lines, cc 55); `wiring_check.py:95 check_service` (101 lines, cc 51) | open (F-04) |
| 3 | Silent exceptions | 76 broad `except Exception`; 10 do nothing (`pass`) | note |
| 4 | Zero as missing | 2 `int/float(x.get(k) or N)` sites | review |
| 5 | Frontend route match | n/a | carried |
| 6 | Security | No routes. The scripts read environment names of their own (`MD_URL`, `PS_URL`, `RT_URL`, `PROBE_TIMEOUT_S`, `STOCKKY_ADMIN_USER`, `STOCKKY_ADMIN_PASS`), which `docs/ENV_REFERENCE.md` does not cover (it lists service code only). `STOCKKY_ADMIN_PASS` is read from the environment, never from a file (secrets check passes). | pass |
| 7 | Env settings | 18 names read; 1 have no mention outside the generated `docs/ENV_REFERENCE.md` | note |
| 8 | Money-path / critical functions | `dhan_sell_probe.py` places ONE real 1-share SELL and covers it (the calibration runner has a `dhan_sell_probe` job for it); `dhan_swing_probe.py` is the swing twin. The script sends nothing without `--yes`, the service demands the phrase `PLACE_ONE_REAL_SELL_ORDER`, and refuses outside 09:30-14:30 IST, when not armed, when the kill switch is tripped, and for a held symbol. | note |
| 9 | Tests | 63 files, 530 test functions (not run in the sandbox) | VM run |
| 10 | Hygiene | stray files: 0; files over 1 MB: 0; same-name copies of shared/: 0 | pass |


Tests per 1,000 lines: 36.1. Silent handlers by folder: audit_service_sheet.py 3, _progress.py 1, fetch_history_for_backtest.py 1, monday_check.py 1, records_report.py 1.

## Findings
None new.

## Not done in this sheet
- Document the six script variables in the script docstrings or in `docs/ENV_REFERENCE.md`.
- `pytest` for this area on Python 3.14 (no pytest / sqlalchemy in the sandbox): `./stockky_full_test.sh`.
