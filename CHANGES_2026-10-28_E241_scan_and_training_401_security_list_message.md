# E241 - Time Machine scan / training 401s, clearer "security_id" error (2026-10-28)
Roll back: restore the four files named below from the E240 zip.

- `frontend/src/positionStocksApi.ts` - `timeMachineScan` now sends the admin session token (`requireAuth=true`). The route needs `require_admin`; the button sent nothing and got 401 every time.
- `frontend/src/gatewayAuth.ts` (+ test) - gateway admin calls (training start, alert settings, power-off ...) fall back to the Position Stocks session token when there is no Real Trade token. The gateway accepts either. Before, logging in on Position Stocks only left `POST /training/api/train` bare -> 401.
- `services/real-trade-service/execution/dhan_client.py`, `services/position-stocks-service/execution/dhan_client.py` - when the Dhan security list failed to load (cache empty), `get_security_id` says "security list is empty - check the Dhan token" instead of "No security_id for HFCL". New test in `test_dhan_client_remaining_coverage.py`.

Not code, do on the VM:
- `GET /scheduler-control/flags` 503: set `SCHEDULER_CONTROL_KEY` (16+ chars, same value for api-gateway and notification-scheduler-service) in `.env`, then `docker compose up -d api-gateway notification-scheduler-service`.
- `HFCL` "no security_id": reconnect Dhan (token may be expired; the market-data `DH-906 Invalid Token` shows the same). Order placement was never reached.
- Hugging Face 400: set `HF_MODEL` to a model your token can use.
- NOT run here: backend pytest (no deps in sandbox). Frontend: `node --experimental-strip-types src/gatewayAuth.test.mjs` passes.

## E241b - a failed security-list load is not retried on every order
- `execution/dhan_client.py` (real-trade-service and position-stocks-service): when the Dhan security list could not be loaded (cache still empty), `get_security_id` no longer starts another ~100 MB download on each order. It answers from the empty cache for 60 s (`_SECURITY_LOAD_RETRY_AFTER_FAILURE_S`), then tries again. A non-empty (stale) cache still reloads as before. The background warm task is unchanged.
- `tests/conftest.py` (both services): autouse fixture resets `_security_load_failed_at` between tests. New test `test_e241b_failed_load_is_not_retried_on_every_order`.
- NOT run here (no pytest in the sandbox): run `pytest tests/test_dhan_client_remaining_coverage.py tests/test_group179_security_cache_warmup.py` in real-trade-service and `tests/test_dhan_client.py` in position-stocks-service.
