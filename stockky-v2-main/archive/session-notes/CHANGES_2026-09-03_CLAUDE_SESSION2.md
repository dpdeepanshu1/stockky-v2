# Stockky — Session 2026-09-03 (Claude, this conversation): catalyst
watchlist + resilience status now visible on the dashboard

Closes the one open item both this conversation's audit and SESSION4
independently found: the new `WatchlistEntry`/`trade_watchlist` data and
circuit-breaker/dynamic-universe state had no backend endpoint and no
frontend surface at all.

## Backend

1. **`services/real-trade-service/cycle_runner.py`** — the
   `dynamic_universe` stage previously called `refresh_dynamic_universe(db)`
   and discarded the result. Now captures it and persists it via
   `resilience.local_cache.save_snapshot()` under `dynamic_universe_last`,
   tagged with `mode` and `synced_at`. Only writes on an actual sync (the
   function returns `None` on its own no-op cycles — outside market hours
   or before its ~20-min interval), so this doesn't spam the cache.

2. **`services/real-trade-service/resilience/circuit_breaker.py`** — added
   `CircuitBreaker.to_dict()`: returns `state` (`closed`/`open`/
   `half_open`), `consecutive_failures`, `failure_threshold`, `cooldown_s`,
   `seconds_until_retry`. Read-only, doesn't change breaker behavior.

3. **`services/real-trade-service/pipeline_status.py`** — `STAGES` tuple
   now includes `dynamic_universe` and `watchlist` (was missing both —
   backend-side counterpart to the frontend `STAGE_LABELS` fix from the
   prior round).

4. **`services/real-trade-service/main.py`** — two new endpoints:
   - `GET /watchlist-entries/{mode}` — lists `WatchlistEntry` rows
     (`?status=` filter, `limit` up to 300), newest catalyst first. This is
     a *different, earlier* stage than the existing `/candidates`-backed
     "Watchlist" tab — shows what was detected as a catalyst *before* any
     price-band/risk check ran, including `missed` rows (price ran past
     the entry band) so the chase-guard's actual behavior is visible.
   - `GET /resilience/status` — both circuit breakers' current state +
     the last dynamic-universe sync result. Both routes are purely
     observational: neither is read by entry_engine or anything in the
     trading path, only by the dashboard.

## Frontend

5. **`frontend/src/realTradeApi.ts`** — `watchlistEntries(mode, status?)`
   and `resilienceStatus()`, same `rtRequest` pattern as every other call.
   New types: `WatchlistEntry`, `WatchlistEntriesResponse`,
   `CircuitBreakerStatus`, `DynamicUniverseLast`, `ResilienceStatus`.

6. **`frontend/src/components/RealAutoTrade.tsx`** — new
   `CatalystWatchlistPanel` component, self-contained (fetches its own
   data every 30s, independent of the rest of the tab's state so a
   failure here can't disrupt anything else), added to the Pipeline tab
   right after the existing live-cycle status:
   - **Signal sourcing health** — a colored dot per breaker (green
     closed / amber half-open / red open) with failure count and retry
     countdown when degraded, plus the last dynamic-universe sync
     (added/removed/kept counts, timestamp).
   - **Catalyst watchlist** — filterable by status (active/entered/
     missed/expired), each row shows symbol, catalyst type, source tier
     (Tier 1/2/3, plain-language), horizon class, entry band %,
     conviction score, detection time, and the `missed_reason` text when
     the chase-guard rejected it — this is the piece that makes the
     chase-guard's actual day-to-day behavior visible instead of just
     trusted.

## Verified

- Every backend file: `python3 -c "import ast; ast.parse(...)"` — clean.
- Both frontend files: brace/paren balance checked — clean. (No
  `node_modules`/network available in this sandbox to run the real `tsc`
  build — same limitation as before; run `npm run build` before deploying
  to catch anything a syntax-only check can't, same as the last two
  sessions' own notes.)
