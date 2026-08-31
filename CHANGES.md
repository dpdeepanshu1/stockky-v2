# Surprise / Volume-Shocker scanner fixes — 31-Aug-2026

## Files changed
- `services/api-gateway/surprise_scanner.py`
- `services/api-gateway/surprise_premarket.py`
- `services/api-gateway/main.py` (one-line docstring only)
- `services/market-data-service/surprise_premarket.py`

Drop these four files into the matching paths in your existing repo
(overwrite in place). No other files were touched, no new dependencies,
no schema changes.

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
your Groww "Volume shockers" screenshots using a year of NSE bhavcopy
delivery data: only 7/17 scored "breakout" before the fix.

**Fix:** added a "Volume Shocker" override — a confirmed ≥5% move with
RVOL ≥2x and price above previous close now tags as `breakout` on its own,
independent of 52W distance or order-book data. Existing scoring is
untouched. Env-tunable: `SURPRISE_SHOCKER_MIN_CHANGE_PCT` (default 5.0),
`SURPRISE_SHOCKER_MIN_RVOL` (default 2.0).

Result: 14/17 now score "breakout".

## Fix 2 — silent 14-mega-cap fallback universe
**Files:** `services/api-gateway/surprise_premarket.py`,
`services/market-data-service/surprise_premarket.py`

`default_universe_from_env()` — the last-resort symbol list used when
`SURPRISE_UNIVERSE`/`SCAN_UNIVERSE` isn't set AND the live ~500-symbol
universe builder fails — silently fell back to just 14 mega-caps
(RELIANCE, TCS, HDFCBANK...). None of those are the small/mid-cap names
that actually show up as volume shockers, and nothing in the response
indicated degraded coverage.

**Fix:** replaced the 14-name list with 250 real, liquid NSE symbols,
data-derived from a year of NSE bhavcopy delivery data (ranked by median
daily turnover, ETFs excluded) — spans large, mid and small caps across
sectors. Also added a `logger.warning(...)` when this fallback activates.

## Fix 3 — is_liquid pre-filter was price-blind (NEW — found in this pass)
**Files:** `services/api-gateway/surprise_premarket.py`,
`services/market-data-service/surprise_premarket.py` (4 call sites each,
8 total)

Every baseline-computation path (`bulk_baselines_from_yfinance`,
`compute_baseline_for_symbol`, `compute_baseline_from_bhavcopy`,
`bulk_baselines_from_bhavcopy`) set `is_liquid` from **raw share volume**:
`avg_daily_vol >= 50,000 shares/day` — with no price awareness at all.
`SurpriseStockEngine.scan()` then filters its scan universe on this same
flag: `keys = [k for k,v in static_cache.items() if v.get("is_liquid")]`.

That's a sane bar for a ₹20 penny stock (50,000 shares ≈ ₹10L/day), but an
absurd one for anything mid-priced or higher: a ₹1,000+ stock trading a
genuinely liquid ₹1Cr+/day on "only" 10,000 shares was marked
`is_liquid=False` and silently dropped from the scan universe **before
`score_stock()` ever ran** — no matter how real the move was. This is
almost certainly why higher-priced names from your screenshots (Balrampur
Chini ₹721, Sundaram-Clayton ₹1,315, eClerx ₹2,022, RPG Life Sciences
₹2,641, Craftsman Automation ₹11,424, John Cockerill ₹8,299, LMW
₹19,711...) were structurally unreachable regardless of Fix 1.

Confirmed with real data: AYM SYNTEX (₹254, 42,182 avg shares/day ≈
₹1.07 Cr/day turnover) failed the old 50,000-share gate and would never
have been scanned, despite being genuinely liquid.

**Fix:** replaced the raw-share-count gate with a rupee-turnover floor —
`(avg_daily_volume × prev_close) >= LIQUID_MIN_DAILY_TURNOVER` (default
₹50L/day, the same `HARD_FLOOR_LIQUIDITY` value `score_stock()` already
uses downstream, so the pre-filter and the in-score check now agree).
Env-tunable: `SURPRISE_LIQUID_MIN_TURNOVER`.

## Minor — stale docstring (fixed)
`services/api-gateway/main.py`, `/api/surprise/scan` — docstring said
"score filter (>=60, change >1%)"; the real values (65 / 1.5%, plus the
new Volume Shocker override) had drifted since. Updated to point at the
module constants instead of hardcoding numbers that go stale again.

## Reviewed, no issue found
- `dedupe_by_symbol()` / sector-sympathy pass in `surprise_scanner.py` —
  correct, unaffected by the above.
- `directional_filter()` in `surprise_scanner.py` — defined but never
  called anywhere. Harmless dead code (all three tiers already require
  `price_change_pct > 0`, so it would be a no-op if wired in). Left as-is
  since removing/wiring it wasn't asked for and doesn't affect behavior.
- Frontend `SurpriseStocks.tsx` — trusts the backend's `tier` field with no
  client-side re-filtering by score, so it will correctly display the new
  "Volume Shocker" breakout hits. (One cosmetic note, not a bug: a
  shocker-override hit can show e.g. "40/100" next to a green "Breakout"
  badge, which may look inconsistent — say the word if you'd like the score
  badge itself adjusted for this case.)
- `buy_sniper.py` — no similar price-blind liquidity gate; it consumes an
  already-vetted stock list, so Fix 3 doesn't apply there.
- 52-week high/ATR/avg-volume lookback windows (`period="1y"`, 30-day tail)
  in all baseline functions — correct.

## Testing
`backtest_surprise_test.py` (included) replicates `score_stock()`'s scoring
logic in pure Python against `nse_bhavdata_delivery_1y.csv` — no DB/network
needed. Run it before/after applying Fix 1 to reproduce the breakout-tier
numbers above.
