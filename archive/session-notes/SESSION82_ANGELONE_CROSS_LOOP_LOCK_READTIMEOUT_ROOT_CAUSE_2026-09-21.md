# Session 82 — AngelOne feed cross-event-loop lock crash → ReadTimeout storm root cause (2026-09-21)

## The finding

Everything in the "check for ReadTimeout / all other issues" sweep traces
back to ONE bug:

```
market-data-service-1 | ERROR:angelone-ws-feed:AngelOne feed error:
  <asyncio.locks.Lock object at 0x... [unlocked, waiters:1]> is bound
  to a different event loop
```

### Root cause

`AngelOneSession` (`market-data-service/angelone_client.py`) is a
module-level singleton (`_session = AngelOneSession()`) used from **two
different event loops in the same process**:

1. The main uvicorn loop — every FastAPI request handler in `main.py`
   that touches AngelOne (quotes, health checks, etc).
2. `angelone_ws_feed.py`'s dedicated background thread, which spins up
   its own `asyncio.new_event_loop()` (`start_feed_background()` →
   `_run()`) and calls `session.ensure_session()` on every poll cycle.

`AngelOneSession.__init__` created **one shared `asyncio.Lock()`**
(`self._lock`) guarding `ensure_session()`'s token-refresh critical
section. An `asyncio.Lock` lazily binds to whichever event loop first
contends on it; any subsequent `await` from a *different* loop raises
exactly the `RuntimeError` above.

### Why it mattered so much

`_poll_cycle()` called `await session.ensure_session()` with **no
try/except**, outside the `get_quotes_batch` try/except a few lines
below it. Once the lock bug fired, the exception propagated all the way
up through `_poll_forever()` to `_run()`'s outer handler, which does:

```python
except Exception as e:
    logger.error("AngelOne feed error: %s", e)
finally:
    _running = False
```

— logs one line and lets the **daemon thread die for good**. Nothing
ever restarted it (the same failure mode a prior 2026-09-21 fix already
patched for the "scrip master not loaded yet" case, just via a different
trigger this time).

With the feed thread dead, `live_quotes` (the fast, already-in-memory
AngelOne price source — "Source 1" in `real-trade-service/market_feed/feed.py`)
went permanently stale. Every subsequent quote lookup, from every
service, for **every symbol** fell through to the slow per-symbol
yfinance-backed paths instead:

- `real-trade-service`: `get_quote()` Source 2 (`/quote/{symbol}`) —
  this is the overwhelming majority of the `ReadTimeout` lines in the
  logs.
- `position-stocks-service`'s `quality_gate`: `technical fetch failed`,
  `fundamental fetch failed`, `event fetch failed` (all `ReadTimeout`).
- `analysis-intelligence-service`: `market-data history error ... timed
  out` for the same symbols, for the same reason — `yfinance` calls for
  the whole universe hitting market-data-service at once instead of a
  handful of cache-miss stragglers.

market-data-service and analysis-intelligence-service simply can't serve
~2,690 symbols' worth of individual yfinance calls per cycle — they were
never supposed to; that's exactly what the AngelOne feed's bulk polling
existed to avoid. One dead background thread turned the whole stack's
quote path into that worst case.

## The fix

**`market-data-service/angelone_client.py`** — `AngelOneSession` now
keeps **one `asyncio.Lock` per event loop** (keyed by the loop object,
in a dict guarded by a plain `threading.Lock` for the dict mutation
itself) instead of one shared lock:

```python
def _get_lock(self) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    with self._locks_guard:
        lock = self._locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[loop] = lock
    return lock
```

`ensure_session()` uses `async with self._get_lock():` instead of
`async with self._lock:`. Each loop gets its own lock instance, so
neither the main uvicorn loop nor the ws-feed thread's loop can ever
hit the "different event loop" error again. Trade-off: in the vanishingly
rare case both loops decide a token refresh is needed at the exact same
instant, they could both call `_login()` concurrently instead of one
waiting for the other — harmless (AngelOne just issues two valid
tokens) and far better than the guaranteed crash the old code had.

**`market-data-service/angelone_ws_feed.py`** — defense in depth,
matching the existing "retry instead of permanently dying" pattern
already in this file for the scrip-master case: `_poll_cycle()` now
wraps `await session.ensure_session()` in its own try/except. A failed
refresh logs a warning and skips just that one cycle instead of
propagating up and killing the thread forever.

**New regression test**:
`market-data-service/tests/test_angelone_session_cross_loop_lock.py` —
runs `ensure_session()` from two real, independent event loops (one via
`asyncio.run()` on the main thread, one via `asyncio.new_event_loop()`
on a background thread — the exact shape of the real bug) and asserts
no exception and that two distinct per-loop locks were created.

## What this does NOT explain / other items from the sweep

- **IndianAPI 429 (AVADHSUGAR)** — external provider rate limit, not a
  bug; existing fallback handling already in place. No change made.
- **`yfinance: possibly delisted` (SONA, QUALIANCE)** — routine
  yfinance noise for symbols with thin/no Yahoo coverage under their
  `.NS`/`.BO` suffix; `market-data-service` already has multi-source
  fallback for this. No change made.
- **nginx `connect() failed (111: Connection refused)` clusters for
  ports 8005/8006 across 09-18 through 09-21** — these line up with
  separate redeploy windows across sessions 73–81 (each `docker compose
  up -d` briefly drops the old container before the new one is
  healthy), not a distinct crash loop. Worth keeping an eye on, but
  there's no evidence in these logs of an ongoing issue independent of
  deploys — `docker compose ps` in this same session shows both
  containers `Up ... (healthy)`.
- **`GET /admin/config.php` from 130.210.2.159 (09-20 13:51)** —
  internet background-noise vulnerability scanning hitting the VM by
  raw IP rather than the `stockky.duckdns.org` hostname; nginx's
  `server_name`-only vhost doesn't serve it as anything meaningful
  (matches the catch-all/default block, if any), not a Stockky bug.

## Verification

```
services/market-data-service:       13 passed  (was 12 — +1 new regression test)
services/real-trade-service:        475 passed, 1 xfailed
services/position-stocks-service:  1218 passed, 1 skipped
```

No `FAILED` lines. No application code besides the two files above
touched.
