# Service sheets - index (E170)

Twelve one-page sheets, one per area, all with the same ten checks. Numbers come from `python3 scripts/audit_service_sheet.py <area>` (`--all` for the table below); a verdict is written by hand in each sheet. Nothing here was run against a broker or database, and no backend test suite was run (the sandbox has no pytest, fastapi or sqlalchemy).

## The ten checks
1. Inventory (files, lines, functions, routes, test files)
2. Function size and complexity (over 100 lines, branch count over 25, longest functions)
3. Silent exceptions (`except Exception: pass`)
4. Zero treated as missing (`float(x.get(k) or N)`)
5. Frontend route match (carried from `scripts/audit_tabs.py`)
6. Security (login guard on mutating routes; the gateway uses a central list, see `scripts/audit_route_auth.py`)
7. Env settings (names read, and whether anything besides the generated `docs/ENV_REFERENCE.md` explains them)
8. Money-path functions (read by hand where an order, stop or capital is touched)
9. Tests (files and test functions; not run here)
10. Hygiene and shared code (stray files, files over 1 MB, same-name copies of `shared/`)

## All areas
| Area | Files | Lines | Functions | Over 100 lines | Silent `pass` handlers | Test functions | Tests per 1,000 lines |
| --- | --- | --- | --- | --- | --- | --- | --- |
| real-trade-service | 163 | 52,083 | 1,782 | 46 | 92 | 4,597 | 88.3 |
| position-stocks-service | 145 | 38,146 | 1,470 | 33 | 47 | 3,190 | 83.6 |
| api-gateway | 92 | 39,233 | 1,284 | 67 | 195 | 6,252 | 159.4 |
| decision-prediction-service | 84 | 22,952 | 840 | 23 | 79 | 291 | 12.7 |
| market-data-service | 28 | 16,948 | 696 | 15 | 86 | 1,526 | 90.0 |
| notification-scheduler-service | 21 | 7,302 | 339 | 3 | 27 | 98 | 13.4 |
| calibration-runner-service | 17 | 4,438 | 311 | 1 | 17 | 258 | 58.1 |
| analysis-intelligence-service | 53 | 14,388 | 561 | 11 | 59 | 2,175 | 151.2 |
| kronos-forecast-service | 7 | 1,577 | 80 | 0 | 1 | 11 | 7.0 |
| timesfm-forecast-service | 5 | 680 | 38 | 0 | 0 | 27 | 39.7 |
| shared | 34 | 4,108 | 182 | 2 | 2 | 212 | 51.6 |
| scripts | 86 | 14,668 | 641 | 4 | 10 | 530 | 36.1 |

## Findings from the twelve sheets
| ID | Sev | Area | Finding | State |
| --- | --- | --- | --- | --- |
| F-24 | S4 | real-trade | Three stray `*.py,cover` files shipped inside the service | Removed (E169) |
| F-25 | S4 | real-trade | Swing journal percent uses quantity 1 when the stored quantity is 0 (journal only, no order) | Left |
| F-26 | S4 | real-trade | `/auth/config-check` open; shows secret length and an 8-character fingerprint | Left |
| F-27 | S3 | all services | `docs/ENV_REFERENCE.md` listed 802 variables; 690 more were read through wrappers (`_env_int("X", 5)`, `_bs_bool`, `__import__("os").getenv`, `ref.os.getenv`) and were missing | Fixed: generator extended, reference regenerated (1,492 variables, wrapper-only names marked `(helper)`), two tests added |
| F-28 | S3 | decision-prediction, notification-scheduler, kronos | Thinnest tests by function count: 12.7, 13.4 and 7.0 per 1,000 lines, against 84 to 151 in the big services | Open |
| F-29 | S3 | api-gateway / decision-prediction | Five paper-ledger write routes under `/training/api/...` need no login (kept open in E148 because the UI calls them; never touch the broker) | Open: decide keep or guard |
| F-30 | S4 | position-stocks | `/time-machine/run` and `/compare` need no login and replay thousands of simulated sessions per call | Left |
| F-31 | S4 | decision-prediction | Two pickled model files committed (`model.pkl`, `walk_forward_results.joblib`, 2.4 MB); pickle runs code when loaded | Left; load only files you built |
| F-32 | S3 | market-data, api-gateway | 86 and 195 silent `pass` handlers (5.1 and 5.0 per 1,000 lines), the densest in the repo; in a quote path a swallowed error is a stale or empty price with no log line | Open: log one warning per provider-call site |

## What a sheet cannot tell you
- Whether a long function is correct: the six real-trade and four position-stocks money functions are still unread line by line (days of reading, listed in each sheet).
- Whether any test passes: run `./stockky_full_test.sh` on the VM.
