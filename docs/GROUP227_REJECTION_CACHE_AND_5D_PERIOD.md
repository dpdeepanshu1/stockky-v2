# Group 227 - rejection cache for the standard candidate track, and the missing `5d` period

Cumulative on group 226. Item 2 of the 2026-10-07 10:33 IST log review (items 1 and 3 are still open).
Rebuild: `docker compose build real-trade-service market-data-service && docker compose up -d`.
Files changed: `real-trade-service/candidate_engine/candidates.py`, `market-data-service/main.py`,
`real-trade-service/tests/test_group227_reject_cache.py` (new, 24), `market-data-service/tests/test_group227_5d_period_window.py` (new, 4).

## What the log showed
AXISCADES, RAYMOND, TRANSRAIL, JAYKAY, INDHOTEL, AETHER, KAVDEFENCE, DELTACORP, NYKAA, BHAGYANGR, MUKANDLTD, TATVA and GODIGIT were rejected
every cycle for reasons that cannot change in minutes ("top 12% of 52w range", "6m return < -10%"). Each re-evaluation cost 7 `/history`
calls, a quote and a market-cap call. That is the largest source of AngelOne `getCandleData` calls and so of the 403 cooldowns in item 1.

## Change 1 - rejection cache (real-trade-service)
`_multi_tf_analysis` now tags every definite rejection with `reject_kind`. `_refresh_standard_candidates` asks the cache first:

| `reject_kind` | Cached for | Why |
|---|---|---|
| `price_floor`, `downtrend_6m`, `atr`, `volume` | `REJECT_CACHE_STABLE_S` (3600 s) | built from daily/weekly history, barely moves intraday |
| `bullish_score`, `range_52w`, `resistance` | `REJECT_CACHE_PRICE_S` (900 s) | move with the intraday price, so a shorter hold |

A cached symbol skips the bulk quote prefetch, all 7 history calls and the market-cap call, and its "CANDIDATE REJECTED" line moves to DEBUG;
the cycle logs one INFO line `N of M symbol(s) answered from the rejection cache`.
- Never cached: data-starved, incomplete-history ("cannot judge"), "No live quote", MTF errors and passes. Only a real verdict is remembered.
- An entry is dropped at the IST date change, and an ATR rejection is dropped as soon as the adaptive ATR cap rises above the cached ATR.
- Memory only, bounded at 3,000 symbols (cleared when exceeded), cleared by `clear_history_state()` (so the test fixtures reset it).
- `REJECT_CACHE_STABLE_S=0` and `REJECT_CACHE_PRICE_S=0` together restore the old re-evaluate-every-cycle behaviour.

## Change 2 - `5d` period (market-data-service)
Found while tracing the 7 calls. `_multi_tf_analysis` asks `/history/{symbol}?period=5d&interval=1d` for its "1w" horizon. `5d` was not in the
AngelOne period map (nor the NSE fallback map), so it defaulted to 180 days: whenever AngelOne served the candles the "1w" return was really a
six-month return. It is now 7 calendar days in both maps, and `5d` ranks below every cap so a small `MAX_HISTORY_PERIOD` cannot widen it.
Expect fewer false "1w bullish" scores (a likely cause of the HFCL +211% 1w value group 186 had to drop as implausible).
This changes which candidates pass, so watch the first cycles.

## Not changed / limits
- Item 1 (candle cooldown also stopping quotes/feed) and item 3 (empty 200 from `/history`, last-good candles lost on restart) are not part of this group.
- The 7 timeframe calls per symbol that still run (uncached or cache-expired symbols) are unchanged; deriving them from one daily series is a separate change.
- The volume-shock track keeps its own checks and was not touched.
- The cache is per process: right after a restart the first cycle evaluates everything once.

| Env (real-trade-service) | Default | Meaning |
|---|---|---|
| `REJECT_CACHE_STABLE_S` | 3600 | hold for stable rejections; 0 = off |
| `REJECT_CACHE_PRICE_S` | 900 | hold for price-sensitive rejections; 0 = off |

## Tests
`cd real-trade-service && python -m pytest tests -q`: 3533 passed, 1 skipped, 1 error (the known group172 teardown error, identical on the unmodified upload).
`cd market-data-service && python -m pytest tests -q`: 1045 passed.
Coverage of every new line is complete (the 15 uncovered `candidates.py` lines in this sandbox are identical on the unmodified upload).
Run the new tests: `python -m pytest tests/test_group227_reject_cache.py -q --cov=candidate_engine.candidates --cov-report=term-missing`.
