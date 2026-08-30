# Fix: /scan/watchlist timeout (test run 20260830_131556)

## What failed
```
FAIL   scan/watchlist   GET   000   25.002702s   http://localhost:8000/scan/watchlist
```
Status `000` at exactly the 25s mark = the test client's own timeout fired,
not a server error. The endpoint was still running when the client gave up.

## Root cause
`/scan/watchlist` already had a prior fix in place (per-symbol concurrency
via `WATCHLIST_SCAN_CONCURRENCY`, default 4, plus concurrent inner jobs per
symbol). That helps, but it was never a *hard guarantee*:

- Each batch of up to 4 symbols takes as long as its slowest single upstream
  call. Cold-cache fundamentals lookups alone hit 5.43s in this same test run
  (`fundamental/analyze/RELIANCE`).
- With concurrency=4, a watchlist past roughly 16-20 symbols on a cold cache
  needs 5+ batches — 5 x ~5s = 25s+, right where this failed.
- The per-call `httpx.Client(timeout=180, ...)` meant a single hung upstream
  call could also silently stall a whole batch far past any client's patience,
  with nothing capping the *endpoint's* total time.

## Fix (services/api-gateway/main.py, scan_watchlist())
1. Raised default fan-out: `WATCHLIST_SCAN_CONCURRENCY` 4 -> 8. Fewer batches
   for the same watchlist size; analysis-intelligence-service is stateless
   and takes the extra concurrent load fine.
2. Added a real wall-clock deadline: new `WATCHLIST_SCAN_TIMEOUT_SECONDS`
   (default 20s). The endpoint now uses `concurrent.futures.wait(..., timeout=...)`
   instead of blocking on every future via `as_completed`. Whatever hasn't
   finished by the deadline is reported back as a skipped entry in the
   response's `errors` list (`"scan deadline (20s) exceeded, skipped this
   run"`) instead of holding up the HTTP response. A new `"partial": true/false`
   field on the response makes this visible to the frontend.
3. Dropped the per-call `httpx.Client` timeout from 180s to 30s — with the
   endpoint-level deadline now doing the real bounding, a 180s per-call
   ceiling no longer serves a purpose and only masked a genuinely hung
   upstream call.
4. Straggler threads are released with `pool.shutdown(wait=False,
   cancel_futures=True)` so the HTTP response isn't held hostage by them —
   anything still in flight finishes in the background and simply warms the
   redis/kv caches those inner jobs already write to, so the *next*
   `/scan/watchlist` call picks up more of the watchlist warm.

## docker-compose.yml
- `WATCHLIST_SCAN_CONCURRENCY` default 4 -> 8.
- Added `WATCHLIST_SCAN_TIMEOUT_SECONDS` (default 20s), wired into the
  `api-gateway` service's environment.

## .env.example
- Documented both new/changed knobs under a new "Watchlist scan tuning"
  section (commented out, defaults match docker-compose.yml).

## Not changed — the WARNs are expected states, not bugs
- `training/api/report` (404), `training/api/insights` (404): both raise a
  deliberate 404 when no model has been trained yet in this environment —
  correct behavior on a fresh test run, not a missing route (both exist and
  are wired in `services/decision-prediction-service/training/app.py`).
- `dhan/status`, `dhan/network-check`, `audit-log`, `dhan/positions`,
  `dhan/holdings`, `dhan/orders` (all 401): the test script's own output
  notes these require an authenticated admin session and that 401 here is
  expected, not a bug.

No other files were touched — this zip only contains the 3 changed files,
same relative paths as your repo, so you can drop them straight in.

## How to merge
Copy these into your repo at the same paths (overwrite):
- `services/api-gateway/main.py`
- `docker-compose.yml`
- `.env.example`

Then rebuild/restart just the gateway:
```
docker compose build api-gateway
docker compose up -d api-gateway
```
No other services need rebuilding — nothing outside api-gateway changed.
