# Service sheet - notification-scheduler-service (E170)

Measured read-only in the E170 sandbox with `python3 scripts/audit_service_sheet.py notification-scheduler-service` plus the repo scanners; nothing was run against a broker or a database. The ten checks are the same for every sheet (see `docs/SERVICE_SHEETS_INDEX.md`).

| # | Check | Result | Verdict |
| --- | --- | --- | --- |
| 1 | Inventory | 21 files, 7,302 lines, 339 functions; routes 22 GET + 16 mutating; 14 test files | note |
| 2 | Function size | 3 over 100 lines, 7 with branch count over 25. Longest: `notification/kv_cache.py:788 hard_reset_stockky_kv` (196 lines, cc 34); `scheduler/symbol_master_sync.py:106 run_sync` (138 lines, cc 21); `notification/kv_cache.py:614 kv_get_many` (101 lines, cc 30) | open (F-04) |
| 3 | Silent exceptions | 170 broad `except Exception`; 27 do nothing (`pass`) | note |
| 4 | Zero as missing | 0 `int/float(x.get(k) or N)` sites | pass |
| 5 | Frontend route match | Called by the gateway and the scheduled jobs. | carried |
| 6 | Security | 14 mutating routes, no login inside the service (internal port): `/notify`, `/call/me` (places a phone call), `/config` and `/config/{channel}` (change alert credentials), `/test`, `/outbox/process`, `/overnight/*`, `/hydrate/weekend`, `/symbol-master/sync`, `/neon-keepalive` and the pattern / watchlist-move runners. The gateway guards the public versions (`/notifications/config`, `/call/me`, `/test`, `/send-picks`, `/ops/overnight/*`, E150 senders). Anything that can reach 127.0.0.1 on the VM can call these directly. | note |
| 7 | Env settings | 90 names read; 31 have no mention outside the generated `docs/ENV_REFERENCE.md` | note |
| 8 | Money-path / critical functions | No order code. It sends messages and calls; a wrong send is noisy, not costly. | note |
| 9 | Tests | 14 files, 98 test functions (not run in the sandbox) | VM run |
| 10 | Hygiene | stray files: 0; files over 1 MB: 0; same-name copies of shared/: 0 | pass |


Tests per 1,000 lines: 13.4. Silent handlers by folder: notification 13, scheduler 12, control 2.

## Findings
| ID | Sev | Finding | Action |
| --- | --- | --- | --- |
| F-28 | S3 | 98 test functions for 7,302 lines (13.4 per 1,000 lines), second-thinnest after decision-prediction. | Add tests for `/notify` dedupe and the outbox. |

## Not done in this sheet
- Review of `notification/kv_cache.py` `hard_reset_stockky_kv` (196 lines; a hard-reset helper, duplicated in analysis-intelligence: F-01).
- `pytest` for this area on Python 3.14 (no pytest / sqlalchemy in the sandbox): `./stockky_full_test.sh`.
