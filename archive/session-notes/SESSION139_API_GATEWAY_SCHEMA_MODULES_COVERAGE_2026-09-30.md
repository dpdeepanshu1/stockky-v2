# Session 139 — api-gateway coverage pass 7: hotpicks_schema / ipo_schema / surprise_schema (2026-09-30)

- New: `services/api-gateway/tests/test_hotpicks_schema.py` (115 tests), `test_ipo_schema.py` (84),
  `test_surprise_schema.py` (73) = 272 tests. No production code changed.
- Covered (all three): `_normalize_db_url` shapes, `_raw_url` precedence, `is_oracle` / `dialect` /
  `database_url` (env DSN, oracle URL scheme, `oracle_compat` raising, `oracle_compat` missing), `ddl_statements`,
  `table_exists_sql`, `make_engine` (Postgres kwargs, Oracle pool env overrides, missing `oracle_compat`),
  `upsert_sql` per dialect, `adapt_rows`, `ensure_*_schema` (Postgres one-transaction path, Oracle
  `exec_ddl_safe` path, blank statements skipped, engine None, failures reported + truncated to 240 chars, engine
  always disposed, dispose failure swallowed). hotpicks + surprise also: `now_func`, `shared_engine` cache (keyed by
  app name + URL, None never cached) and `dispose_shared_engines`. hotpicks also: `select_recent_sql`,
  `delete_older_than_sql`, `_clip_utf8`, `coerce_bool`, import-time byte ceilings. ipo also: the legacy
  `1900-01-01` sentinel healer and that only the Oracle path runs it.
- Drift guards (fail if someone edits one side only): each DDL's column list == `SELECT_COLUMNS`; `ROW_KEYS` +
  `updated_at` == `SELECT_COLUMNS`; every named bind in each dialect's upsert == `ROW_KEYS`; Oracle VARCHAR2 byte
  budgets == the Oracle DDL; the Oracle MERGE never updates its join key(s) (ORA-38104).
- Hermetic: every test loads a FRESH copy of the module (no `_ENGINE_CACHE` leakage) with `sqlalchemy` and
  `oracle_compat` replaced by small fakes in `sys.modules`; no network, no DB, no real sqlalchemy needed.
- Verification: this sandbox had NO pytest / pytest-cov / sqlalchemy and no network, so the real suite was not run
  here. Instead the tests were run with a throwaway pytest stand-in (fixtures, parametrize, monkeypatch, caplog)
  plus stdlib line tracing: 272 passed, 3 repeat runs identical, every executable statement of the three modules hit
  (162 + 133 + 126; `except ImportError: _oc = None` lines are `# pragma: no cover`). 27 hand-made mutants, all caught
  (one test was tightened after the first round caught 26/27). The VM run of `bash run_tests.sh --single` is the
  real confirmation: expect 1348 passed (1076 + 272), the three modules at 100%, gateway TOTAL roughly 15% -> 18%.
- Observations, left unchanged:
  * `_normalize_db_url` only collapses the `?&` left by a FIRST-position `channel_binding`. In the middle of a query
    string (`?sslmode=require&channel_binding=require&application_name=x`) it leaves `&&`
    (`...sslmode=require&&application_name=x`). Same bug class as position-stocks db.py round 14 / real-trade-service
    session97. Six copies in the gateway: hotpicks_schema, ipo_schema, surprise_schema, kv_cache, surprise_premarket,
    surprise_scanner. Not pinned by a test (that would enshrine the bug). Neon's own shape puts channel_binding last,
    so production is unaffected today.
  * surprise_schema creates `idx_surprise_static_sym` on Postgres, redundant with the PRIMARY KEY index (Oracle
    skips it for that reason). Harmless; pinned by `test_redundant_symbol_index_exists_on_postgres_only`.
  * ipo_schema's `_clip_utf8` assumes a str (or None); callers guard with `isinstance`, so it is fine today.
- Next (pass 8): Tier 4 — `hotpicks_store` (523 statements, the biggest of the group).
