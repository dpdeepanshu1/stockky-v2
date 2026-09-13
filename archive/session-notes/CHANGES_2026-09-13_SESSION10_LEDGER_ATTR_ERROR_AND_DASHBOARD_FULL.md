# Session 10 — ScalpCapitalLedger AttributeError fix + full dashboard rebuild

**Date:** 2026-09-13  
**Service:** position-stocks-service + frontend  
**Bugs fixed:** 1 | Improvements: 1

---

## Bug: AttributeError 'ScalpCapitalLedger' object has no attribute 'daily_loss_kill_switch_tripped' → 500 on GET /ledger

**Root cause:**  
`scalp_capital_ledger` was first created in an earlier deploy before
`daily_loss_kill_switch_tripped` and `daily_loss_kill_switch_tripped_date`
columns were added to the model. SQLAlchemy's `create_all()` only creates
missing *tables*, never alters existing ones. The row already existed, so
both columns were missing from Oracle — any attribute access raised
AttributeError → 500.

The session-9 fix added `_ensure_columns()` migration infrastructure but
didn't list the two new `scalp_capital_ledger` columns in `_COLUMN_MIGRATIONS`.

**Fix:** Added both entries to `_COLUMN_MIGRATIONS` in `db.py`:
```python
("scalp_capital_ledger", "daily_loss_kill_switch_tripped", "NUMBER(1)", "BOOLEAN", "0", "FALSE"),
("scalp_capital_ledger", "daily_loss_kill_switch_tripped_date", "VARCHAR2(10)", "VARCHAR(10)", None, None),
```
On next boot, `_ensure_columns()` will ALTER TABLE and add them automatically.

**Defensive patch:** `loadAll()` in the frontend now wraps `positionStocksApi.ledger()` 
in `.catch(() => null)` so a /ledger 500 no longer kills the entire dashboard poll 
(the other panels — status, positions, candidates, history — still load).

**Files changed:**
- `services/position-stocks-service/db.py`

---

## Dashboard: Full rebuild of PositionStocksTab.tsx

Added sections and detail that were missing:

**System Health panel** (new): circuit breaker state + retry countdown, WS
subscribed symbols count, last WS tick time, reconnect attempts counter,
orders placed today, first-live-order done flag, risk-per-trade confirmation status.

**Capital pool**: capital utilisation % bar (green → amber → red as pool fills), 
kill switch badge shown inline next to sync timestamp.

**Kill switch banner**: prominent red banner at the top when kill switch tripped
(checked from both `/status` and `/ledger` so it shows even if one endpoint is slow).

**Trade history**: W/L breakdown bar (proportional green/red strip) above the
trade table; symbol bolded; trigger price column in Dhan live orders table.

**Position rows**: Dhan super-order ID shown; window label next to status badge;
first-live-order tagged with 🟡 emoji for easy spotting.

**Last-refreshed timestamp**: shown in dashboard header so you can see data
freshness at a glance.

**Ledger 500 resilience**: other panels keep loading even if /ledger 500s.

**Files changed:**
- `frontend/src/components/PositionStocksTab.tsx`
