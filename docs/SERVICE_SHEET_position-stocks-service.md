# Service sheet - position-stocks-service (E170)

Measured read-only in the E170 sandbox with `python3 scripts/audit_service_sheet.py position-stocks-service` plus the repo scanners; nothing was run against a broker or a database. The ten checks are the same for every sheet (see `docs/SERVICE_SHEETS_INDEX.md`).

| # | Check | Result | Verdict |
| --- | --- | --- | --- |
| 1 | Inventory | 145 files, 38,146 lines, 1,470 functions; routes 47 GET + 38 mutating; 168 test files | note |
| 2 | Function size | 33 over 100 lines, 49 with branch count over 25. Longest: `orders/entry.py:448 attempt_entry` (701 lines, cc 106); `main.py:773 _run_cycle` (613 lines, cc 103); `orders/eod_squareoff.py:421 run_eod_squareoff` (525 lines, cc 68); `orders/reconcile.py:1626 run_exit_reconciliation` (480 lines, cc 61) | open (F-04) |
| 3 | Silent exceptions | 619 broad `except Exception`; 47 do nothing (`pass`) | note |
| 4 | Zero as missing | 11 `int/float(x.get(k) or N)` sites | review |
| 5 | Frontend route match | 18 routes are not referenced by any frontend file (service-to-service, scripts, workflow). | carried |
| 6 | Security | Only two mutating routes lack a guard in the service: `POST /time-machine/run` and `POST /time-machine/compare` (simulation only, no orders; `compare` runs up to 15 stocks, so it is a CPU cost, not a money risk). Every order, gate, arm, close and login route takes the admin session. The service listens on 127.0.0.1:8006 only (compose loopback check passes); the gateway is the public door. | note |
| 7 | Env settings | 496 names read; 130 have no mention outside the generated `docs/ENV_REFERENCE.md` | note |
| 8 | Money-path / critical functions | Entry (`orders/entry.py` `attempt_entry`), exit reconcile, EOD square-off and the cycle are the four big money functions; their quantity checks default a missing broker quantity to 0, which reads as a mismatch (the cautious direction). Config defaults read in `config.py`: `STOPOUT_GUARD_ENABLED=true`, `ENTRY_MIN_QTY=3`. Line-by-line read NOT done. | partly done |
| 9 | Tests | 168 files, 3,190 test functions (not run in the sandbox) | VM run |
| 10 | Hygiene | stray files: 0; files over 1 MB: 0; same-name copies of shared/: 0 | pass |


Tests per 1,000 lines: 83.6. Silent handlers by folder: capital 10, main.py 7, boot_forensics.py 6, feed 5, screening 4.

Order / capital / stop-named functions that are long or complex: `orders/entry.py:448 attempt_entry` (701 lines, cc 106); `orders/reconcile.py:1626 run_exit_reconciliation` (480 lines, cc 61); `orders/entry.py:1178 attempt_manual_entry` (264 lines, cc 34); `orders/eod_squareoff.py:226 _fire_flat_sell` (175 lines, cc 27); `execution/dhan_client.py:637 place_super_order` (171 lines, cc 23); `main.py:424 _fast_reconcile_loop` (153 lines, cc 38).

## Findings
| ID | Sev | Finding | Action |
| --- | --- | --- | --- |
| F-30 | S4 | `/time-machine/run` and `/compare` need no login and replay thousands of simulated sessions per call. | Left (UI uses them without a login; loopback + gateway). Add a request cap or the login if the site is public. |

## Not done in this sheet
- Line-by-line read of `orders/entry.py` `attempt_entry` (701 lines), `main.py` `_run_cycle` (613), `orders/eod_squareoff.py` `run_eod_squareoff` (525), `orders/reconcile.py` `run_exit_reconciliation` (480), `attempt_manual_entry`, `_fire_flat_sell`.
- `pytest` for this area on Python 3.14 (no pytest / sqlalchemy in the sandbox): `./stockky_full_test.sh`.
