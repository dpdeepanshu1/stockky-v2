# Fix round 3 — /scan/watchlist, /market/top-gainers, /market/top-losers

## What the last run showed
```
FAIL   scan/watchlist       GET   000   25.002693s
FAIL   market/top-gainers   GET   000   25.002079s
FAIL   market/top-losers    GET   000   25.002126s
```
PASS/WARN/FAIL: 113/8/3 (was 115/8/1 before my round-2 patch). **Round 2 made
things worse** — `scan/watchlist` still failed, and it dragged two previously
fine, unrelated endpoints down with it. Full root-cause below; this round
reverts the mistake and fixes the actual problem.

## Why round 2 was wrong
Round 2 raised `WATCHLIST_SCAN_CONCURRENCY` 4 → 8 to cut the number of
batches. That ignored that each of those 8 "outer" symbols already runs its
own 5-way **inner** job concurrency (price/fundamentals/news/events/
prediction). 8 outer × 5 inner = up to **40 simultaneous outbound calls**
from one `/scan/watchlist` request alone.

This codebase already has an established, deliberately conservative ceiling
for exactly this reason — `MAX_PARALLEL_WORKERS` (used by `/scan` itself),
with its own comment on record:

> Higher values (12–20) spawn 4–5 internal HTTP calls per stock → 50–100+
> concurrent requests into analysis-intelligence-service, causing
> PoolTimeout / ReadTimeout and circuit-breaker opens that feed neutral
> 50.0 scores into the ML model.

Round 2's 40 concurrent calls blew straight through that ceiling, starving
`analysis-intelligence-service` and `market-data-service` for every other
endpoint sharing the same free-tier backend — which is exactly why
`market/top-gainers` and `market/top-losers` (completely unrelated code
paths) started timing out in the same run.

Round 2 also **leaked an `httpx.Client`**: the deadline/cleanup code called
`pool.shutdown(wait=False, cancel_futures=True)` but never closed `client`.
Every watchlist scan that hit the deadline left its connection pool open
forever, compounding the resource pressure on repeated calls.

## Root cause, `market/top-gainers` / `market/top-losers`
Independent of the above, `_get_nifty50_data()` had two real bugs of its own:
1. **No lock around the cold-cache fetch.** `top-gainers` and `top-losers`
   hit the endpoint back-to-back in the test; both saw the same cache miss
   and each independently kicked off its own full 50-symbol sequential
   yfinance fetch — double the work for the same "today" cache entry
   (`most-active`, tested right after, passed instantly once the cache was
   finally warm).
2. **Sequential, unbounded fetch loop.** 50 `yf.Ticker(...).history()` calls
   in a plain `for` loop, one at a time, with nothing capping total time.

## The actual fix (round 3)

### `services/api-gateway/main.py` — `scan_watchlist()`
- `WATCHLIST_SCAN_CONCURRENCY` reverted 8 → 4 (outer fan-out).
- **New**: a single shared `threading.BoundedSemaphore`
  (`WATCHLIST_SCAN_HTTP_CONCURRENCY`, default 8 = `MAX_PARALLEL_WORKERS`)
  that every inner job acquires around its actual network call. This caps
  **total** concurrent outbound calls for the whole request at 8, no matter
  how many symbols/jobs are queued — outer concurrency can no longer
  multiply into an overload.
- Deadline (`WATCHLIST_SCAN_TIMEOUT_SECONDS`) lowered 20s → 18s for more
  margin.
- **Fixed the leak**: stragglers past the deadline are now handed to a
  background daemon thread (`_cleanup_scan_resources`) that waits up to
  `WATCHLIST_SCAN_GRACE_SECONDS` (default 30s) for them, then always shuts
  down the pool and **closes the client**. Resources are always released;
  just not on the response's clock.
- Prediction job's per-call timeout dropped 60s → 30s so one slow call
  can't sit on a semaphore slot for a full minute.
- Response still includes `"partial": true/false`.

### `services/api-gateway/main.py` — `_send_scan_notification` call
- This was synchronous on the response path (a real POST, `timeout=15`,
  plus a possible second CallMeBot POST at `timeout=20`) — on top of the
  scan deadline, that alone could add 15–20s+ to the response. It's now
  fired from a background thread; the scan response no longer waits on
  Telegram/Discord/CallMeBot delivery.

### `services/api-gateway/main.py` — `_get_nifty50_data()`
- Added a module-level `threading.Lock` so concurrent cold-cache callers
  wait for the first fetch and reuse its result instead of duplicating it
  (with a post-lock cache re-check).
- Replaced the sequential 50-symbol `for` loop with a bounded
  `ThreadPoolExecutor(max_workers=MAX_PARALLEL_WORKERS)` — same ceiling
  used everywhere else in this file for yfinance-backed work.

### `docker-compose.yml` / `.env.example`
- `WATCHLIST_SCAN_CONCURRENCY` back to 4.
- Added `WATCHLIST_SCAN_HTTP_CONCURRENCY` (default 8) and
  `WATCHLIST_SCAN_GRACE_SECONDS` (default 30).
- `WATCHLIST_SCAN_TIMEOUT_SECONDS` default 20 → 18.
- `.env.example` section updated to match, with the reasoning inline.

## Not changed — still expected states, not bugs
- `training/api/report` / `training/api/insights` (404): correct behavior
  before any model has been trained in a fresh environment.
- All `dhan/*` and `audit-log` 401s: the test script's own output already
  flags these as expected (require an authenticated admin session).

## Verified
- `python3 -m py_compile` on every `.py` file under the repo (145 files,
  matching the test script's own Step 0) — clean.
- `docker-compose.yml` parsed with `yaml.safe_load` — clean.

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
No other service needs rebuilding — nothing outside api-gateway changed.

## If it still fails
If `/scan/watchlist` and/or `/market/top-gainers`/`/market/top-losers`
*still* hit 25s after this, the next lever is almost certainly upstream
capacity, not gateway-side concurrency — check
`analysis-intelligence-service` and `market-data-service` logs during the
test window for `PoolTimeout`/`ReadTimeout`/circuit-breaker-open entries,
and consider whether the test's watchlist size or NIFTY-50 fetch is simply
larger than 8-way concurrency can clear inside 18s on a fully cold cache
(in which case raising `WATCHLIST_SCAN_TIMEOUT_SECONDS` — not concurrency —
is the right knob, since the deadline mechanism now guarantees a response
either way, just possibly a partial one).
