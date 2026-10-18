# Group 425 - Kronos forecast gate (Gate 4f), shipped OFF

## What was added
| Piece | Where |
| --- | --- |
| Worker (Kronos-small, vendored MIT code, FastAPI, one forecast at a time) | `services/kronos-forecast-service/` |
| Gate module (pure `evaluate()`, cached fail-open fetch, counters) | `services/real-trade-service/entry_engine/kronos_gate.py` |
| Config (`KRONOS_*`, default `off`) | `services/real-trade-service/config.py` (after the `STRUCTURE_*` block) |
| Gate 4f (after 4e, before Gate 5) and Gate 5.6 target | `entry_engine/entry.py` |
| `GET /kronos/status` | `services/real-trade-service/main.py` |
| Compose service, `KRONOS_URL`, `kronos-hf-cache` volume | `docker-compose.yml`, `.env.example` |
| Backtest | `scripts/kronos_backtest.py` |
| Tests | worker `tests/`, real-trade `tests/test_group425_*.py` |

## Behaviour
- `KRONOS_GATE_MODE` / `KRONOS_GATE_DEMO_MODE`: `off` (default, worker never called) | `log` (call the worker, count and log `KRONOS_GATE {...}`, never block, never change a target) | `enforce` (WAIT with `gate_tag="kronos"`; Gate 5.6 uses `min(fixed target, forecast EV)`). Any other value = `off`.
- Reject rule: forecast EV % below `KRONOS_MIN_EDGE_X_COST` x round-trip cost %, or fewer than `KRONOS_MIN_P_TARGET` of the paths reaching the target before the stop.
- A worker that is down, slow (`KRONOS_TIMEOUT_S`), or returns garbage = no opinion = the entry proceeds. A failed fetch is cached `KRONOS_FAIL_TTL_S`.
- 10 paths per forecast is a noisy estimate (P(target) moves in steps of 0.1). The defaults are hypotheses, not calibrated values.

## Turn-on order (do not skip the first step)
1. Fetch history (`scripts/fetch_history_for_backtest.py --sample 400 --seed 7`), start the worker, run `python3 scripts/kronos_backtest.py DIR --max-forecasts 200` first to time it, then the full run.
2. Enable only if the report says `VETO HELPS` (vetoed rows clearly worse, passed rows positive net of cost). `NOT PROVEN` or `VETO SEPARATES` means leave it off.
3. If it passes: `KRONOS_GATE_DEMO_MODE=log` for a few days (watch `/kronos/status` and `docker logs | grep KRONOS_GATE`), then `enforce` in DEMO, then REAL.
Kill switch: `KRONOS_GATE_MODE=off` (and `KRONOS_GATE_DEMO_MODE=off`) and restart real-trade-service.

## Not done on purpose
- No stop / position-size change from the forecast range (`risk_engine`): higher risk, only after the backtest passes.
- `decision-prediction-service`, `api-gateway`, `position-stocks-service`, frontend untouched.

## Verified here vs not
Run and passing in the build sandbox: worker stats / engine tests (11), gate unit tests and backtest tests (18), compose YAML parse, repo guard scripts (compose ports, market-data URL, shared copies, merge markers, table names, ENV_REFERENCE).
NOT run here (no pytest / sqlalchemy / torch / network in the sandbox): `tests/test_group425_kronos_entry_wiring.py`, the existing real-trade suite after the `entry.py` edit, the Docker build, and the real Kronos model. Run `python -m pytest tests -q` in `services/real-trade-service` first.
