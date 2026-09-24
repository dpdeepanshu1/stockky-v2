# Session 97 (2026-09-24): db.py — 7% → 100%, a migration drift guard, one production fix

## Where this started

The VM run you pasted confirmed session96: **1972 passed, 1 xfailed, 94%**,
`auth/dhan_credentials.py` 100%. Next item on the plan: `db.py` (521 stmts,
7%, 482 missed) — the engine/session factory plus all 22 additive schema
migrations. `create_all(checkfirst=True)` only creates *missing tables*, so any
column added to `models.py` after a table's first deploy exists **only** if an
`_ensure_*` function here adds it; if one is missing, every ORM query on that
table dies with "no such column" / ORA-00904 / UndefinedColumn (the session-11
class of bug in position-stocks-service). Nothing in the suite had ever run
any of it.

## Production fix (1, low severity)

`_normalize_pg_url()` — a `channel_binding` parameter in the **middle** of the
query string (`?a=1&channel_binding=require&b=2`) left `a=1&&b=2`. libpq's URI
parser rejects that (`psycopg2.extensions.parse_dsn` →
`missing key/value separator "=" in URI query parameter`). Neon's default
string has `channel_binding` last, which is why it never bit; any other order
would have crashed engine creation at boot. Fixed by collapsing `&&` before the
existing leading/trailing cleanup. Regression tests fail on the old code (3
failures, including one that hands the result to libpq).

## What was added

**`tests/test_db.py` — 217 tests, real SQLite engines, `db.py` 100% (521/521).**
The technique: build a genuine *legacy* schema (every model column any
migration adds is removed, plus all model-level indexes), run the real
functions on it, and inspect the result. A before-cursor-execute hook records
every statement, so the **Oracle branch — whose SQL SQLite rejects — is still
captured and checked** even though it can't execute.

- `_normalize_pg_url`, `dialect`, `get_engine` (oracle vs postgres, 4+4 pool,
  connect args, caching, no-DB → `None` **not cached**), `get_session_factory`,
  `get_db` (session always closed, incl. when the handler raises).
- `init_schema`: every `_ensure_*`/fixup is wired in (a new migration function
  nobody calls is a silent no-op — this now fails a test), each runs exactly
  once with the right dialect, the backfill runs **after** the column it reads
  exists, Oracle-only autoincrement step runs first, and a full
  legacy→current upgrade through `init_schema()` itself.
- The 17 column migrations, executed for real: each adds exactly its own
  columns and issues zero ALTERs on the second boot; after all of them the
  schema equals `models.py` (same column set, same NOT NULL flags) and the ORM
  can SELECT every mapped table; **pre-existing rows survive and read back with
  the same defaults a freshly inserted row would get** (e.g. a legacy REAL gate
  still has `overnight_hold_enabled=True`, `edis_morning_check_enabled=True`).
  A control test proves the ORM really does break on the un-upgraded schema.
- DDL-vs-model invariants, both dialects, from the *recorded* SQL: same
  (table, column) set per function on Postgres and Oracle; `NUMBER(1)`↔Boolean,
  `VARCHAR(n)`/`VARCHAR2(n)` lengths equal the model's `String(n)`; every
  `NOT NULL` add has a `DEFAULT` (otherwise the ALTER fails on a non-empty
  table); the DEFAULT equals the model's default. **All currently consistent** —
  I looked for drift and found none.
- Failure handling for every column function: `inspect()` failure warns and
  skips; "already exists" and `ORA-01430` (including a non-English NLS message
  where only the ORA code identifies it) are swallowed silently; any other
  error warns and the remaining columns are still attempted.
- Index ensurers (real `CREATE INDEX IF NOT EXISTS`; names and column order must
  equal the model's own `Index` objects; Oracle SQL has no `IF NOT EXISTS`;
  ORA-00955/ORA-01408 swallowed), `_backfill_broker_imported_flag` (real UPDATE
  semantics on both dialect variants: only OPENED + "Imported from Dhan demat
  holdings…" rows flip, already-True and other rows untouched, idempotent),
  `_fix_stale_dhan_token_expiry` (SQL per dialect, only ever shortens, logging),
  `_ensure_oracle_autoincrement` (fake Oracle engine: non-`id` PK tables
  skipped, identity tables untouched, sequence starts at MAX(id)+1 or 1,
  ORA-00955 swallowed, trigger DDL exact, failures warn and continue).

**Drift guard (the future-proofing).** `BASELINE` in the test is a frozen
snapshot of every model column that is *not* added by a migration (what
`create_all()` produced on first deploy). A model column that is in neither
BASELINE nor a migration now fails a test whose message says exactly what to
do: *add an `_ensure_*` in db.py, call it from `init_schema`, list it in
`COLUMN_FUNCS`*. New **tables** need nothing (`create_all` makes them whole).
A self-test proves the guard fires. Caveat: the snapshot assumes today's
models are complete — supported by the session21c audit and a scan of every
model column carrying an "added/session/date" comment (all migrated).

**`tests/test_db_postgres_live.py` — 5 tests, optional.** Uses the embedded
`pgserver` package to run the Postgres-branch SQL against a **real PostgreSQL**:
fresh-DB init, a legacy DB upgraded with **zero warnings** (this includes
`INTERVAL '24 hours'`, which SQLite cannot run), the stale-token cap on real
rows (30-day row → 24h; a 5h row, an exactly-24h row, NULLs untouched;
idempotent), the broker-imported backfill with real `TRUE/FALSE`, and
`CREATE INDEX IF NOT EXISTS`. **Skipped automatically when `pgserver` isn't
installed**, so the VM run shows `1 skipped`. To run it there:
`pip install pgserver && python3 -m pytest tests/test_db_postgres_live.py -q`.
The Oracle branch is the one thing that can't be executed outside Oracle —
it's covered statically (above), not live.

## Verification

- VM-equivalent full run (pgserver hidden): **2189 passed, 1 skipped,
  1 xfailed**, overall **94% → 96%**, `db.py` **100%**, `tests/test_db.py`
  100%. With pgserver installed: 2194 passed (the 5 live tests run).
- **Mutation-checked**: 60 deliberate regressions to `db.py` (pool sizes,
  `pool_pre_ping`, sslmode handling, scheme rewrite, the `&&` fix, cache
  removal, `get_db` not closing, dropped `init_schema` calls and reordering,
  swallowed-error clauses, a typo'd column name, wrong VARCHAR length, wrong
  type, NOT NULL without DEFAULT, DEFAULT flipped vs the model, Oracle column
  dropped / NUMBER(1)→NUMBER(5) / VARCHAR2 length / default flipped, index
  column order and name, `IF NOT EXISTS` on the wrong dialect, backfill
  TRUE→FALSE / dropped event-type / dropped flag guard / oracle 1→0, stale-fix
  `>`→`<` and wrong interval syntax, every autoincrement branch, …).
  **0 survivors.** One first-run survivor was a weak test, not an equivalent
  mutant: the `ORA-01430` clause was masked because the English Oracle message
  also contains "already exists" — added a non-English-message case.

## Observations (not changed)

1. **`notifier.py` coverage is incidental and unstable — not a regression.** It
   read 52% (session94) → 48% (session95 run) → 23% (session96 run) with no
   change to `notifier.py`; I reproduced 23% in a clean sandbox both with and
   without this session's tests. It has **no direct tests**: the covered lines
   come from whichever unmocked `notify_*` calls happen to execute in other
   tests. Next candidate — a direct test file gives a stable 100%.
2. **`exec_ddl_safe` hides real DDL errors.** Anything other than "already
   exists" is logged at DEBUG only, and `_ensure_hot_path_indexes` /
   `_ensure_nextday_watchlist_indexes` then log "ensured index …" regardless.
   A typo'd index column would create no index and say it did. Pinned by a test;
   `oracle_compat.py` is shared, so left alone.
3. **Upgraded databases lack the FK on `watchlist_entry_id`.** The models
   declare `ForeignKey("trade_watchlist.id")` on trade_candidates / orders /
   positions, but the migration adds a bare `INTEGER` / `NUMBER(10)`. Fresh DBs
   have the constraint, upgraded ones don't. Harmless (nullable, nothing
   depends on the cascade); noted because "fresh == upgraded" is otherwise true.
4. Oracle sequence/trigger names for `stockky_shared_order_budget`,
   `stockky_shared_symbol_lock`, `trade_adaptive_metric_history` are 33–36
   chars, and one index name is 38. Fine on Autonomous (identifiers up to 128
   bytes since 12.2); would fail on a pre-12.2 Oracle. Asserted ≤128 in a test.
5. Carried over (observability only): `pipeline_status` stage timings
   misattributed since session48b; `json.dumps` outside `save_snapshot`'s `try`.
6. Carried over from session96 (unchanged): missing encryption key raises
   instead of failing closed; no-token-field TOTP response isn't Telegrammed;
   `DHAN_PIN` unvalidated; a past `expiryTime` stored as-is; AngelOne login
   `except` blocks not audited for secret leakage.

## Still open, in priority order

1. `notifier.py` (23%, unstable) — small, high fan-out, needs direct tests
2. `market_feed/feed.py` (66%), `entry_engine/entry.py` (84%)
3. small modules: `symbol_master.py` 29%, `shared_adaptive.py` 27%,
   `boot_forensics.py` 18%, `admin_auth.py` 61%, `shared_exposure.py` 76%,
   `event_depth_local.py` 40%, `tz_utils.py` 80%, `pipeline_status.py` 91%,
   `config.py` 90%
4. `offline_test_harness.py` (288 stmts, 0%) is a dev harness — consider
   excluding it from coverage.
