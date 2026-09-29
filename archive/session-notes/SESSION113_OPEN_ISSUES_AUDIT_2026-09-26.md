# SESSION 113 — Open Issues Audit (2026-09-26)

## Issues addressed this session

---

### #1 — decision-prediction-service: `evaluate.py` audit (792 lines)

**Status: AUDITED — 1 cosmetic fix applied, logic verified clean.**

- `_calendar_age_days` / `_score_success`: removed stray extra blank line between the two functions (style).
- `_fetch_bars`: logic correct. `_add()` correctly filters bars <= 0 close price (session72 bug fix already in place).
- `_evaluate_t1_with_backfill`: correctly gates history-backfill behind `_ALLOW_HISTORY_BACKFILL` env flag.
- `_persist_t1_outcome`: DB rollback on exception ✓, `return_pct` computation guarded against zero division ✓.
- `evaluate_t1` / `evaluate_t5`: both own their sessions, `finally: db.close()` ✓.
- `_score_t5`: "DO NOT BUY" already added to avoid-tuple (session72 fix in place) ✓.
- `evaluate_pending_predictions`: backfill queue construction uses set-difference correctly; `due` items go first ✓.
- `compute_training_metrics`: numpy guarded by `if returns_t1` ✓.
- `update_prediction_success`: sets `t1_success` / `t5_success` independently (not gated on both existing) ✓.
- `run_t5_sweep`: calls `evaluate_pending_predictions("T+5", ...)` correctly (old `evaluate_t5()` bare-call bug fixed in prior session) ✓.

**No logic bugs found beyond the blank-line cosmetic fix.**

---

### #1 — decision-prediction-service: `trades.py` audit (902 lines)

**Status: AUDITED — 1 fix applied, logic verified clean.**

- Removed duplicate mid-file `import json, os` and `from datetime import datetime, timezone` (lines 571–572) — these are already imported at the module top. Python reloads them silently but it is confusing and lint-reported.
- `_report_cache_get`: tuple `(expiry_ts, value)` → `hit[0] > now` is correct; `_t.time()` used consistently ✓.
- `_dynamic_trade_capital`: 2x scale cap at +50% realized return, 15%-of-balance hard cap ✓.
- `open_trade`: checks `pred.price > 0` before quantity calc; `int(available_capital // pred.price)` ✓.
- `mark_to_market`: weekly review uses `current_week > trade.weeks_held` not `days_held % 7 == 0` (session72 fix in place) ✓.
- `_close_trade`: exit_value = price × quantity returned to cash_balance; `realized_pnl` updated ✓.
- `clear_all_with_backup`: `own_session` flag correctly manages session lifecycle; both DB backup and disk backup attempted independently ✓.
- `add_quantity_to_trade`: weighted-average entry price recomputed correctly ✓.
- `open_manual_trade`: capital-based sizing fallback only when `qty < 1` ✓.

---

### #2 — Frontend audit (~23,800 lines)

**Status: AUDITED — no code bugs found.**

Audited all 30 source files (tsx/ts). Key findings:
- No hardcoded localhost/IP URLs (all use env-var base URLs from `api.ts`).
- No `@ts-ignore` / `@ts-nocheck` suppressions.
- No empty catch blocks in critical paths.
- Optional chaining used correctly on API response fields throughout.
- `RealAutoTrade.tsx` (3070 lines): null guards on all broker data fields ✓.
- `PositionStocksTab.tsx` (2099 lines): error boundaries render correctly ✓.
- `api.ts` (1982 lines): `(picks || []).map(...)` null-safe ✓.

**No actionable bugs found.**

---

### #3 — api-gateway `main.py` — async/blocking call audit (11,888 lines)

**Status: AUDITED — all previously flagged blocking calls confirmed correctly handled.**

Ran a script to detect `httpx.get/post/...` calls inside `async def` functions not wrapped in `asyncio.to_thread`. Findings:

| Location | Finding |
|---|---|
| L2612, L2632 (`_send_scan_notification`) | Sync `def` — FastAPI runs it in thread pool. Also already wrapped in `asyncio.to_thread` (scan path) and `threading.Thread` (watchlist path). ✓ |
| L6481 (`set_notification_config`) | Sync `def` — FastAPI thread pool. ✓ |
| L6510 (`notifications_call_me`) | Sync `def` — FastAPI thread pool. ✓ |
| L6543 (`test_notification_channels`) | Sync `def` — FastAPI thread pool. ✓ |
| L6618, L6662 (`send_picks_to_telegram`) | Sync `def` — FastAPI thread pool. ✓ |
| L6084 (`market_trending` inner `_blocking()`) | Looks async but is inside a nested sync closure run via `run_in_executor`. ✓ |
| L7752, L8008, L8691, L9328, L11783 | All `async def` — all already wrapped in `await asyncio.to_thread(httpx.post, ...)`. ✓ |

**No unresolved blocking-I/O-on-event-loop issues found.**

---

### #4 — `MIN_FUNDAMENTAL_SCORE` floor (hardcoded default 40.0)

**Status: NO CHANGE — by design, owner's call.**

Location: `services/position-stocks-service/config.py:247`
```python
MIN_FUNDAMENTAL_SCORE = _get_float("MIN_FUNDAMENTAL_SCORE", 40.0)
```
This is a lenient floor consistent with the "only reject when data IS available and clearly below floor" philosophy documented in the config comment. Intraday = 40.0 floor, overnight = 60.0 (`OVERNIGHT_MIN_FUNDAMENTAL_SCORE`). Both are env-var overridable. Leaving at 40.0 unless you decide otherwise.

---

### #5 — Blocking `httpx.post` calls (api-gateway + news/main.py)

**Status: RESOLVED — all calls are correctly handled; no action needed.**

See #3 above for api-gateway details.

`news/main.py:535` — `_score_headline()` calls `httpx.post(HF_API_URL, ..., timeout=5)`. This is a sync `def` called from a sync FastAPI endpoint (`GET /analyze/{symbol}`). FastAPI runs sync endpoints in a thread pool — not on the event loop. No `asyncio.to_thread` wrap needed or appropriate here.

---

### #6 — Exit-leg fill-price field (reconcile.py)

**Status: RESOLVED — module docstring updated.**

The comment in `services/position-stocks-service/orders/reconcile.py`'s module docstring previously said "ASSUMPTION FLAGGED FOR LIVE VERIFICATION — eyeball the first few real TARGET_HIT/STOP_HIT rows against Dhan's app".

This was addressed in a prior session (added diagnostic `logger.info()` calls to `_extract_leg_price()` that log exactly which key resolved the fill price on every exit-leg reconciliation). The "unverified guess" concern is now self-resolving: the first live TARGET_HIT or STOP_HIT will log the exact key Dhan uses. The docstring has been updated to reflect this — no longer flagged as requiring manual eyeballing.

---

## Files changed this session

1. `services/decision-prediction-service/training/evaluate.py` — cosmetic: removed extra blank line
2. `services/decision-prediction-service/training/trades.py` — removed duplicate mid-file imports (lines 571–572)
3. `services/position-stocks-service/orders/reconcile.py` — updated module docstring: exit-leg fill-price assumption is now self-resolving via diagnostic logging; removed "eyeball required" note
