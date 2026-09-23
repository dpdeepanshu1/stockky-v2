# Stockky v2 — Fixes Applied (30-Aug-2026 session)

Applied against `STOCKKY_ISSUE_LOG_AND_FIXES.md` + the extra IPO-DB bug found
in this session's docker logs + screenshots. Full files are included in this
zip at their real paths; this doc is just a reading guide to what changed
and why.

---

## Issue 0 — AngelOne `_running` crash
**Status:** Already fixed in your uploaded code (`global _running` present
in `angelone_ws_feed.py`'s `_run()`). No change needed.

## Issue 1 — Candidate filter misses momentum breakouts
**Decision applied: Option A** (second, independent "momentum breakout"
track — the existing MANUAL/AUTO flow is untouched).

**File:** `services/real-trade-service/candidate_engine/candidates.py`

- Added `VOLUME_SHOCK_MULTIPLIER` (default 3.0x) / `VOLUME_SHOCK_MIN_RETURN_PCT`
  (default 5.0%) constants, both env-overridable.
- Added `"volume_shock": "/scan/universe"` to `_SOURCES`.
- Added `_rows_from_volume_shock()` — pulls api-gateway's already-computed
  `momentum_movers` list, no score gating (there is no score on that
  payload; the gate lives entirely in the analysis function below).
- Added `_volume_shock_analysis()` — skips `6m_downtrend`/`weak_MTF`
  entirely; requires today's volume ≥ multiplier × 20-day average AND
  today's return ≥ threshold; still enforces price floor + ATR cap
  (position-safety, not a market-view call). Liquidity floor is enforced
  downstream by `risk_engine`'s existing `hard_floor_liquidity` check at
  order time, so it isn't duplicated here.
- Split `refresh_candidates()` into `_refresh_standard_candidates()`
  (byte-for-byte the original logic, just extracted into its own function)
  and `_refresh_volume_shock_candidates()` (new). Both run every cycle;
  the volume-shock track excludes any symbol the standard track already
  saw (inserted or rejected) so nothing is proposed twice under two
  different `source_tab`s in one cycle.

Re-run `backtest_candidates.py` against this file's new constants before
trusting it live, per the issue log's own instruction.

## Issue 2 — Live feed frozen on static 14-symbol list
**Files:**
- `services/market-data-service/angelone_ws_feed.py` — added
  `stop_feed_background()`.
- `services/market-data-service/main.py` — added `import asyncio` (was
  missing) + `_refresh_feed_universe_loop()` background task, started from
  a new `@app.on_event("startup")` hook, re-pointing both WS feeds at
  api-gateway's `/scan/universe` every `FEED_UNIVERSE_REFRESH_INTERVAL_S`
  (default 900s / 15 min).

## Issue 3 — risk_engine SELL-bypass docstring/code mismatch
**File:** `services/real-trade-service/risk_engine/engine.py`

- Checks 1 (`global_pause`), 3 (`daily_loss_limit`), 8 (`stale_market_data`)
  now gated to `intent.side == "BUY"` — matching every other BUY-only check
  in the file and the codebase-wide "SELL must never be blocked" policy.
- Module docstring and `evaluate()`'s own docstring now state the same,
  correct list: SELL bypasses everything except #2 (market hours) and #9
  (abnormal volatility).

## Issue 4 — exit_engine
No bug found in the original review; no change made.

---

## Extra bug found this session — IPO Tracker "always shows 0 in DB"

Not in the original issue log — found by tracing your docker-compose log
(`ORA-01858: A non-numeric character was found...` /
`ipo_static_feed: upserted 0/278 rows`) against the code, and confirmed
against your screenshots (`IPO Database Feed Health` panel permanently
stuck at `HEALTH SCORE 0% / TOTAL TRACKED 0 / FULLY SCORED 0`, and the
Auto-Repair / Full Re-scan buttons never appearing because they're
conditionally hidden while `total_tracked === 0`).

**Root cause — two compounding bugs:**

1. `_normalize_nse_row()` (`services/api-gateway/ipo_scanner.py`) stored
   NSE's raw `listingDate` string (e.g. `"06-AUG-2026"`) straight through,
   never converting it to ISO. The Oracle upsert in `ipo_schema.py` does
   `TO_DATE(SUBSTR(:listing_date,1,10),'YYYY-MM-DD')`, which requires ISO
   input — anything else raises exactly `ORA-01858`.
2. `_ipo_db_upsert()`'s Oracle branch executed every row inside **one**
   shared `eng.begin()` transaction. The moment row N raised `ORA-01858`,
   the exception propagated out of the whole `for row in payload:` loop,
   and the entire transaction — all 278 rows, not just the bad one — rolled
   back. That's the exact mechanism behind `upserted 0/278 rows`, and why
   the DB has shown 0 rows on every single scan, not just some.

**Fix — `services/api-gateway/ipo_scanner.py`:**

- `_normalize_nse_row()`: `listing_date` is now parsed with the existing
  `_parse_date()` helper (which already handles NSE's `%d-%b-%Y` format)
  and re-emitted as ISO (`YYYY-MM-DD`) immediately at the source, before
  it's used anywhere else in the function (including the `stage`
  computation).
- New `_iso_listing_date()` helper + used inside `_ipo_db_upsert()` as a
  second, defense-in-depth normalization pass — this covers rows that
  reach the DB writer from `ipoalerts` or hand-typed `add_manual_ipo()`
  calls too, not just NSE auto-scan rows, so the writer never sees a
  non-ISO date regardless of source.
- `_ipo_db_upsert()`'s Oracle branch: each row now gets **its own**
  `eng.begin()` transaction instead of one shared transaction for the
  whole batch. A row that still somehow fails is now logged and skipped —
  it can never again zero out rows that already succeeded.

No DDL/schema change needed — `ipo_static_feed.listing_date` was always a
proper `DATE` column on both dialects; only the bind value going into it
was wrong.

**Frontend — spinners:**

`IpoTracker.tsx` had zero `animate-spin` usage anywhere, while every other
panel in the app (`ScanPanel`, `DecisionCard`, `BuySniperModal`,
`StockChart`) uses a small spinning-ring `<span>` next to a busy button's
label. Added a shared `BusySpinner` component and wired it into:
- Premarket Feed / Scan IPOs (DB) / Force Scan (upstream) — top toolbar
- 🎯 Scan for Buy → (per IPO row)
- Auto-Repair All / Full Re-scan / Refresh Audit — in `IpoFeedHealth.tsx`

The "repair button never shows" complaint was a symptom of the same DB
bug (the button is conditionally rendered only when `total_tracked > 0`
in `IpoFeedHealth.tsx`) — once the write path above actually persists
rows, the panel's counts stop being 0 and both Auto-Repair and Full
Re-scan buttons appear normally. No change was needed to that
conditional; it was working correctly against wrong (empty) data.

---

## What to verify after deploying

1. Redeploy `api-gateway`, `market-data-service`, `real-trade-service`.
2. Trigger an IPO scan → check logs for `ipo_static_feed: upserted N/N
   rows` with N matching the scanned count (not `0/N` anymore).
3. Open IPO Tracker → IPO Database Feed Health should show a non-zero
   `TOTAL TRACKED` and a real `HEALTH SCORE`.
4. Watch `market-data-service` logs after ~15 min for `feed universe
   refreshed: N symbols` where N is close to your real scan-universe size,
   not 14.
5. Re-run `backtest_candidates.py` against the new `candidates.py`
   constants; check `real-trade-service` logs for
   `candidate_engine: volume_shock inserted=... skipped=...` entries
   during/after a volume-shock session.
