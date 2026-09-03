# 2026-09-01 — Bulk-quote prefetch stall (504) + volume-shock threshold widening

## Context (per Sync Context STOCKKY entries #17/#18/#21/#23)
Entry #18 fixed `/quotes/bulk` to check cache/live-feed before calling
`yf.download()`, and to degrade gracefully (200 + `degraded: true`) instead of
raising a raw 502. That fix is correct and still in place — the new incident
is a **different** bug in the caller, not a regression of it.

## Incident (admin-reported, 2026-09-01 ~08:59 IST)
Dashboard showed a 504 Gateway Time-out. The Pipeline tab's live cycle status
showed `Fetching candidates` stuck at **stage 333.9s / total 334.0s**,
`Source: volume_shock`, with the pre-market warning banner active. Container
logs showed repeated:
```
WARNING:rate-limiter:yfinance call exceeded hard timeout of 18s
ERROR:market-data-service:yf.download bulk failed: ... TimeoutError
INFO:circuit-breaker:rate_limit_hit recorded provider=market_data status=502
```
...back-to-back, with `/quotes/bulk` still returning **200** each time (entry
#18's graceful-degrade working as designed) but the *overall* stage still
stalling long enough for nginx's own timeout to fire a 504 before the client
ever saw a response.

## Root cause
`candidate_engine/candidates.py`'s `_prefetch_quotes_bulk()` (added in the
2026-09-01 incident-1 fix, see module docstring above it) chunks the scan
universe into groups of `CANDIDATE_BULK_QUOTE_CHUNK_SIZE` (40) and POSTs each
chunk to `/quotes/bulk` **sequentially, one at a time**. Pre-market:
- The AngelOne/Yahoo live-tick caches are cold (entry #18's market-hours gate
  correctly idles those background feed threads outside trading hours).
- The `quote:{symbol}` KV cache is cold for a fresh day.

So *every* chunk misses both cache layers and falls through to
`yf.download()`, which pays the full `YFINANCE_HARD_TIMEOUT_SEC` (18s) before
failing. A ~700-symbol `volume_shock` universe / 40 per chunk ≈ 18 chunks ×
~18s sequential ≈ **324s** — matching the observed 333.9s stall almost
exactly.

This was not a market-data-service bug (that service's own fixes are working
correctly) — it was the caller retrying a predictably-failing call 18 times
in a row instead of noticing after a few tries that yfinance isn't going to
answer right now.

## Fix (`services/real-trade-service/candidate_engine/candidates.py`)
`_prefetch_quotes_bulk()` rewritten with three independent, defensive layers
(function is still explicitly best-effort — never raises):

1. **Bounded concurrency** — chunks now fire concurrently, capped at
   `CANDIDATE_PREFETCH_CHUNK_CONCURRENCY` (default 4) via a semaphore, instead
   of one-at-a-time. Turns the N×18s worst case into roughly `ceil(N/4)×18s`.
2. **Fast circuit-break** — after `CANDIDATE_PREFETCH_FAIL_STREAK_BREAKER`
   (default 3) consecutive chunk failures/timeouts, stops firing further
   chunks for the rest of this call. Symbols that didn't get warmed fall back
   to the existing per-symbol `_fetch_quote()`/`_fetch_history()` path (or a
   clean "no quote available" reject) exactly as any unwarmed symbol already
   does — no behavior change there, just no more wasted 18s hard-timeouts once
   the pattern is clear.
3. **Overall wall-clock budget** — `asyncio.wait_for(..., timeout=
   CANDIDATE_PREFETCH_TOTAL_BUDGET_SECONDS)` (default 60s) as a hard backstop,
   so this stage can never itself exceed nginx's proxy timeout regardless of
   universe size or how the two fixes above behave.

All three are env-var tunable; no other function signature changed.

Verified: `python3 -m py_compile` clean.

## Volume-shock threshold widening (admin request — more candidates)
`VOLUME_SHOCK_MULTIPLIER` 2.0x → **1.5x**, `VOLUME_SHOCK_MIN_RETURN_PCT` 5.0%
→ **3.5%** (both env-overridable, same as before). This only widens the base
tier's entry door:
- `HIGH_CONVICTION` (15x vol / 15% return) and `UPPER_CIRCUIT` (≥19.9%
  return) tiers are untouched — that's where the strongest backtested edge
  is (55.7%/69.7% next-day win rate).
- The base tier's delivery-quality gate (`BASE_TIER_MIN_DELIVERY_PCT` ≥30%)
  is also untouched, so the additional candidates this admits are still
  screened for institutional-vs-churn delivery before insertion.

This is a **deliberate loosening, not a re-backtested threshold** — the
2026-09-01 819,906-row NSE bhavcopy re-backtest validated the tiers at their
prior cutoffs (2.0x/5.0%), not at 1.5x/3.5%. Recommend re-running that
backtest at the new cutoffs once there are a few weeks of live results, to
confirm win-rate/mean-return hold up, before loosening further.
