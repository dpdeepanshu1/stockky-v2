# Group 299 - one fresh fundamentals computation per symbol at a time (market-data-service)

From the 2026-10-10 boot log: `peer_multi_quarter: Fundamentals fetch failed for CIPLA/DRREDDY/SUNPHARMA/DIVISLAB/APOLLOHOSP:
timed out` and `decision-engine: Fundamental unavailable: ReadTimeout`.

## What the log showed
- Both analysis-intelligence (`/fundamentals/X.NS`) and decision-prediction (`/fundamentals/X.NS?force=false`) ask market-data for
  the same symbol within the same second. `Fundamentals for X ...` is logged twice for ASTERDM, TRENT, MANIKA, CLEANMAX, INDOAMIN,
  ACEVECTOR, AFFORDABLE, NUVAMA, DIVISLAB, WELCORP, HFCL ...: each request ran the full Yahoo pass (`info`, `financials`,
  `balance_sheet`, `cashflow`, each with retries), so the same four-plus Yahoo calls were made twice.
- The peer batch has a 15 s client timeout. The same requests answered `200 OK` later (the work was done, the caller had given up).

## What changed (`market-data-service/main.py`)
`_get_fundamentals_inner` is now a thin wrapper; the old body is `_get_fundamentals_compute`, unchanged.
- The first caller for a symbol computes (with its own `force`). A caller that arrives meanwhile waits for it and then reads the
  result from the cache (a forced follower does not recompute: the first caller just fetched fresh data).
- A follower that waits longer than `FUNDAMENTALS_JOIN_WAIT_S` (default 40, blank/invalid/<=0 = 40), or finds the cache empty because
  the first caller failed, computes for itself exactly as before. A slow or broken Yahoo call can never block everyone.
- Spellings (`INFY`, `INFY.NS`, `infy`) share one flight. Different symbols never wait for each other. The lock map is capped at 5000
  (idle locks dropped first).
- `FUNDAMENTALS_SINGLE_FLIGHT=0` restores one computation per call. Both settings are in `.env.example` and `.env.oracle.recommended`.

## What it does not do
- It halves the duplicate Yahoo load; it does not make Yahoo faster. A cold pharma peer group of five symbols still needs five full
  Yahoo passes, which can exceed the 15 s peer timeout the first time. The result is then cached for 24 h, so the next request is
  instant. Raising the peer timeout (`fetch_fundamentals(timeout=15)` in analysis-intelligence) or warming peers before the session
  would be the next lever; neither was changed.
- Fundamentals stay on Yahoo (with the NSE fallback). Dhan has no fundamentals.

## Test-suite fix in the same group
8 tests in `test_group233_closed_market_last_close.py` / `test_group235_closed_skip_quota_sources.py` failed in a full
market-data run (they passed alone, and on the untouched group298 tree too). Cause: `test_group211_angelone_budget.py::
test_a_403_on_candles_no_longer_stops_quote_callers` sets a `rate_limiter` provider cooldown that outlived the test, so the next quote
tests skipped Yahoo/AngelOne. `tests/conftest.py` now clears `rate_limiter._cooldowns` around every test
(`_g299_reset_rate_limiter_cooldowns`). No service code changed for this.

## Tests
`tests/test_group299_fundamentals_single_flight.py` (27, real threads): one computation for two callers, spellings share a flight,
different symbols run in parallel, force handling for leader / follower / timed-out follower, leader failure, join limit, switch and
env parsing, lock released after an error, lock-map cap, the endpoint through the wrapper. 11 mutations on the new logic, all caught
(one by a deadlock timeout). Full market-data-service suite: 1594 passed. Rebuild market-data-service.
