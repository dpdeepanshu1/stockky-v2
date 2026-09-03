# 2026-09-01 — Bulk-quote prefetch 504 stall + volume-shock threshold widening

## What the screenshot/log showed
Dashboard: 504 Gateway Time-out. Pipeline tab stuck on "Fetching candidates",
stage 333.9s, source `volume_shock`, started pre-market (08:59 IST). Log:
repeated `yf.download bulk failed: TimeoutError` (18s hard timeout) back to
back, each followed by `circuit-breaker: rate_limit_hit ... status=502`.

## Root cause
`candidate_engine/candidates.py`'s `_prefetch_quotes_bulk()` chunks the
volume_shock scan universe (up to hundreds of symbols) into groups of 40 and
POSTs each chunk to `/quotes/bulk` **sequentially** — one `await` at a time.
Pre-market, AngelOne/Yahoo live-tick caches are cold, so every chunk falls
through to `yf.download()`, which pays the full 18s hard timeout before
failing. N chunks x 18s summed is exactly what produced the 300+s stall and
the resulting 504.

A second, compounding bug: `rate_limiter.py`'s monkeypatched `yf.download`
(`_patched_download`/`_yf_call_with_hard_timeout`) never checked the shared
"yfinance" circuit breaker before attempting a call — only the single-ticker
path (`_with_retry` in market-data-service/main.py) did. So even though the
breaker was recording failures on every chunk, nothing used that state to
skip the doomed 18s attempt on the next chunk.

## Fixes
1. **`services/market-data-service/rate_limiter.py`** — `_yf_call_with_hard_timeout`
   now checks `get_breaker("yfinance")` before submitting to the hardcap pool,
   and records success/failure on it. Once Yahoo is clearly down, chunk 2
   onward fails in milliseconds instead of another 18s.
2. **`services/real-trade-service/candidate_engine/candidates.py`** —
   `_prefetch_quotes_bulk()` now fires chunk POSTs concurrently (semaphore,
   `CANDIDATE_BULK_QUOTE_CONCURRENCY`, default 4) instead of one at a time.
   Combined with fix 1, a full Yahoo outage now costs roughly one hard-timeout
   window total, not one per chunk.

Both changes are additive/defensive — no change to what data is fetched or
how quotes are used, only to how fast a failing upstream is detected and how
many chunks run in parallel while it's healthy.

## Volume-shock breakout threshold (widened per request)
`VOLUME_SHOCK_MULTIPLIER`: 2.0x → **1.5x** (today's volume vs 20-day average)
`VOLUME_SHOCK_MIN_RETURN_PCT`: 5.0% → **3.5%** (today's return)

Both remain env-overridable (`CANDIDATE_VOLUME_SHOCK_MULTIPLIER`,
`CANDIDATE_VOLUME_SHOCK_MIN_RETURN_PCT`) with no code change needed to tune
further. `HIGH_CONVICTION`/`UPPER_CIRCUIT` tiers (15x/15%, 19.9%) and the
base-tier delivery gate were **not** touched — those are the tiers with the
strongest backtest support (55.7%/69.7% win rates); loosening the entry gate
around them doesn't change how they're computed.

**Trade-off, stated plainly:** the 30-Aug/2026-09-01 backtest numbers in the
code comments (44.8–45.7% base-tier win rate, mean ~+0.3–0.4%) were measured
at 2.0x/5.0%. Loosening the gate will let more, generally weaker breakouts
through — more candidates, but the base tier's average edge per trade is
expected to be a bit lower until re-backtested at these looser cutoffs. If
1.5x/3.5% turns out too loose (too much noise) or not loose enough (still too
few candidates), it's a one-line env var change, no redeploy needed if those
vars are wired to your env config.

Verified: `python3 -m py_compile` clean on both changed files.
