# Group 161 — "no price" cache on `/quote`, and the missing `/last-close` route (market-data-service)

Item 1 of the 2026-10-05 open-market log list (08:42-08:46 UTC).

## What the log showed
- BMISL, QUALIANCE and BAGMANE timed out on real-trade-service's `/quote` call (8 s) every cycle, and
  market-data logged "possibly delisted; no price data found" for them.
- 3PLAND, MFML, AVALON, CMRGREEN and SUNLOC timed out on `/quote`, then got `404` from `/last-close`.
- Group 160 ignores timeouts on purpose, so these were still asked about every cycle.

## Causes
1. `/quote` walked the whole waterfall (Yahoo, NSE, AngelOne REST, IndianAPI, TwelveData, AlphaVantage,
   Polygon, bhavcopy) for a symbol that has no price anywhere, and could hold a worker up to the 18 s
   yfinance hard timeout. The caller gave up after 8 s, the worker kept running, and the next cycle
   started another one.
2. `GET /last-close/{symbol}` **did not exist** in market-data-service. real-trade-service's preview path
   (`_get_preview`) calls it after `/quote`, so every call was a `404 Not Found` and a wasted request.

## Changes (`services/market-data-service/main.py`)
- **"No price" cache.** After `QUOTE_NEG_AFTER` (default 2) full-waterfall failures in a row for a symbol,
  `/quote` answers the same "no price" payload at once (`source: "negative_cache"`, HTTP 200, `price: null`)
  for `QUOTE_NEG_TTL_S` (default 300 s). Each further failure doubles the window up to `QUOTE_NEG_MAX_S`
  (default 3600 s). real-trade-service already counts a 200 with no price as a miss, so group 160's
  30-minute pause now takes over for these symbols.
- **What does not count / what is never cached:**
  - failures while yfinance is in cooldown (an outage says nothing about the symbol);
  - a symbol that has a last-good price (it is served normally);
  - the entry is cleared by any real price.
- Spellings (`X`, `X.NS`, `x.ns`, `X.BO`) share one entry. The table is capped at 5000 symbols.
- `QUOTE_NEG_CACHE=0` turns the cache off. State is per process.
- **`GET /last-close/{symbol}`** (new). Answers from the quote cache / last-good fallback (previous close
  preferred), then the local NSE bhavcopy; never calls Yahoo or any paid API. `404` for delisted symbols
  and when no close is known.
- `/quote` is now a thin wrapper (`get_quote`) around `_get_quote_inner`, which holds the unchanged
  waterfall plus one early return for the negative cache.

## Not changed
- real-trade-service (group 160 pause logic, `_get_preview`) — untouched.
- The first two calls for a new dead symbol still walk the waterfall once each, per process.
- A liquid symbol whose sources all fail twice (outside a yfinance cooldown) is answered "no price" for
  up to 5 minutes. If you see that, check which source was down.

## Tests
`services/market-data-service/tests/test_group161_quote_negative_cache.py` (22 tests): threshold, window
expiry, doubling and cap, spellings, other symbols unaffected, real price clears, yfinance cooldown,
last-good price, off switch, bad env values, bounded table, `/last-close` sources and 404s.

Run on the VM:
`python3 -m pytest services/market-data-service/tests/test_group161_quote_negative_cache.py -q`
then the rest of that folder (`test_main_routes.py`, `test_main_helpers.py`, `test_delisted_fast_paths.py`).

Rebuild market-data-service.
