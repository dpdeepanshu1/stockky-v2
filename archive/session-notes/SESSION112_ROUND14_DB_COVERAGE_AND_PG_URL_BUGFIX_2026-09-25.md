# Session 112, round 14 (2026-09-25): `db.py` coverage 16% → ~100% (position-stocks-service) + a real bug fix

Next item by priority off round 13's list (`db.py` 16%, 102 missing
lines — essentially every function: engine/session setup, every schema
migration). No test file existed at all.

## Real bug found and fixed

While diffing this module against real-trade-service's `db.py` to check
for prior fixes not yet ported (same technique that surfaced round 7/8's
`shared_exposure.py` bug), found that `_normalize_pg_url` here is missing
a fix real-trade-service's copy already has from **session97**: a
`channel_binding` param sitting in the *middle* of a query string (Neon's
own connection-string shape, e.g. `?a=1&channel_binding=require&b=2`)
left a doubled `&&` after the regex substring removal —
`?a=1&&b=2` — which libpq's URI parser rejects outright ("missing
key/value separator '=' in URI query parameter"). Real-trade-service
fixed this in session97 by collapsing repeated `&` before the
leading/trailing cleanup; that one-line fix was apparently never ported
to this service's copy of the same function. Reproduced the failure
against the pre-fix code with a standalone script (confirmed the exact
`&&` in the output), then ported the fix verbatim. This is a live risk
for this service too, since it shares the identical `DATABASE_URL`/Neon
contract.

## What was added

`tests/test_db.py` — written fresh (this module's structure is
meaningfully simpler than real-trade-service's `db.py`, which has ~18
separate `_ensure_*` column-migration functions; this service drives one
`_ensure_columns` off a `_COLUMN_MIGRATIONS` data table instead), though
it borrows the established real-SQLite-plus-statement-recorder technique
real-trade-service's own `tests/test_db.py` uses for the Postgres/SQLite
path, and a small fake-connection recorder for the Oracle-only DDL that
doesn't exist on SQLite.

Covers: `_normalize_pg_url` (every case real-trade-service's tests pin,
including the regression fixed above — this test fails against the
pre-fix code), `dialect`/`get_engine`/`get_session_factory`/`get_db`
(Oracle detection by URL scheme and by `ORACLE_DSN` env var, engine/
factory caching — `create_engine` called exactly once across repeat
calls, the no-`DATABASE_URL` result is explicitly NOT cached so a later
successful config still works, `get_db` raises `RuntimeError` when
unconfigured and always closes its session including when the caller
raises inside the `with` block), `init_tables` (no-engine early return,
the wiring order `create_all` → `_ensure_columns` → [Oracle only]
`_ensure_oracle_autoincrement` → `_ensure_hot_path_indexes`, confirming
the Oracle step is skipped entirely on Postgres), `_ensure_columns`
(missing table skipped, existing column no-op, missing column with a
default added `NOT NULL DEFAULT`, missing column with no default added
nullable, a failed `ALTER` logged without aborting the rest of the list,
`inspect().has_table` itself raising caught per-entry), a **drift guard**
over `_COLUMN_MIGRATIONS` itself (no entry mixes a `None` Oracle default
with a non-`None` Postgres default or vice versa — the module's own
`nullable = oracle_default is None and pg_default is None` check silently
assumes this never happens; also no duplicate `(table, column)` entries),
`_ensure_hot_path_indexes` (creates the documented index, safe to call
twice), and `_ensure_oracle_autoincrement` (skips a non-`id`-PK table,
skips a table that already has identity, computes `SEQUENCE` start from
`MAX(id)+1`, falls back to 1 when that query fails, creates the
`SEQUENCE`+`TRIGGER` for a table that needs it, and warns-but-continues
when either the identity check or the trigger DDL itself fails).

## Verification

`sqlalchemy` isn't installed in this sandbox (no network), so the actual
pytest file — which needs real SQLite via SQLAlchemy for several of its
tests — wasn't run here. `_normalize_pg_url` needed no stubbing at all
(pure string/regex logic): ran all 11 cases directly against the real,
modified function, confirming the fix and that every pre-existing case
still passes. For the DB-touching functions, stubbed a minimal
`sqlalchemy`/`sqlalchemy.orm` (just enough surface to import the real,
unmodified `db.py`) and drove `_ensure_oracle_autoincrement`,
`_ensure_columns`, `dialect()`, and the `_COLUMN_MIGRATIONS` drift guard
directly with hand-built fake connections/engines mirroring the test
file's own fakes — 13 additional checks, 13/13 passed (one of my own
fake's bugs was caught and fixed along the way: a case-sensitivity typo
comparing `"MAX(id)"` against an upper-cased string, unrelated to the
real module). `get_engine`/`get_session_factory`/`get_db`/`init_tables`
and the SQLite-`MetaData`-based `_ensure_columns` tests need a real engine
and weren't separately re-verified beyond `py_compile`+`ast` — flagging
that the VM's actual pytest run is what confirms those.

## Next by priority (unchanged from round 13's list, minus this item)

`execution/dhan_client.py` 21%, `feed/*`, and `main.py`.
