# Group 251 - after an AngelOne candle 403 the candle bucket refills gradually (2026-10-08 open-market log)

## What the log showed
`getCandleData returned HTTP 403 ... exceeding access rate` twice within about 3 minutes of the open (trip #1 -> 30 s candle
cooldown, trip #2 -> 60 s), while real-trade-service was asking `/history` for the volume-shock and candidate symbols
(CONFIPET, GALAXYSURF, TBOTEK, INDUSINDBK, JSIPL, TECHM, EIDPARRY ... ). The call pattern was already as light as it can be per
symbol (group 230/231: one cached 1y/1d fetch per symbol, 1 h cache while open), so this is about *how the candle bucket
behaves around a trip*, not about the number of calls per symbol.

Cause found in the code: during the 30 s cooldown the candle token bucket kept refilling (to its full burst of 3) and the
`/history` callers that were shed or queued during it were still waiting. When the cooldown ended they all went at once, which
is the same synchronized spike the 2026-09-21 fail-closed limiter was written to prevent. The 1.5 req/s sustained rate itself
also sits close to where AngelOne has been seen to 403 (forum reports in `rate_limiter.py`).

## market-data-service
- `rate_limiter.py`
  - `angelone_candle` default bucket 1.5/s burst 3 -> **1.0/s burst 2** (`RL_ANGELONE_CANDLE_RPS` / `RL_ANGELONE_CANDLE_BURST` still override).
  - New `slow_down(provider, hold_s, factor, slow_s)`: empties the bucket and holds the refill until the cooldown ends, then refills at
    `rps * factor` for `slow_s` more seconds. `_Bucket._eff_rps()` is used by `acquire`, `bucket_level`, `would_block`; `stats()`
    now also reports `effective_rps`.
- `angelone_budget.py`: a candle-family trip calls `slow_down("angelone_candle", cooldown, factor, window)`. Defaults:
  `ANGELONE_CANDLE_SLOWDOWN_FACTOR` 0.5, `ANGELONE_CANDLE_SLOWDOWN_S` 600; `_S=0` or factor >= 1 turns the slowdown off (the hold still applies
  only when on). Quote buckets and the quote cooldown are untouched; late 403s that arrive during a running cooldown do not re-slow.

## Limits
- Candle throughput is lower on purpose (about 60/min normally, about 30/min for 10 minutes after a trip). Callers that cannot get a token
  within `ANGELONE_CANDLE_MAX_WAIT_S` (15 s) are shed as before and fall back to yfinance or the last-good daily series, so a burst of
  never-seen symbols will see more yfinance fallbacks than before; the trade is fewer 30-60 s full candle blackouts.
- I could not see the exact call counts per second in the log, so 1.0/s and the 0.5 factor are judgement calls, not measured limits.
  Tune with the env vars above; `GET /angelone/budget` shows `effective_rps` and the trip count.
- Not confirmed live. After the next open: candle trips should stay at 0-1 and not repeat within minutes.

## Tests
New `tests/test_group251_candle_post_trip_slowdown.py` (20): new defaults and env overrides, empty-and-hold, half-rate refill after the hold,
no refill during the hold, return to full rate after the window, second trip extends, candle trip leaves quote bucket alone, quote trip leaves candle
bucket alone, late 403 does not re-slow, switches off, blank/bad env, budget off, `slow_down` never raises.
Real pytest, market-data-service: 1204 passed.
