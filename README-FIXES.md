# Stockky test-sweep fixes — 2026-08-30

You sent two runs from the same day:

- **11:08:51** — cold start (first hit of the session): PASS 106 / WARN 8 / FAIL 5 / SKIP 1
- **11:22:14** — warm, ~14 min later: PASS 114 / WARN 8 / FAIL 2

Comparing the two pinpoints exactly which FAILs are real bugs vs. one-off
cold-start noise. Below is every FAIL from both runs, what it actually was,
and what (if anything) got changed.

## The 8 WARNs (both runs) — not bugs

- `training/api/report`, `training/api/insights` → legitimate 404 until a
  training run has produced a report.
- `dhan/status`, `dhan/network-check`, `audit-log`, `dhan/positions`,
  `dhan/holdings`, `dhan/orders` → 401, require an authenticated admin
  session. The test script says so itself. Nothing to fix.

## Real fixes (code changed)

### 1. `scheduler/hydrate/weekend` — timed out both runs (000 @ 25s)

Ran the full hydration batch synchronously inside the request handler.
That's legitimately a minutes-to-hours job (your own GHA workflow uses a
900s timeout for one slice) — any shorter-timeout client just sees a dead
connection.

**Fix:** background job + `GET /hydrate/weekend/status` to poll, mirroring
the pattern your `refill_additional.py` already uses. `?wait=true` keeps
the old fully-blocking behavior if you ever want it. Updated the GHA
workflow to poll instead of holding one long curl open.

Files: `scheduler/weekend_hydrator.py`, `scheduler/main.py`,
`.github/workflows/weekend-hydrator.yml`

### 2. `notifications/test` — failed only in the 2nd run (502 @ 15s)

Gateway proxied with a flat 15s timeout, but CallMeBot alone can take up to
35s per recipient (your `call/me` endpoint next to it already uses 70s for
exactly this reason). 15s was just too short for a config with CallMeBot
enabled.

**Fix:** raised the gateway timeout to 75s, added the same wake-first ping
`/notifications/config` already does, gave timeouts their own accurate 504
instead of a misleading "unreachable" 502.

**Bonus bug caught while fixing this:** the frontend's `request()` helper
auto-retries on timeout. With the 15s timeout reliably tripping, clicking
"Test Notifications" was silently re-firing the same real alert 2–3 times.
Fixed `testNotifications()` to use 0 retries (real external side effect —
should never auto-retry) and an 85s timeout to match.

Files: `services/api-gateway/main.py`, `frontend/src/api.ts`

### 3. `scan/watchlist` — timed out in the 1st (cold) run only, but it's a real design gap

`/decide/{symbol}` (the correlated `decision/decide/RELIANCE` FAIL below is
the same root cause) processes technical/fundamental/news/events/prediction
concurrently and has its own cache, so it's normally fast once warm. The
bug: `/scan/watchlist` on the gateway called `/decide/{symbol}` **one
symbol at a time in a plain `for` loop**, with no overall time budget. Even
a handful of watchlist symbols compounds linearly, so any slow symbol (cold
cache, upstream hiccup) stalls the whole scan — exactly what happened when
RELIANCE itself was slow that run.

**Fix:** parallelized the per-symbol work with a bounded `ThreadPoolExecutor`
(concurrency 4 by default, `WATCHLIST_SCAN_CONCURRENCY` env override),
mirroring the same concurrency pattern already used by
`refill_additional.py` / `weekend_hydrator.py`. Per-symbol logic is
byte-for-byte identical — only the *iteration* changed from sequential to
bounded-concurrent. This is a real robustness improvement, not just a
timing coincidence fix: a watchlist of even 5–10 symbols was always one
slow symbol away from a timeout.

File: `services/api-gateway/main.py`

### 4. `testCallMeBot` in the frontend — latent duplicate-real-call bug

Found while investigating the `notification/call/me` FAIL below. The
frontend called `/notifications/call/me` with a **30s** client timeout and
**1 retry**, but the gateway's own proxy for that route already uses a
**70s** timeout by design (CallMeBot voice-call-then-text can take that
long). So the frontend was reliably timing out before the gateway
finished, then auto-retrying — which means clicking "Test Call Me" could
place **two real phone calls**. Fixed to 0 retries + 80s timeout, same
pattern as fix #2.

File: `frontend/src/api.ts`

## Explained, not changed — expected cold-start/first-hit behavior

These all FAILED only in the 11:08:51 (cold) run and PASSED comfortably in
the 11:22:14 (warm) run, confirming they're first-hit latency, not bugs:

- **`decision/decide/RELIANCE`** (000 @ 25s → passed at 0.0029s next run).
  `/decide/{symbol}` fetches all pillars concurrently with per-pillar
  timeouts (20–35s) and caches the result. The very first call on a freshly
  started service had to actually compute everything cold; the server kept
  running after the test's 25s client gave up, populated the cache, and
  every call after that was near-instant. This is exactly what the cache
  is for — nothing to fix.
- **`stockky-hot`** (000 @ 25s → passed at 0.0046s next run). This has a
  proper cache (2–5 min TTL in market hours) plus a full async job system
  (`/stockky-hot/run` + `/status` + `/result`) for the expensive path — and
  your own frontend already calls the plain `GET /stockky-hot` with a 90s
  timeout, i.e. the app already expects this specific endpoint to be slow
  on a cache miss. The test harness's flat 25s timeout is just shorter than
  what this endpoint is designed to tolerate; the code already handles it
  correctly (cache-first, batched compute on miss).
- **`notification/call/me`** hit directly on port 8008 (000 @ 25s in the
  first run, 13.28s in the second — still no bug, just genuinely
  CallMeBot-speed-dependent). This is the same slow-by-design CallMeBot
  path as fix #2/#4 above; production traffic goes through the gateway
  proxy, which already budgets 70s and returns a clear message on timeout.

If you want any of these three hardened further (e.g. a startup warm-up
ping for `/decide` on a couple of default symbols, or a background-refresh
version of `stockky-hot` so a cache-expiry moment never blocks a live user
even briefly), say the word — didn't want to change working, intentional
designs without you asking for it.

## How to merge

Copy this folder's contents over your repo root — every path matches your
structure exactly. Files changed in total:

- `services/notification-scheduler-service/scheduler/weekend_hydrator.py`
- `services/notification-scheduler-service/scheduler/main.py`
- `services/api-gateway/main.py`
- `frontend/src/api.ts`
- `.github/workflows/weekend-hydrator.yml`

Nothing else was touched. Re-ran the full 145-file Python syntax check
(Step 0 equivalent) after every change — all clean.
