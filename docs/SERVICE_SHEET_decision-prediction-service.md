# Service sheet - decision-prediction-service (E170)

Measured read-only in the E170 sandbox with `python3 scripts/audit_service_sheet.py decision-prediction-service` plus the repo scanners; nothing was run against a broker or a database. The ten checks are the same for every sheet (see `docs/SERVICE_SHEETS_INDEX.md`).

| # | Check | Result | Verdict |
| --- | --- | --- | --- |
| 1 | Inventory | 84 files, 22,952 lines, 840 functions; routes 44 GET + 26 mutating; 35 test files | note |
| 2 | Function size | 23 over 100 lines, 41 with branch count over 25. Longest: `decision/main.py:1297 _decide_impl` (600 lines, cc 174); `training/train.py:526 train_pick_success_model` (341 lines, cc 39); `training/train.py:870 run_training_pipeline` (334 lines, cc 49); `prediction/pred_train.py:885 main` (238 lines, cc 34) | open (F-04) |
| 3 | Silent exceptions | 388 broad `except Exception`; 79 do nothing (`pass`) | note |
| 4 | Zero as missing | 1 `int/float(x.get(k) or N)` sites | review |
| 5 | Frontend route match | Reached only through the gateway `/training/*` and decision routes; matched in E148 for the Training Lab screen. | carried |
| 6 | Security | 26 mutating routes, none with a login inside the service: it is internal (127.0.0.1:8004 in compose) and the gateway decides who may call. The gateway guards train / promote / lock-clear / admin (F-12). Open through the proxy by earlier decision: the five paper-ledger writes in F-29. | note |
| 7 | Env settings | 112 names read; 47 have no mention outside the generated `docs/ENV_REFERENCE.md` | note |
| 8 | Money-path / critical functions | No order code. `decision/main.py` `_decide_impl` (600 lines, cc 174) produces the verdict that real-trade acts on; it is the highest-value function to read line by line in this service. | note |
| 9 | Tests | 35 files, 291 test functions (not run in the sandbox) | VM run |
| 10 | Hygiene | stray files: 0; files over 1 MB: 2; same-name copies of shared/: 0 | finding |


Tests per 1,000 lines: 12.7. Silent handlers by folder: training 45, decision 25, prediction 7, stockky_ms 2.

Order / capital / stop-named functions that are long or complex: `stockky_ms/order_blocks.py:44 find_order_blocks` (64 lines, cc 34).

## Findings
| ID | Sev | Finding | Action |
| --- | --- | --- | --- |
| F-28 | S3 | Thinnest tests by function count among the big services: 291 test functions for 22,952 lines (12.7 per 1,000 lines) versus 88 in real-trade, 84 in position-stocks, 90 in market-data, 151 in analysis-intelligence. | Add tests for `_decide_impl` branches and the training routes. |
| F-31 | S4 | Two binary model files are committed: `prediction/model.pkl` (1.06 MB) and `prediction/walk_forward_results.joblib` (1.32 MB). Pickle / joblib files run code when loaded. | Load only files you built; keep out any file that arrives from outside. Left as is. |

## Not done in this sheet
- Line-by-line read of `_decide_impl` and `training/train.py` `train_pick_success_model` / `run_training_pipeline`.
- `pytest` for this area on Python 3.14 (no pytest / sqlalchemy in the sandbox): `./stockky_full_test.sh`.
