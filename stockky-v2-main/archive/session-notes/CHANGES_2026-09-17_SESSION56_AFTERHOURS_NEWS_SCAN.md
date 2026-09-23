# Session 56 — After-Hours News Scan → Next-Day Watchlist

**Date:** 2026-09-17  
**Feature:** After-hours RSS news scan feeding NextDayWatchlistEntry → _prepick

---

## What Was Built

### 1. `models.py` — New table + gate column

**`NextDayWatchlistEntry`** (`trade_nextday_watchlist`):
- Separate from `WatchlistEntry` (intraday, same-day decay)
- Persists overnight until _prepick consumes it
- Columns: `mode, symbol, catalyst_type, catalyst_source, headline, priority_score, market_date, collected_at, consumed, consumed_at, updated_at`
- One row per `(mode, symbol, market_date)` — upsert keeps highest `priority_score`
- Indexes: `ix_nextday_watchlist_mode_date_consumed`, `ix_nextday_watchlist_mode_sym_date`

**`TradeGateState`** — two new columns:
- `afterhours_news_scan_enabled` (Boolean, DEFAULT False)
- `afterhours_news_scan_enabled_at` (DateTime, nullable)

### 2. `db.py` — Migrations

Two new migration functions called from `init_schema()`:
- `_ensure_afterhours_gate_column()` — adds `afterhours_news_scan_enabled/at` to `trade_gate_state` on existing DBs
- `_ensure_nextday_watchlist_indexes()` — ensures indexes on the new table (especially important for Oracle)

### 3. `watchlist_engine/afterhours_scan.py` — New module

**`run_afterhours_scan(db, mode, market_date)`**:
- Fetches 4 RSS feeds: Moneycontrol, LiveMint, ET-markets, ET-companies
- Extracts symbol from headline (prefers known NSE symbols whitelist)
- Classifies via `event_depth_local.classify_text` (same as Tier 2 watchlist)
- Scores 0–100: base score by catalyst_type + source_bonus + keyword_bonus
- Positive-keyword filter: headline must contain at least one positive word (profit, results, growth, etc.) — no purely negative/irrelevant news
- Upserts to `NextDayWatchlistEntry`, one row per symbol, best score wins

**`finalize_nextday_watchlist(db, mode, market_date)`**:
- Called once at AFTERHOURS_FINALIZE_TIME_IST (default 08:45)
- Trims to top-N (AFTERHOURS_SCAN_MAX_NEXTDAY_CANDIDATES = 8)
- Marks the rest consumed (discarded)
- Returns shortlisted symbols for Telegram notification

### 4. `config.py` — New config block

```
AFTERHOURS_SCAN_START_IST              = "15:45"   (env: AFTERHOURS_SCAN_START_IST)
AFTERHOURS_SCAN_END_IST                = "08:45"   (env: AFTERHOURS_SCAN_END_IST)
AFTERHOURS_FINALIZE_TIME_IST           = "08:45"   (env: AFTERHOURS_FINALIZE_TIME_IST)
AFTERHOURS_SCAN_INTERVAL_SECONDS       = 3600      (env: AFTERHOURS_SCAN_INTERVAL_SECONDS, floor 300)
AFTERHOURS_SCAN_MAX_NEXTDAY_CANDIDATES = 8         (env: AFTERHOURS_SCAN_MAX_NEXTDAY_CANDIDATES)
AFTERHOURS_SCAN_MIN_INJECT_SCORE       = 30.0      (env: AFTERHOURS_SCAN_MIN_INJECT_SCORE)
```

### 5. `execution/auto_pilot.py` — Loop + _prepick injection

**`_afterhours_scan_loop()`** (new background task):
- Same pattern as `_totp_refresh_loop` — always started, checks window/toggle internally
- Active window: 15:45–08:45 IST (spans midnight, checked via `_is_afterhours_window_active()`)
- Finalize pass fires once per day at/after AFTERHOURS_FINALIZE_TIME_IST, before pre-market
- Task registered in `start()` as `_afterhours_task`

**`_inject_nextday_watchlist_candidates(db, mode)`** (new helper):
- Called from `_prepick` before the US-sector signal
- Reads today's `NextDayWatchlistEntry` rows (market_date == today, consumed=False)
- Injects each as a `TradeCandidate` with `overnight_priority=True, source_tab="afterhours_news_scan"`
- Skips rows below `config.AFTERHOURS_SCAN_MIN_INJECT_SCORE` or already queued
- Marks all rows consumed regardless (prevents double-inject on re-run)
- Count added to `overnight_added` in `_prepick` so Telegram alert shows them with 🌙 tag

### 6. `main.py` — Toggle + status

- `"afterhours_news_scan"` added to `_FEATURE_COLUMNS` map → exposed via `POST /features/{mode}`
- Appears in `GET /status/{mode}` under `scheduled_automation.afterhours_news_scan`

---

## Hard Constraints (unchanged)

- **No pre-open call-auction orders** (9:00–9:08 NSE auction) — Dhan's `dhan_client.py` only supports MARKET/LIMIT for continuous trading. "9:10 entry" means the list is finalized by 08:45, ready to fire MARKET/LIMIT at 9:15+ open.
- **No `retrain-model.yml` changes** — left exactly as-is per standing instruction.
- All injected candidates go through the full existing pipeline: same risk_engine, same ATR stop/target (`_atr_stop_target_pct`), same regime gate. Nothing is bypassed.

---

## Files Changed

| File | Change |
|------|--------|
| `services/real-trade-service/models.py` | Added `NextDayWatchlistEntry` table + `afterhours_news_scan_enabled/at` to `TradeGateState` |
| `services/real-trade-service/db.py` | Added `_ensure_afterhours_gate_column`, `_ensure_nextday_watchlist_indexes` |
| `services/real-trade-service/watchlist_engine/afterhours_scan.py` | **NEW** — RSS scan + finalize |
| `services/real-trade-service/config.py` | Added `AFTERHOURS_SCAN_*` config block |
| `services/real-trade-service/execution/auto_pilot.py` | Added `_afterhours_scan_loop`, `_inject_nextday_watchlist_candidates`, wired into `_prepick` + `start()` |
| `services/real-trade-service/main.py` | Registered toggle + status fields |
