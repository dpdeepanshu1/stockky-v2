# Session 33 — DB Performance Audit (Oracle Autonomous AI Database, Always Free)

**Requested:** "check all db related code and fix it, it taking lot more
time in load, I use oracle Autonomous AI Databases (Always Free tier)"

## Root causes found and fixed: two missing indexes on the hottest queries

Oracle's `create_all()` (used by both `db.py::init_schema`/`init_tables`)
only ever creates an index on a table it is ALSO creating for the first
time. This DB was provisioned months ago — any `Index(...)` added to
`models.py` since then has never actually been applied to the live table,
the same class of gap `_ensure_columns`/`_ensure_*_columns` already exist
to fix for missing columns. Two of the system's most-executed queries had
no supporting index at all and have been full table scans since day one,
getting slower every session purely from data volume (neither table is
ever pruned):

1. **`trade_orders`** (real-trade-service) had NO index beyond its PK.
   `GET /orders/{mode}` (`main.py::list_orders`) filters on `mode` + a
   `created_at` range and sorts by `created_at` — and this fires on
   *every* dashboard poll of the Orders tab (and via `_self_heal_orders`,
   on every Positions-tab poll too). Added
   `ix_trade_orders_mode_created (mode, created_at)`.

2. **`trade_candidates`** (real-trade-service) only had `(mode, symbol)`.
   `entry_engine.evaluate_mode`'s `.filter_by(mode=mode,
   consumed=False).order_by(received_at.asc())` is arguably the single
   most-executed query in the entire system — it runs every auto-pilot
   tick (every `AUTO_PILOT_INTERVAL_SECONDS`, default 180s, all day,
   forever) with zero index support for either the `consumed=False`
   filter or the `received_at` sort. Added
   `ix_trade_candidates_mode_consumed_recv (mode, consumed, received_at)`.

3. **`scalp_positions`** (position-stocks-service) had no index on
   `opened_at`. `GET /positions`'s unfiltered "last 50" query
   (`main.py`) sorts the whole table by `opened_at` with no index. Added
   `ix_scalp_positions_opened_at`.

## How the fix is applied to your already-running DB
Added a migration function to each service's `db.py`
(`_ensure_missing_indexes` / `_ensure_indexes`) that runs
`CREATE INDEX` idempotently on every boot — same "swallow already-exists"
idiom `oracle_compat.exec_ddl_safe` already uses elsewhere in this
codebase. **These apply automatically the next time each service
restarts/redeploys — no manual SQL needed.** Also added the same
`Index(...)` declarations to `models.py` directly so a brand-new
deployment gets them from `create_all()` on first boot too. Oracle
Autonomous DB builds indexes online by default — no downtime, no table
lock.

## Other DB-layer things checked (real, but not code bugs — configuration
to be aware of)

- **Connection count vs. Always Free's session cap.** Always Free
  Autonomous AI Database instances cap out at **30 simultaneous database
  sessions** total (Oracle's own docs — this is a hard platform limit,
  not something the app can raise). Every Stockky service that talks to
  Oracle defaults to `DB_POOL_SIZE=3` + `DB_MAX_OVERFLOW=2` (5 possible
  connections each) via `oracle_compat.py`'s defaults — with ~7 services
  (api-gateway, decision-prediction-service, notification-scheduler-service,
  real-trade-service, analysis-intelligence-service, market-data-service,
  position-stocks-service) all pointed at the same "Stockky-DB" instance,
  that's up to ~35 possible concurrent connections just from steady
  pooling, before background threads (auto-pilot loop, reconcile-on-
  to_thread, etc.) are counted — i.e. it's plausible to occasionally brush
  up against the 30-session ceiling under load, which shows up as
  connection waits/errors that look exactly like "the dashboard is slow."
  **Not changed this session** (tuning pool sizes per-service without
  knowing your actual peak concurrent load risks under-provisioning a
  live-trading service) — but worth checking directly: connect as ADMIN
  and run `SELECT status, COUNT(*) FROM v$session GROUP BY status;` next
  time it feels slow, to see how close you are to the cap. If you're
  regularly near it, lowering `DB_POOL_SIZE`/`DB_MAX_OVERFLOW` on the
  less latency-sensitive services (notification-scheduler,
  decision-prediction) is the safer lever than raising anything.
- **`_self_heal_orders`** (real-trade-service `main.py`) runs a full
  fill-check/expire/reconcile pass on *every* `GET /positions/{mode}` and
  `GET /orders/{mode}` call, not just on a manual Run Cycle — intentional
  by design (documented in its own docstring, session 21/32's own
  comments), but it does mean every dashboard poll pays for that extra
  DB work (and, in REAL mode, a live Dhan round trip) on top of the
  query itself. Left as-is — this is a correctness/freshness tradeoff,
  not a bug, and changing it risks positions/orders looking stale.
- `call_timeout` (both services, `oracle_compat.py`) is already bounded
  at 8s per Oracle round trip — confirmed this was already fixed
  (2026-09-12) and is not itself a source of the slowdown, just a ceiling
  on how bad a single stalled call can get.
- Every other model/query pattern in both services (`ScalpGateState`,
  `ScalpCapitalLedger`, `TradePosition`, `TradeAuditLog`, `WatchlistEntry`,
  `SharedOrderBudget`, etc.) already has adequate index coverage for how
  it's actually queried — checked each one against its real call site,
  no further changes needed.

## Verification
`python3 -m py_compile` clean on `db.py`, `models.py`, `main.py` (both
services) and `entry_engine/entry.py`. Index-migration functions follow
the exact idempotent pattern (`oracle_compat.exec_ddl_safe`, "already
exists" swallowing) already proven in this codebase's column-migration
functions — safe to run repeatedly, safe on a table that already has the
index from a previous boot.
