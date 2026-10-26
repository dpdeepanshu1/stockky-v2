# E224 - short selling and the swing lane now ship in LIVE mode (behind their built-in rails)

## Changed
- `docker-compose.yml`: position-stocks `SHORT_MODE` default shadow -> **live** (`.env` still wins).
- `real-trade-service/config.py` + `control/catalog.py`: `SWING_MODE` default shadow -> **live** (shipped and recommended).
- `position-stocks control/catalog.py`: `SHORT_MODE` recommended shadow -> live. config default stays `off` for a junk / missing value (fail closed).
- Tests re-pinned: `test_compose_live_settings.py`, `test_short_foundation.py`, `test_swing_lane.py` (they asserted shadow). `.env.example`, `docs/ENV_REFERENCE.md` updated.

## Rails NOT changed (they are why live is not the same as "real money at risk today")
- Short live: refused until the Dhan SELL probe (job `dhan_sell_probe`) has a PASS (`SHORT_LIVE_REQUIRES_PROBE=true`); 1 share max (`SHORT_LIVE_QTY_CAP=1`); needs armed + module on + auto-pilot on (E222); always a broker stop; 14:55 cover.
- Swing live: DEMO only. REAL needs `SWING_LIVE_REAL_ENABLED=true` (still false). Needs the EOD square-off switch ON; `GET /swing/status` -> `wiring.problems`.
- Both modes stay on the auto-calibrator's PROTECTED list. Kill switch and loss brakes unchanged.

## Not found / not changed
- `ReconciliationStatus` does not exist anywhere in the code. Reconciliation is the read-only panel (E144) over `/reconcile-report/REAL` and `/reconcile/pending`; the reconcile jobs run live already (no shadow mode).
- The status probes (E144 panel) are read-only GET routes by design; the SELL probe (T6) and swing probe (S4) are runner jobs you start by hand.

## Run on your VM
`bash scripts/docker_test.sh position-stocks-service` and `... real-trade-service tests/test_swing_lane.py tests/test_swing_e138_wiring.py`, then rebuild/restart both services, then run `wiring_check` and open Calibration -> Short selling / Swing lane.
