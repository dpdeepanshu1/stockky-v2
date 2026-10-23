# E49 - E51: more gateway routers, installable engine package, scheduler alert switches

## E49 - gateway router split, second group
13 handlers moved from `api-gateway/main.py` into four files under `gateway_routers/`, code unchanged, handlers re-exported from `main`, each router included exactly where its routes used to be:

| File | Routes |
| --- | --- |
| `ops_status.py` | `/ops/loop-lag`, `/ops/db-latency`, `/ops/circuit-status` |
| `surprise_jobs.py` | IPO scan status / stop, Surprise premarket status, Surprise stop |
| `feed_jobs.py` | `/data-feed/bulk-cache`, structure-refresh stop, refill-additional status |
| `hot_picks.py` | Stockky Hot table and audit |

Proof: the ordered route table (method, path, handler, guards) was dumped before and after: 212 routes, identical. `/data-feed/bulk-cache` is also matched by the pattern route `/data-feed/{symbol}`, so order matters; `tests/test_gateway_routers_split.py` now checks that the FIRST matching route is the moved handler (moving one include to the end makes it fail). Two older source-scanning tests (`test_group360_structure_refresh`, `test_surprise_premarket`) now also read the router files.
Still in `main.py` (they use helpers that live in `main`): everything else; move them only together with their helpers.

## E50 - the market-structure engine as an installable package
`shared/pyproject.toml`: `pip install ./shared` gives `import stockky_ms` (same name as the per-service copies); standard library only. `scripts/check_ms_package.py` (CI step) fails if the file and `shared/market_structure/` disagree or the engine imports anything third-party; `--build` also builds a wheel from a temp copy and imports it. 6 tests in `scripts/test_check_ms_package.py`.
Deliberately NOT changed: the five per-service `stockky_ms/` copies stay (each Docker image is built from its own service folder; `sync_market_structure.py --check` keeps them equal to the folder the wheel is built from). Switching the images to `pip install` needs the compose build context moved to the repo root - do that as its own deploy-and-watch release.

## E51 - scheduler alert switches on the calibration page
New page section **Alert switches** (Real Money Calibration): `PATTERN_ALERTS_ENABLED / _TIME / _MAX_LINES / _MAX_SYMBOLS` and `WATCHLIST_MOVE_ALERTS_ENABLED / _ALERT_PCT / _CHECK_MIN / _MAX_SYMBOLS` - live, audited, alert-only (none touches a trade).
- Scheduler: `control/` (same `kit.py` / `boot.py` as the trading services, `catalog.py`, `env_view.py`, `auth.py`), mounted in `main.py` inside a try/except (a bug can never stop the scheduler booting). The scheduler reads these from the environment at call time and the kit writes `os.environ`, so a change is live at once. Overrides persist in `/data/control` on the existing volume.
- Security: the scheduler is not published and has no login, so `/control/*` answers only requests carrying `SCHEDULER_CONTROL_KEY` (16+ chars; missing/short = 503, wrong = 401). The browser never talks to it: the page calls the gateway `/scheduler-control/*`, which checks the admin session first and forwards only the switch paths (`flags`, `flags/KEY`, `preset/recommended`, `audit`) with the key and the admin's name (shown in the change log).
- Tests: 8 scheduler (`tests/test_e51_control_routes.py`, incl. a real boot of the scheduler app), 8 gateway (`tests/test_e51_scheduler_control_proxy.py`), frontend assertions in `calibration.test.mjs`.

### On the VM
1. `.env`: add `SCHEDULER_CONTROL_KEY=<openssl rand -hex 24>` (one value; both containers read it).
2. `docker compose up -d --build api-gateway notification-scheduler-service frontend`.
3. Calibration page -> Alert switches: 8 rows. Change `WATCHLIST_MOVE_ALERT_PCT`, then Clear it. Without the key the section says "not configured" and changes nothing.
