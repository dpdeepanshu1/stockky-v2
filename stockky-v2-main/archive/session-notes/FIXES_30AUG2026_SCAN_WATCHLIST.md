# Fix: /scan/watchlist timeout (30 Aug 2026)

## What was still failing
Across all 5 test runs in your latest log, `GET /scan/watchlist` timed out
at the client's 25s limit (`http_code=000`) — the *only* failure that
never went away, even after `decision/decide`, `stockky-hot`,
`notification/call/me`, `scheduler/hydrate/weekend`, and
`notifications/test` had already stabilized from earlier fixes.

## Root cause
`fundamental/analyze/{symbol}` in `analysis-intelligence-service`
consistently took ~12s — in isolation, not just under load (see STEP 3 of
every test run: `fundamental/analyze/RELIANCE` at 11.79s–12.36s every
single time).

The reason: a single `/analyze/{symbol}` call fetched peer-stock
fundamentals over HTTP **sequentially, from up to 4 separate call sites**:

1. An inline peer loop in `fundamental/main.py` (up to 4 peers)
2. `rank_against_peers()`'s own ranking loop (up to 7 peers)
3. `compute_peer_relative()` called from inside `rank_against_peers()` (up to 5 peers)
4. `compute_peer_relative()` called *again* from inside
   `enrich_fundamentals_with_peer_and_consistency()` (up to 5 peers)

That's up to ~21 sequential blocking HTTP round trips for one fundamentals
call. `api-gateway`'s `/scan/watchlist` then called this — plus news,
events, and prediction — **sequentially per watchlist symbol**, so even a
single symbol's scan could exceed 25s, well before hitting the outer
per-symbol concurrency (`WATCHLIST_SCAN_CONCURRENCY`) that a prior fix had
already added.

## What changed

### `services/analysis-intelligence-service/fundamental/peer_multi_quarter.py`
- Added a shared, short-TTL (60s, `PEER_FUNDAMENTALS_CACHE_TTL_SECONDS`)
  in-process cache for peer fundamentals lookups.
- Added `fetch_fundamentals_batch()` which fetches any still-uncached
  symbols **concurrently** via a small thread pool
  (`PEER_FUNDAMENTALS_FETCH_WORKERS`, default 6) instead of one at a time.
- `fetch_fundamentals()` (existing function, same signature/return shape)
  now checks the cache first.

### `services/analysis-intelligence-service/fundamental/peer_ranking.py`
- `rank_against_peers()` now fetches its peer set via
  `fetch_fundamentals_batch()` instead of a sequential loop.

### `services/analysis-intelligence-service/fundamental/main.py`
- The inline peer loop in `analyze()` now uses the same cached/batched
  fetch, so it shares results with `rank_against_peers` /
  `compute_peer_relative` instead of re-fetching the same peers from
  scratch.

Net effect: the 4 call sites now share one cache and fetch concurrently,
so a cold-cache `/analyze/{symbol}` call costs roughly one round-trip
instead of the sum of ~21. Expect `fundamental/analyze/{symbol}` to drop
from ~12s to a few seconds on a cold cache, and to be near-instant for a
peer symbol already looked up within the last 60s (e.g. two watchlist
symbols in the same sector).

### `services/api-gateway/main.py`
- `_scan_one_symbol()` (used by `/scan/watchlist`) now runs its four
  per-symbol enrichment lookups — price fallback, fundamentals merge,
  news, events, prediction — **concurrently** instead of sequentially.
  These were always independent of each other (each only reads/writes its
  own keys on the normalized result), so nothing about *what* gets
  computed changed — only that it now happens in parallel. This is
  defense-in-depth on top of the analysis-service fix above: even in a
  worst case where one lookup is still slow, it no longer stacks with the
  other three.

### `docker-compose.yml`
No behavior changes by itself — added explicit env vars (with the same
defaults the code already falls back to) so these are tunable from your
`.env` without a code change/redeploy:
- `analysis-intelligence-service`: `PEER_FUNDAMENTALS_CACHE_TTL_SECONDS` (default 60), `PEER_FUNDAMENTALS_FETCH_WORKERS` (default 6)
- `api-gateway`: `WATCHLIST_SCAN_CONCURRENCY` (default 4, this already existed in code — just wasn't surfaced in compose before)

## What I did NOT touch
- The two `WARN` items (`training/api/report`, `training/api/insights`
  returning 404) are expected — they 404 until a training report exists
  on disk. Not a bug.
- `decision/decide`, `stockky-hot`, `notification/call/me`,
  `scheduler/hydrate/weekend`, `notifications/test` — all passed in your
  most recent run; these were already fixed by earlier changes and I left
  that code as-is.
- Nginx timeouts (`deploy/nginx-stockky.conf`) and the frontend's own
  request timeout (`frontend/src/api.ts`, already 180s for
  `/scan/watchlist`) were already generous — the 25s failures were an
  artifact of the test harness's own curl timeout, not a proxy/frontend
  config issue. No change needed there.

## How to merge
Copy these files into your repo at the same paths (this zip mirrors your
repo structure exactly), then rebuild/restart `api-gateway` and
`analysis-intelligence-service`:

```bash
docker compose build api-gateway analysis-intelligence-service
docker compose up -d api-gateway analysis-intelligence-service
```

Then re-run your test sweep and confirm `/scan/watchlist` passes well
under 25s.
