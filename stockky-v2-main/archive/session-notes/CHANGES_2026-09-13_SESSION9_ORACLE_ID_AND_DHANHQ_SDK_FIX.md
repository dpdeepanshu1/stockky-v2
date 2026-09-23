# Session 9 — Oracle ORA-01400 ID fix + dhanhq SDK constructor fix

**Date:** 2026-09-13  
**Service:** position-stocks-service  
**Bugs fixed:** 2

---

## Bug 1: ORA-01400 cannot insert NULL into SCALP_GATE_STATE.ID / SCALP_CAPITAL_LEDGER.ID

**Root cause:**  
`position-stocks-service/db.py`'s `init_tables()` was calling `create_all()` +
`_ensure_columns()` but was **missing** the Oracle autoincrement backfill step
that `real-trade-service/db.py` has had since 2026-09-02.

SQLAlchemy's `create_all()` emits `GENERATED AS IDENTITY` only when it
*creates* a table from scratch. If the `scalp_gate_state` / `scalp_capital_ledger`
tables were already present in the Oracle ADB (created by an older deploy or
manually), no IDENTITY clause is ever added retroactively. Every subsequent
INSERT then sends `id = NULL`, which Oracle rejects with ORA-01400.

**Fix:**  
Added `_ensure_oracle_autoincrement(engine, models.Base)` call inside
`init_tables()` after `create_all()` (Oracle path only), and added the
function itself — a verbatim port of `real-trade-service/db.py`'s equivalent.

For each `scalp_*` table with an `id` PK:
1. Queries `user_tab_identity_cols` to check if a real IDENTITY column exists.
2. If not, creates a `{table}_id_seq` SEQUENCE starting at `MAX(id)+1`.
3. Attaches a `BEFORE INSERT TRIGGER` that populates `:NEW.id` from the
   sequence when `NEW.id IS NULL`.

**Important implementation note:** uses `exec_driver_sql()` (not `text()`) for
the trigger DDL because Oracle trigger correlation syntax `:NEW.id` is
indistinguishable from a SQLAlchemy bind parameter — `text()` would raise
"a value is required for bind parameter 'NEW'" and silently skip the trigger.

**Files changed:**
- `services/position-stocks-service/db.py` — `init_tables()` + new `_ensure_oracle_autoincrement()`

---

## Bug 2: dhanhq.__init__() takes 2 positional arguments but 3 were given

**Root cause:**  
`dhanhq` SDK was upgraded from 2.0.2 (where the constructor is
`dhanhq(client_id, access_token)`) to 2.2.0 (where the constructor changed
to `dhanhq(DhanContext(client_id, access_token))`).

`position-stocks-service/execution/dhan_client.py`'s `_get_sdk_client()` was
still calling the old two-arg form, causing a `TypeError` on every Dhan API
call (live orders fetch, fund sync, etc.).

**Fix:**  
`_get_sdk_client()` now probes for `DhanContext` at import time:
- If `dhanhq.DhanContext` exists (SDK ≥2.1) → uses `dhanhq(DhanContext(client_id, access_token))`
- If `DhanContext` is not importable (SDK 2.0.x) → falls back to the old `dhanhq(client_id, access_token)`

This makes the code forward-compatible and backward-compatible simultaneously.

**Files changed:**
- `services/position-stocks-service/execution/dhan_client.py` — `_get_sdk_client()`
