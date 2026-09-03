# Fixes for the 30-Aug-2026 test-sweep failures

Source: `~/stockky_test_results/failures_*.log` + `test_*.log` across 4 runs
(11:08, 11:22, 11:55, 12:15 UTC).

## Already fixed in your uploaded code (no action needed)

These showed up in run #1 (11:08) but the code you uploaded already has
the fix — comments in the files explain it, and runs #2-4 confirm they
pass now:

- `POST /notifications/test` — 502 "Notification service unreachable:
  timed out" at 15.02s. Your code already raised the timeout to 75s to
  match CallMeBot's worst case (`api-gateway/main.py`, `test_notification_channels`).
- `GET/POST /scheduler/hydrate/weekend` — occasional 25s hang. Your code
  already runs this as a background job with `/hydrate/weekend/status`
  polling (`weekend_hydrator.py`).

## Fixed in this zip

### 1. Root cause: yfinance calls can hang forever (no hard timeout)

**Files:** `services/api-gateway/rate_limiter.py`,
`services/market-data-service/rate_limiter.py`,
`services/analysis-intelligence-service/fundamental/rate_limiter.py`,
`services/notification-scheduler-service/scheduler/rate_limiter.py`

`yf.Ticker(...).history()` / `.info` / `yf.download()` are called all over
the codebase (movers, `/market/indices` sentiment, IPO/premarket scans,
etc.) without a hard ceiling. yfinance's own `timeout=` only bounds a
single underlying HTTP request — internal retries/backoff, and Yahoo
occasionally black-holing a connection instead of erroring, can let one
call hang indefinitely. Because `/market/indices` (sentiment) is on the
call path of every `decide()`, one stuck yfinance call was enough to hang
`decision/decide/{symbol}`, which cascades into `scan/watchlist` and
`stockky-hot` — this matches the `http_code=000` / exactly-25.00s pattern
in your logs (client gave up, server was still stuck).

All 4 services already funnel every yfinance call through one monkeypatch
(`rate_limiter.patch_yfinance()`), so the fix is centralized there: every
call now runs on a small dedicated thread pool with a hard wall-clock cap
(`YFINANCE_HARD_TIMEOUT_SEC`, default 18s, override via env). On timeout
it raises `TimeoutError`, which the existing `except Exception` blocks
already handle by falling back to cached/neutral data — no behavior change
on the happy path, just a guaranteed ceiling on the unhappy path.

### 2. `GET /stockky-hot` blocked on a full universe scan on cache miss

**File:** `services/api-gateway/main.py` (`stockky_hot_endpoint`)

On a cold cache this ran the full-universe scan (`stockky_hot_stocks`)
inline on the request — for a few hundred symbols that's minutes, not
seconds. Your codebase already has the right pattern for this
(`POST /stockky-hot/run` + `GET /stockky-hot/status`, background job +
poll). The GET route now reuses that: cache hit → return it; cache miss →
kick off the same background job (no-op if already running) and return
immediately with the last-known result (marked `stale`/`warming`) or a
`warming` placeholder if there's no prior result at all.
`force=true` still runs synchronously inline, unchanged, for callers with
a long enough client-side timeout (manual/GHA use).

### 3. `notification/call/me` could block for up to ~140s

**File:** `services/notification-scheduler-service/notification/main.py`

`_send_callmebot` tries a voice call then a text fallback per recipient,
each up to a 35s timeout, with one retry — worst case ~140s for a single
recipient. `call_me_now` ran this synchronously on the request.

Now it defaults to fire-and-forget (same pattern as `hydrate/weekend`):
kicks off the send on a background thread and returns immediately with
`{"ok": true, "started": true}`. Poll the new `GET /call/me/status` for
the real outcome (`idle` / `running` / `done` / `error` + the `_send_callmebot`
result string). Pass `?wait=true` to keep the old fully-synchronous
behavior for callers with a long enough client-side timeout.

## How to merge

Every file in this zip is a full replacement — same path as your repo, so
you can just copy them over:

```
services/api-gateway/main.py
services/api-gateway/rate_limiter.py
services/notification-scheduler-service/notification/main.py
services/notification-scheduler-service/scheduler/rate_limiter.py
services/analysis-intelligence-service/fundamental/rate_limiter.py
services/market-data-service/rate_limiter.py
```

All 6 files were verified with `python3 -m ast` (syntax-clean) before
packaging. Re-run your test sweep after deploying — `scan/watchlist`,
`stockky-hot`, `decision/decide/{symbol}` and `notification/call/me`
should no longer hit the 25s client timeout; `notification/call/me`'s
response shape changes (see #3) unless you pass `wait=true`.

## New/changed env vars (all optional, sane defaults)

- `YFINANCE_HARD_TIMEOUT_SEC` (default `18`) — hard ceiling per yfinance
  call, in each of the 4 services above.
- `YFINANCE_POOL_WORKERS` (default `8`) — thread pool size backing that
  ceiling, per service.
