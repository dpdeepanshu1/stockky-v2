# Round 14 fixes — 2026-09-01

## Changed files (3 files + 1 new doc)

### 1. `services/notification-scheduler-service/Dockerfile`
**Root cause:** docker-compose maps host `8008 → container 8000` and passes
`PORT=8000`, but the Dockerfile's CMD hardcoded `--port 8008` regardless.
Nothing was listening on container port 8000, so every proxied request
(`GET /notifications/config`, admin app's "DB AWAKE" panel, etc.) hit
connection-refused → 502 upstream. Matched the screenshot and diagnostic
log's `port 8008: 000`.

**Fix:** Added `ENV PORT=8000`, changed `EXPOSE` and `--port` to `8000`,
matching every other `:8000`-mapped service (analysis-intelligence-service,
decision-prediction-service) and the PORT env var docker-compose already passes.

---

### 2. `services/notification-scheduler-service/main.py`
Cosmetic follow-up: the `if __name__ == "__main__"` fallback still defaulted
to port 8008; aligned to 8000 for consistency (not on the container's actual
startup path, but would bite anyone running it directly).

---

### 3. `services/market-data-service/rate_limiter.py`
**Root cause of the 323s DEMO-cycle stall / "likely bulk-prefetch stall"
warning:** The "yfinance" circuit breaker was only checked *after* the
blocking rate-limiter `acquire()` (inside `_yf_call_with_hard_timeout`).
Once yfinance started timing out, all 3 patched call sites (`yf.download`,
`Ticker.history`, `Ticker.info`) kept paying the full `acquire()` wait
before ever reaching the breaker check.

**Fix:** Added `_breaker_allows_call()`, called at the top of all 3 patched
call sites, *before* `acquire()`. Once the breaker trips, subsequent calls
fail in milliseconds instead of paying rate-limiter wait + 18s hard timeout.

---

### 4. `services/real-trade-service/main.py`
Added the missing `GET /risk-config/{mode}` route. Diagnostic log showed
`HTTP 404` on this exact call. Read-only, no admin auth (matches the existing
`GET /status/{mode}`'s public-ish risk_config block), returns the same
fields `POST /risk-config` accepts.

---

## Oracle DB / onrender.com audit (no code change needed)
- All DB engines gate on `oracle_is_configured()` — Oracle always wins when
  `ORACLE_DSN` is set, regardless of `DATABASE_URL`.
- The `onrender.com` strings in the code are inter-service HTTP URL fallback
  defaults only, never DB connections. docker-compose.yml overrides every one
  with internal `http://<service>:<port>` addresses — dead code paths.
- `surprise_premarket.py` / `surprise_scanner.py` are intentionally
  Postgres-only and safely no-op when `DATABASE_URL` is unset on Oracle.
- **Action needed on your side:** confirm your VM's `.env` has `ORACLE_DSN`
  set. Code will always prefer Oracle once it's set.

---

## New finding (flagged for next round)
`GET /scan/universe` timed out at 20.01s (`HTTP 000`) in the diagnostic run.
`_get_momentum_movers()` appears to make a large number of sequential
per-symbol `/quote/{symbol}` calls. Not fixed in this round — next round.
