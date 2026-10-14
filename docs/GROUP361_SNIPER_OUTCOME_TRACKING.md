# Group 361 - Buy Sniper outcome tracking (WS-6 for the sniper)

real-trade and position-stocks store a `StructureObservation` per symbol / day (g356). The sniper had none, so nothing showed whether its structure gate (g357, enforce by default) helps. This adds the same measurement in the gateway, on the shared pure engine `stockky_ms.observation`, so all three are judged by the same rules.

## What is recorded
After every `/scan/find-buys` answer, one row per symbol per IST day (the first concrete verdict of the day wins):
- surfaced card with a structure verdict -> `passed`, or `rejected_shadow` when `SNIPER_STRUCTURE_MODE=shadow` flagged it
- `structure_rejected` setups (enforce) -> `rejected_enforced`
- gate off / no opinion / no price -> nothing

Row = price then, structure stop / target / ATR, reasons, a trimmed `ms_*` summary, gate mode. Later the evaluator fills T+1/5/10/20 return, MFE, MAE and the first touch of the structure's own target vs stop. A row is complete at T+20; a row with no bars after `SNIPER_OBS_GIVE_UP_DAYS` (45) is closed (history answered but has nothing after the day). A failed or empty-inside-window fetch never closes a row.

## Where it lives
- `sniper_observation.py` (new). Storage is `kv_cache` (durable in Neon): one shard per IST day `stockky:sniper_obs:day:YYYY-MM-DD` and an index `stockky:sniper_obs:index` with `{n, open}` per day, so the evaluator only reads days that still have open rows. Retention 120 days. The prefix is registered in the gateway's `_DURABLE_PREFIXES` (gateway-only, listed in `test_kv_cache_drift.GATEWAY_ONLY`; `shared_copies_baseline.json` updated for that one intended difference).
- `main.py`: the route hands the finished answer to a worker thread (`_sniper_observe`, fire-and-forget, fail-open: the response never waits on the DB and `out` is never changed). New routes:
  - `GET /api/scan/find-buys/edge?days=60&recent=40&min_n=20` - per-verdict stats, gate edge (passed minus rejected), conclusion, newest rows, process counters.
  - `POST /api/scan/find-buys/edge/evaluate` - one evaluator pass now.
  - QStash tick body `{"action":"sniper-observe-eval"}` runs the same pass.
  - Counter `stockky_sniper_obs_total{event=recorded|evaluated}` on `/metrics`.
- Single-process assumption: a read-modify-write lock is process-local; a second gateway instance could lose a same-day write (one row).

## How to read it
`conclusion` is `insufficient_sample` until both the passed and rejected sides have `min_n` (20) rows with a finished T+N; then `gate_helps | gate_hurts | inconclusive` (edge threshold 0.5 %). Expect to wait ~20 trading days. Do not change thresholds on less. Observations are stored whatever `SNIPER_STRUCTURE_MODE` is (except `off`), so `shadow` for a while gives the cleanest comparison: the rejected setups are still shown and still tracked.

## Schedule it
POST `{"action":"sniper-observe-eval"}` to `/ops/qstash/tick` at ~16:30 IST Mon-Fri (after the g360 structure refresh at ~16:00), or hit `POST /api/scan/find-buys/edge/evaluate`.

## Config (`.env.example`, `docs/ENV_REFERENCE.md`)
`SNIPER_OBS_ENABLED` (1), `SNIPER_OBS_MAX_PER_DAY` (60), `SNIPER_OBS_RETENTION_DAYS` (120), `SNIPER_OBS_BATCH` (40 symbols per pass), `SNIPER_OBS_HISTORY_TIMEOUT_S` (8), `SNIPER_OBS_GIVE_UP_DAYS` (45).

## Tests
`tests/test_group361_sniper_observation.py` (40): row building with the REAL sniper (enforce, shadow, gate off, no opinion, dedupe, garbage), record (shard + index, first verdict wins, day cap, kv failure, retention), evaluate (not due same day, outcomes, T+20 completion, 20 h refetch guard, fetch failure, give-up rules, batch, first touch), report (empty, aggregate, newest first, window clamp), history fetch, and `main.py` wiring as source / AST. Full gateway suite: same 81 failures / 42 collection errors as the untouched g359 zip (missing yfinance / sqlalchemy here), +108 passes (g360 + g361).

## Not run
Real fastapi app, live kv / Neon round trip, live market-data history, `npm run build`.

## Not done
No UI for the edge report yet (use the endpoint). Rows hold one verdict per symbol per day, not the later fate of a card the user actually traded.
