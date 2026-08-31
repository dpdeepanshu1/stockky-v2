# Surprise / Volume-Shocker scanner fixes — 31-Aug-2026

## Files changed
- `services/api-gateway/surprise_scanner.py`
- `services/api-gateway/surprise_premarket.py`
- `services/market-data-service/surprise_premarket.py`

Drop these three files into the matching paths in your existing repo
(overwrite in place). No other files were touched, no new dependencies.

---

## Fix 1 — "breakout" tier was unreachable for most real volume shockers
**File:** `services/api-gateway/surprise_scanner.py`

`score_stock()` required a score of 65/100 to tag a stock "breakout", but
30 of those points were structurally almost never available:
- 15 pts needed live order-book depth (`buy_pct`) — the quote feed doesn't
  carry this, so it silently defaulted to neutral (0 pts) on nearly every call.
- 15 pts required the stock to be within 8% of its 52-week high — most
  single-day news/event-driven pops are nowhere near their 52W high.

Backtested against real historical big-move days for the exact stocks from
your Groww "Volume shockers" screenshots (Asian Hotels North, Bodal
Chemicals, Balrampur Chini, Ashoka Buildcon, Manali Petrochemicals, Modison,
etc.) using a year of NSE bhavcopy delivery data: only 7/17 scored
"breakout" before the fix; confirmed 5%+ moves like Indo Rama (+20%),
Ashoka Buildcon (+7.7%), Modison (+20%) were capped at "building" purely
because of those two inapplicable checks.

**Fix:** added a "Volume Shocker" override — a confirmed ≥5% move with
RVOL ≥2x and price above previous close now tags as `breakout` on its own,
independent of 52W distance or order-book data. Existing scoring is
untouched and can still promote a stock to breakout on its own merits.
Env-tunable: `SURPRISE_SHOCKER_MIN_CHANGE_PCT` (default 5.0),
`SURPRISE_SHOCKER_MIN_RVOL` (default 2.0).

Re-running the same backtest after the fix: 14/17 now score "breakout".

## Fix 2 — silent 14-mega-cap fallback universe
**Files:** `services/api-gateway/surprise_premarket.py`,
`services/market-data-service/surprise_premarket.py`

`default_universe_from_env()` is the last-resort symbol list used when
`SURPRISE_UNIVERSE`/`SCAN_UNIVERSE` isn't set AND the live universe builder
(`_build_scan_universe()`, ~500 symbols from NSE's securities list +
movers/news/bulk-deals/52W-extremes) fails or returns nothing — e.g. NSE
endpoint blocked or rate-limited. It silently fell back to just 14
mega-caps (RELIANCE, TCS, HDFCBANK...) — none of which are the small/mid-cap
names that actually show up as volume shockers. On a day the live fetch
failed, the premarket job would return 200 OK having seeded baselines for
only those 14 stocks, so the feature looked healthy while being structurally
unable to surface the stocks you actually care about — with nothing in the
response indicating degraded coverage.

**Fix:** replaced the 14-name list with 250 real, liquid NSE symbols,
data-derived from a year of NSE bhavcopy delivery data (ranked by median
daily turnover, ETFs excluded, requires ≥200 trading days present so
newly-listed/delisted noise is excluded) — spans large, mid and small caps
across sectors, not just mega-caps. Also added a `logger.warning(...)` when
this fallback activates, so it's visible in your logs instead of failing
silently.

## Testing
`backtest_surprise_test.py` (included) replicates `score_stock()`'s scoring
logic in pure Python against `nse_bhavdata_delivery_1y.csv` — no DB/network
needed. Run it before/after applying Fix 1 to reproduce the before/after
breakout-tier numbers above.
