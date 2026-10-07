# Group 234 - bhavcopy close price, off-hours REAL cycle, IndianAPI only on real "no data"

Why group 233 "did not work": the closed-market last-close path ends in `eod_close_from_bhavcopy()`, and that never
found a price.

## 1. market-data-service / bhavcopy.py - close column not recognised (root cause)
- Both CSV parsers looked for the close column under `CLOSE, LAST, ClsPric, LastPric, ClosePrice`. NSE's
  `sec_bhavdata_full` (the first-choice source) names it `CLOSE_PRICE` (and `LAST_PRICE`); after the header clean-up
  that is `close_price`, which matched nothing. Every row was kept for its delivery % but had `close = None`, so every
  symbol was "not found" and `/quote`, `/quotes/bulk` fell through to AngelOne / Yahoo.
- New shared `_CLOSE_COL_NAMES` (CLOSE first, then CLOSE_PRICE, ClosePrice, ClsPric, LAST, LAST_PRICE, LastPric);
  legacy and UDiFF headers behave as before. One WARNING per distinct header when a file has no recognisable close
  column (`_warn_no_close_col`), so the next NSE format change is visible in the log.
- Single-flight per session date (`_BHAV_INFLIGHT`): concurrent callers share one ~10 MB download instead of one each.
  A failed fetch is not remembered. `BHAVCOPY_DAY_WAIT_S` (45).
- `prewarm_latest_day()` + a boot hook in main.py loads the newest session into the per-date cache, so the first
  closed-market `/quote` and `/quotes/bulk` calls are dict lookups. `BHAVCOPY_PREWARM=0` turns it off (tests/conftest
  sets it so TestClient boots never download).
- Item 2 of the plan ("seed quote:{symbol} at boot") is not needed: group 233 already answers from this day cache.

## 2. real-trade-service - manual REAL cycle outside market hours
- `POST /cycle/run/REAL` returns 409 "Outside market hours ..." unless `?force=true`. DEMO is unchanged.
  `MANUAL_REAL_CYCLE_OFFHOURS_BLOCK=0` restores the old "warn and run". Fails open if the clock check raises.
- A manual cycle gives its two data stages (watchlist, candidates) a deadline, `CYCLE_MANUAL_STAGE_TIMEOUT_S`
  (default 60, 0 = off). On the deadline the stage is cancelled, the session rolled back, and the cycle continues with
  what exists; the result carries `timed_out: true` and `stage_timeouts: [...]`. Entry, fills, exits and reconcile are
  never cancelled; auto-pilot cycles are never cut.
- Frontend: Run Cycle (REAL) asks for confirmation on the 409 and retries with `force=true`; a timed-out cycle shows
  "finished with partial results - timed out: ...". `rtRequest` now reports "Could not reach <path> (...)" instead of
  a bare "Failed to fetch".

## 3. analysis-intelligence-service - IndianAPI quota
- `/analyze/{symbol}` calls IndianAPI only when market-data ANSWERED with no core data. A timeout, transport error,
  429 or 5xx no longer spends IndianAPI quota (the 429s came from exactly that). `INDIANAPI_ON_MD_FAILURE=1` restores
  the old behaviour. Logged at INFO when skipped.

## Verify on the VM
    curl -s -A "Mozilla/5.0" https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_07102026.csv | head -2
After deploy the boot log should show `bhavcopy prewarm: <date> cached (N rows, M with a close price)` with M close to N.
M = 0 plus a `no close-price column recognised` WARNING means NSE changed the header again.

## Not done (see the remaining list in the changelog entry)
Concurrency cap on heavy market-data routes, IndianAPI use in other services, AngelOne lane budget, HF_MODEL, the
Moneycontrol 403, the COHANCE snapshot mismatch, polling protected endpoints before login.

## Tests
market-data tests/test_group234_bhavcopy_close_price.py (10); real-trade tests/test_cycle_runner.py (+6,
TestManualStageDeadline), tests/test_main_routes_trading.py (+6, TestRunCycle); analysis-intelligence
tests/test_fundamental_main.py (+9).
