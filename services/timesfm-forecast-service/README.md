# timesfm-forecast-service (group 434e)

Forecast **parameters** (range, volatility ratio, volume pace, stop-touch probability), never a direction. See `docs/GROUP434E_TIMESFM_PARAMETER_PLAN.md`.

    docker compose --profile timesfm up -d --build timesfm-forecast-service
    docker compose --profile timesfm exec timesfm-forecast-service python selfcheck.py     # first real test of the model adapter
    python3 scripts/tsfm_nightly.py --universe-url http://localhost:8000/api/universe       # after the end-of-day bars

| Route | Use |
|---|---|
| `GET /health` | status, backend, `backend_error` (e.g. licence refusal), cached record count |
| `POST /batch` | `{"symbols": [...], "horizon": 5, "force": false}` - forecast and cache; skips a symbol already current |
| `GET /features/{sym}?stop_pct=&target_pct=&cached_only=true` | parameters from the cache (404 = missing or stale = no opinion) |
| `POST /forecast` | stateless, for `scripts/tsfm_probe.py` (candles in the request, nothing stored) |
| `GET /records` | what the cache holds |

Env: `TSFM_BACKEND` (`timesfm25` | `bigquery` | `research-3.0` | `fake`), `TSFM_TRADE_MODE` (`REAL` default | `DEMO`; `research-3.0` refuses `REAL`),
`TSFM_CHECKPOINT`, `TSFM_RESEARCH_CHECKPOINT` / `TSFM_RESEARCH_CLASS`, `TSFM_BQ_PROJECT`, `TSFM_HORIZON`, `TSFM_MAX_AGE_DAYS`, `TSFM_BATCH_MAX`, `TSFM_PRELOAD`.
Tests: `cd services/timesfm-forecast-service && python -m pytest tests -q` (no model needed).
