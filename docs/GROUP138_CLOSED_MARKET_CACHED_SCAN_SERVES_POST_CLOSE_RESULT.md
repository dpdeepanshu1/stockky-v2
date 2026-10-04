# group138 (2026-10-04) - closed market: `scan(cached=true)` serves the post-close result instead of re-sweeping (api-gateway)

Cumulative on group137. Application code changed in api-gateway only: rebuild api-gateway.

## Problem
Groups 120/131/132/137 stop the BOOT from sweeping when the market is closed. But every later `/surprise/scan?cached=true`
call still age-checked the restored result against `cached_max_age_sec` (~220 s). A Friday-evening or Sunday result is days
old, so the first caller (real-trade-service, the after-hours scan) ran the full ~1000-quote sweep, and then again every ~220 s
whenever someone called. While the market is closed prices cannot change, so none of those sweeps can find anything new.

## Change (`api-gateway/surprise_scanner.py`)
After the normal age check fails, `scan(cached=True)` (full-universe only, never a per-symbol request) now serves the
in-memory/durable result anyway when ALL of these hold:
- the session is over: weekend, NSE holiday (from `nse_holidays`), or outside 08:30-15:30 IST. The pre-open window 08:30-09:15 is
  excluded on purpose (it wants live data);
- the result was computed at least 600 s after the most recent trading-day 15:30 IST close (the sweep takes ~5 min, so one that
  straddled the close is not trusted). Anything older triggers one live sweep, which then serves all night;
- `SURPRISE_CLOSED_MARKET_CACHE` is not set to a false value (blank = on).
The response gets `from_cache: true`, the real `cache_age_sec`, and a new `market_closed_cache: true`. Any error in the
check falls through to the live scan as before. Open-market behaviour is unchanged.

Calendar note: in your `nse_holidays`, Fri 2026-10-02 is a holiday, so from Sunday the last close is Thursday's.

## Tests
`tests/test_surprise_scanner.py`: new `TestClosedMarketCache` (12: Sunday serves a Thursday post-close result, pre-close and
grace-window results are not trusted, open market still age-rejects, pre-open stays live, early morning serves, weekday evening
serves today's but not yesterday's, off switch + blank value, per-symbol requests excluded, helper failure falls through, close
date skips weekend/holiday). The existing fast-path age tests now pin the switch off because their fake clock falls after a
close. api-gateway full suite here: 8208 passed (was 8196).

## After deploying
    docker compose up -d --build api-gateway
While the market is closed, after the first caller (at most one sweep) `/surprise/scan?cached=true` answers from cache with
`market_closed_cache: true`; check market-data-service `/quote` volume stays near zero overnight. Set
`SURPRISE_CLOSED_MARKET_CACHE=0` in the api-gateway environment to restore the old behaviour.
