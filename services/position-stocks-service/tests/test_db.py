"""
tests/test_db.py

Covers db.py (session112 round 14) — previously 16%, missing lines 34-41,
45-47, 52-80, 85-91, 96-103, 124-134, 258-294, 313-316, 331-393 (every
function). db.py is the engine/session factory plus every additive schema
migration this service runs on boot — the same "model added a column but
create_all() never ALTERs an existing table" bug class as session-11's
/ledger 500 (see _ensure_columns's own docstring). Nothing in the suite
exercised any of it directly.

A REAL BUG was found and fixed while writing these tests: `_normalize_pg_url`
was missing a fix real-trade-service's copy already has (session97) — a
`channel_binding` param in the MIDDLE of a query string (Neon's own
"?a=1&channel_binding=require&b=2" shape) left a doubled `&&` after the
substring removal, which libpq's URI parser rejects outright. Fixed by
porting the one-line `&{2,}` collapse from real-trade-service's version.
See db.py's own comment on the fix for detail. `TestNormalizePgUrl` below
pins this with the exact regression shape, and fails on the pre-fix code.

Structurally this module is much simpler than real-trade-service's db.py
(single `_ensure_columns` driven by a `_COLUMN_MIGRATIONS` data table,
one hot-path index, one Oracle-autoincrement backfill — no per-table
`_ensure_*` functions), so this file is written fresh rather than ported,
though it borrows the established real-SQLite-plus-statement-recorder
technique this repo's own real-trade-service/tests/test_db.py uses.

Everything runs against REAL SQLite engines (real inspect(), real ALTER
TABLE) for the Postgres/SQLite code path. The Oracle-only DDL
(_ensure_oracle_autoincrement's user_tab_identity_cols / SEQUENCE /
TRIGGER) doesn't exist on SQLite, so that path uses a small fake
connection object that records executed SQL and returns configurable
query results — same idea as real-trade-service's _FakeOracleEngine.

What is covered:
  * _normalize_pg_url — postgres:// scheme, channel_binding stripped from
    the start/middle/end of the query (incl. the bug fixed this round),
    sslmode added or left alone, case-insensitive existing sslmode.
  * dialect / get_engine / get_session_factory / get_db — oracle vs
    postgres detection (URL scheme and ORACLE_DSN env var both), engine +
    factory caching (create_engine called exactly once across repeat
    calls), no-DATABASE_URL behaviour (None from get_engine, logged
    error, NOT cached — a later successful config still works),
    get_session_factory returning None when there's no engine,
    get_db raising RuntimeError when unconfigured and yielding +
    always closing the session when configured.
  * init_tables — no-engine early return (logged error, create_all never
    called), happy path wires create_all -> _ensure_columns -> (Oracle
    only) _ensure_oracle_autoincrement -> _ensure_hot_path_indexes in
    that order, Postgres path skips the Oracle step entirely.
  * _ensure_columns — table doesn't exist yet (skipped, create_all will
    handle it), column already present (no-op, zero ALTERs), column
    missing with a default (NOT NULL DEFAULT added) and with no default
    (added nullable), a failed ALTER is caught and logged without
    aborting the remaining migrations in the list, inspector.has_table
    itself raising is caught the same way.
  * _COLUMN_MIGRATIONS drift guard — every (table, column, oracle_default,
    pg_default) tuple has oracle/pg defaults that are BOTH None or BOTH
    set (never mixed), so a table's nullability can't disagree between
    the two dialects; and no duplicate (table, column) entries.
  * _ensure_hot_path_indexes — delegates to oracle_compat's
    create_index_sql/exec_ddl_safe with the documented index name/table/
    column, safe to call twice (IF NOT EXISTS / swallowed duplicate).
  * _ensure_oracle_autoincrement — skips a table whose PK isn't `id`,
    skips a table that already has a real IDENTITY column, computes
    SEQUENCE start from MAX(id)+1 (and falls back to 1 when that query
    fails), creates the SEQUENCE + BEFORE INSERT TRIGGER for a table that
    needs it, and warns-but-continues when the identity check or the
    trigger DDL itself fails.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_db.py -q --cov=db --cov-report=term-missing
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, event, inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import config
import db
import oracle_compat as _oc

LOGGER = "position-stocks-db"


# ── fixtures / helpers ──────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_db_module(monkeypatch):
    """db.py's engine/session are module-level singletons — every test
    starts from a clean slate, and ORACLE_DSN never leaks in from the
    real environment this sandbox happens to run in."""
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.delenv("ORACLE_DSN", raising=False)
    monkeypatch.setattr(config, "DATABASE_URL", "")


def new_engine():
    """Real in-memory SQLite engine that also records every executed
    statement, so ALTER TABLE / CREATE INDEX DDL can be asserted on."""
    eng = create_engine("sqlite:///:memory:")
    eng._sql = []

    @event.listens_for(eng, "before_cursor_execute")
    def _hook(conn, cursor, statement, params, context, executemany):
        eng._sql.append(statement)

    return eng


def alters(eng):
    return [s for s in eng._sql if s.strip().upper().startswith("ALTER TABLE")]


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and r.name == LOGGER]


# ══════════════════════════════════════════════════════════════════════════
# _normalize_pg_url
# ══════════════════════════════════════════════════════════════════════════

class TestNormalizePgUrl:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("postgres://u:p@h/db", "postgresql://u:p@h/db?sslmode=require"),
            ("postgresql://u:p@h/db", "postgresql://u:p@h/db?sslmode=require"),
            ("postgresql://u:p@h/db?a=1", "postgresql://u:p@h/db?a=1&sslmode=require"),
            ("postgresql://u:p@h/db?sslmode=disable", "postgresql://u:p@h/db?sslmode=disable"),
            ("postgresql://u:p@h/db?SSLMODE=verify-full", "postgresql://u:p@h/db?SSLMODE=verify-full"),
            ("postgresql://u:p@h/db?channel_binding=require", "postgresql://u:p@h/db?sslmode=require"),
            ("postgresql://u:p@h/db?sslmode=require&channel_binding=require", "postgresql://u:p@h/db?sslmode=require"),
            ("postgresql://u:p@h/db?channel_binding=require&sslmode=require", "postgresql://u:p@h/db?sslmode=require"),
            ("postgres://u:p@h/db?channel_binding=require", "postgresql://u:p@h/db?sslmode=require"),
        ],
    )
    def test_cases(self, raw, expected):
        assert db._normalize_pg_url(raw) == expected

    @pytest.mark.parametrize(
        "raw, expected",
        [
            # REGRESSION (session112 round 14, ported fix from real-trade-
            # service's session97): used to leave "a=1&&b=2", which libpq
            # rejects ("missing key/value separator '=' in URI query
            # parameter"). FAILS on the pre-fix code.
            ("postgresql://u:p@h/db?a=1&channel_binding=require&b=2", "postgresql://u:p@h/db?a=1&b=2&sslmode=require"),
            ("postgresql://u:p@h/db?a=1&channel_binding=x&channel_binding=y&b=2", "postgresql://u:p@h/db?a=1&b=2&sslmode=require"),
        ],
    )
    def test_channel_binding_in_the_middle_leaves_no_empty_parameter(self, raw, expected):
        out = db._normalize_pg_url(raw)
        assert out == expected
        assert "&&" not in out


# ══════════════════════════════════════════════════════════════════════════
# dialect / get_engine / get_session_factory / get_db
# ══════════════════════════════════════════════════════════════════════════

class TestDialect:
    def test_postgresql_by_default(self, monkeypatch):
        monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u:p@h/db")
        assert db.dialect() == "postgresql"

    def test_oracle_by_url_scheme(self, monkeypatch):
        monkeypatch.setattr(config, "DATABASE_URL", "oracle+oracledb://u:p@h/svc")
        assert db.dialect() == "oracle"

    def test_oracle_by_env_dsn(self, monkeypatch):
        monkeypatch.setenv("ORACLE_DSN", "mydb_high")
        monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u:p@h/db")
        assert db.dialect() == "oracle"

    def test_empty_database_url_is_postgresql_not_oracle(self, monkeypatch):
        monkeypatch.setattr(config, "DATABASE_URL", "")
        assert db.dialect() == "postgresql"


class TestGetEngine:
    def test_no_database_url_returns_none_and_logs_error(self, monkeypatch, caplog):
        monkeypatch.setattr(config, "DATABASE_URL", "")
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            assert db.get_engine() is None
        assert "cannot persist anything" in caplog.text

    def test_no_database_url_result_is_not_cached(self, monkeypatch):
        """A later successful config must still work — the None result from
        an unconfigured env must never poison db._engine."""
        monkeypatch.setattr(config, "DATABASE_URL", "")
        assert db.get_engine() is None
        monkeypatch.setattr(config, "DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setattr(db, "create_engine", lambda *a, **k: new_engine())
        eng = db.get_engine()
        assert eng is not None

    def test_postgres_path_normalizes_url_and_passes_pool_config(self, monkeypatch):
        monkeypatch.setattr(config, "DATABASE_URL", "postgres://u:p@h/db")
        monkeypatch.setattr(config, "DB_POOL_SIZE", 7)
        monkeypatch.setattr(config, "DB_MAX_OVERFLOW", 9)
        monkeypatch.setattr(config, "DB_POOL_RECYCLE", 111)
        monkeypatch.setattr(config, "DB_POOL_TIMEOUT", 22)
        captured = {}

        def fake_create_engine(url, **kw):
            captured["url"] = url
            captured["kw"] = kw
            return new_engine()

        monkeypatch.setattr(db, "create_engine", fake_create_engine)
        eng = db.get_engine()
        assert eng is not None
        assert captured["url"] == "postgresql://u:p@h/db?sslmode=require"
        assert captured["kw"]["pool_size"] == 7
        assert captured["kw"]["max_overflow"] == 9
        assert captured["kw"]["pool_recycle"] == 111
        assert captured["kw"]["pool_timeout"] == 22
        assert captured["kw"]["pool_pre_ping"] is True

    def test_oracle_path_delegates_to_build_oracle_engine(self, monkeypatch):
        monkeypatch.setenv("ORACLE_DSN", "mydb_high")
        monkeypatch.setattr(config, "DATABASE_URL", "")
        monkeypatch.setattr(config, "DB_POOL_SIZE", 3)
        monkeypatch.setattr(config, "DB_MAX_OVERFLOW", 3)
        captured = {}

        def fake_build_oracle_engine(url, **kw):
            captured["url"] = url
            captured["kw"] = kw
            return new_engine(), None

        monkeypatch.setattr(_oc, "build_oracle_engine", fake_build_oracle_engine)
        eng = db.get_engine()
        assert eng is not None
        assert captured["kw"]["db_pool_size"] == 3
        assert captured["kw"]["db_max_overflow"] == 3

    def test_engine_is_cached_across_calls(self, monkeypatch):
        monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u:p@h/db")
        calls = []

        def fake_create_engine(url, **kw):
            calls.append(url)
            return new_engine()

        monkeypatch.setattr(db, "create_engine", fake_create_engine)
        e1 = db.get_engine()
        e2 = db.get_engine()
        assert e1 is e2
        assert len(calls) == 1


class TestGetSessionFactory:
    def test_none_when_no_engine(self, monkeypatch):
        monkeypatch.setattr(config, "DATABASE_URL", "")
        assert db.get_session_factory() is None

    def test_builds_and_caches_a_sessionmaker(self, monkeypatch):
        monkeypatch.setattr(config, "DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setattr(db, "create_engine", lambda *a, **k: new_engine())
        f1 = db.get_session_factory()
        f2 = db.get_session_factory()
        assert f1 is f2
        assert f1() is not None  # a real, usable sessionmaker


class TestGetDb:
    def test_raises_runtime_error_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr(config, "DATABASE_URL", "")
        with pytest.raises(RuntimeError, match="not configured"):
            next(db.get_db())

    def test_yields_a_session_and_always_closes_it(self, monkeypatch):
        monkeypatch.setattr(config, "DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setattr(db, "create_engine", lambda *a, **k: new_engine())
        gen = db.get_db()
        session = next(gen)
        assert isinstance(session, Session)
        closed = []
        session.close = lambda: closed.append(True)
        with pytest.raises(StopIteration):
            next(gen)
        assert closed == [True]

    def test_closes_even_if_the_caller_raises_inside_the_with_block(self, monkeypatch):
        monkeypatch.setattr(config, "DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setattr(db, "create_engine", lambda *a, **k: new_engine())
        gen = db.get_db()
        session = next(gen)
        closed = []
        session.close = lambda: closed.append(True)
        with pytest.raises(ValueError):
            gen.throw(ValueError("boom"))
        assert closed == [True]


# ══════════════════════════════════════════════════════════════════════════
# init_tables
# ══════════════════════════════════════════════════════════════════════════

class _FakeModels:
    def __init__(self):
        self.calls = []

        class _Meta:
            def create_all(inner_self, engine):
                self.calls.append(("create_all", engine))

        class _Base:
            metadata = _Meta()

        self.Base = _Base


class TestInitTables:
    def test_no_engine_logs_error_and_never_touches_models(self, monkeypatch, caplog, sys_modules_models):
        monkeypatch.setattr(config, "DATABASE_URL", "")
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            db.init_tables()
        assert "no engine" in caplog.text
        assert sys_modules_models.calls == []

    def test_happy_path_postgres_wires_create_all_then_ensure_columns_then_indexes(
        self, monkeypatch, sys_modules_models
    ):
        monkeypatch.setattr(config, "DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setattr(db, "create_engine", lambda *a, **k: new_engine())
        order = []
        monkeypatch.setattr(db, "_ensure_columns", lambda eng: order.append("columns"))
        monkeypatch.setattr(db, "_ensure_hot_path_indexes", lambda eng, d: order.append("indexes"))
        monkeypatch.setattr(db, "_ensure_oracle_autoincrement", lambda eng, base: order.append("autoincrement"))
        db.init_tables()
        assert sys_modules_models.calls and sys_modules_models.calls[0][0] == "create_all"
        assert order == ["columns", "indexes"]  # NOT autoincrement — postgres path

    def test_oracle_path_also_runs_autoincrement_before_indexes(self, monkeypatch, sys_modules_models):
        monkeypatch.setenv("ORACLE_DSN", "mydb_high")
        monkeypatch.setattr(config, "DATABASE_URL", "")
        monkeypatch.setattr(_oc, "build_oracle_engine", lambda url, **kw: (new_engine(), None))
        order = []
        monkeypatch.setattr(db, "_ensure_columns", lambda eng: order.append("columns"))
        monkeypatch.setattr(db, "_ensure_hot_path_indexes", lambda eng, d: order.append("indexes"))
        monkeypatch.setattr(db, "_ensure_oracle_autoincrement", lambda eng, base: order.append("autoincrement"))
        db.init_tables()
        assert order == ["columns", "autoincrement", "indexes"]


@pytest.fixture()
def sys_modules_models(monkeypatch):
    """init_tables() does `import models` INSIDE the function body — patch
    sys.modules so that import resolves to a spy instead of the real,
    sqlalchemy-Column-laden models.py (irrelevant to what init_tables
    itself does with it: call create_all() and pass Base to the
    autoincrement helper)."""
    fake = _FakeModels()
    monkeypatch.setitem(sys.modules, "models", fake)
    return fake


# ══════════════════════════════════════════════════════════════════════════
# _ensure_columns
# ══════════════════════════════════════════════════════════════════════════

class TestEnsureColumns:
    def _engine_with_table(self, table, columns):
        eng = new_engine()
        md = MetaData()
        Table(table, md, Column("id", Integer, primary_key=True),
              *[Column(c, String(32)) for c in columns])
        md.create_all(eng)
        eng._sql.clear()
        return eng

    def test_missing_table_is_skipped_not_altered(self, monkeypatch):
        eng = new_engine()  # no tables at all
        monkeypatch.setattr(config, "DATABASE_URL", "")  # dialect() -> postgresql
        db._ensure_columns(eng)
        assert alters(eng) == []

    def test_existing_column_is_a_no_op(self, monkeypatch):
        # BUG FIX (session112 round 15): this used to pre-create the table
        # with only "service_enabled" present, then assert zero ALTERs —
        # which could only pass if _ensure_columns silently skipped every
        # OTHER scalp_gate_state column too. It was accidentally testing
        # "one column already exists" while claiming to test "no-op",
        # masking the fact that the assertion was really exercising a much
        # weaker (and wrong) invariant. Pre-create every scalp_gate_state
        # column from _COLUMN_MIGRATIONS so this genuinely tests the no-op
        # path: nothing missing anywhere -> zero ALTERs.
        eng = self._engine_with_table("scalp_gate_state", [
            c for (t, c, *_rest) in db._COLUMN_MIGRATIONS if t == "scalp_gate_state"
        ])
        db._ensure_columns(eng)
        assert alters(eng) == []

    def test_missing_column_with_default_is_added_not_null_with_default(self, monkeypatch):
        eng = self._engine_with_table("scalp_gate_state", [])  # service_enabled missing
        db._ensure_columns(eng)
        stmts = alters(eng)
        assert any("service_enabled" in s and "DEFAULT" in s.upper() and "NOT NULL" in s.upper() for s in stmts)

    def test_missing_column_with_no_default_is_added_nullable(self, monkeypatch):
        eng = self._engine_with_table("scalp_gate_state", [
            c for (t, c, *_rest) in db._COLUMN_MIGRATIONS if t == "scalp_gate_state" and c != "last_cycle_run_at"
        ])
        db._ensure_columns(eng)
        stmts = alters(eng)
        matching = [s for s in stmts if "last_cycle_run_at" in s]
        assert len(matching) == 1
        assert "NOT NULL" not in matching[0].upper()

    def test_oracle_dialect_builds_add_column_ddl_without_column_keyword(self, monkeypatch):
        # Covers the `is_oracle` DDL branch (lines 296-298) — every other
        # _ensure_columns test above runs with dialect() -> postgresql, so
        # the Oracle-specific "ADD {col} {type}" (no COLUMN keyword,
        # oracle_type/oracle_default) string was never built or exercised.
        monkeypatch.setattr(config, "DATABASE_URL", "oracle+oracledb://u:p@h/svc")
        eng = self._engine_with_table("scalp_gate_state", [])  # service_enabled missing
        db._ensure_columns(eng)
        stmts = alters(eng)
        matching = [s for s in stmts if "service_enabled" in s]
        assert len(matching) == 1
        stmt = matching[0]
        assert "ADD COLUMN" not in stmt.upper()  # Oracle syntax omits COLUMN
        assert "NUMBER(1)" in stmt  # oracle_type, not pg_type (BOOLEAN)
        assert "DEFAULT 1 NOT NULL" in stmt.upper()  # oracle_default, not pg_default (TRUE)

    def test_a_failed_alter_is_logged_and_does_not_abort_the_rest(self, monkeypatch, caplog):
        eng = new_engine()
        md = MetaData()
        Table("scalp_gate_state", md, Column("id", Integer, primary_key=True))
        Table("scalp_capital_ledger", md, Column("id", Integer, primary_key=True))
        md.create_all(eng)
        eng._sql.clear()

        real_begin = eng.begin
        state = {"failed_once": False}

        # BUG FIX (session112 round 15): the original version of this fixture
        # monkeypatched `ctx.__enter__` as an INSTANCE attribute on the
        # context manager returned by `eng.begin()`. The `with` statement
        # looks up dunder methods on the TYPE, not the instance, so that
        # override was silently never invoked — `_ensure_columns` never
        # actually saw a failure, nothing was logged, and the test only
        # "passed" by accident (or, once _ensure_columns changed, failed
        # for a reason that had nothing to do with what it was written to
        # check). `@contextlib.contextmanager` builds a real class whose
        # `__enter__`/`__exit__` live on the type, so this genuinely raises
        # on the first `with engine.begin() as conn:` and behaves normally
        # after that.
        @contextlib.contextmanager
        def flaky_begin():
            if not state["failed_once"]:
                state["failed_once"] = True
                raise OperationalError("ALTER", {}, Exception("simulated failure"))
            with real_begin() as conn:
                yield conn

        monkeypatch.setattr(eng, "begin", flaky_begin)
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            db._ensure_columns(eng)
        assert _warnings(caplog) or any(r.levelno >= logging.ERROR for r in caplog.records)
        # every later migration for this table still got a chance to run —
        # confirmed by the fact _ensure_columns returned without raising.

    def test_inspector_has_table_failure_is_caught_per_entry(self, monkeypatch, caplog):
        class BoomEngine:
            def connect(self):
                raise RuntimeError("connection refused")

        # sqlalchemy.inspect() rejects an object of this shape with its own
        # NoInspectionAvailable before ever calling .connect() — so
        # BoomEngine.connect() itself is never reached via _ensure_columns.
        # Confirm the mock genuinely reproduces the "connection refused"
        # failure mode it's named for, directly.
        with pytest.raises(RuntimeError, match="connection refused"):
            BoomEngine().connect()

        with caplog.at_level(logging.ERROR, logger=LOGGER):
            db._ensure_columns(BoomEngine())  # must not raise
        assert any(r.levelno >= logging.ERROR for r in caplog.records)


class TestColumnMigrationsDriftGuard:
    def test_defaults_are_never_mixed_across_dialects(self):
        """When one dialect's default is None, the other's must be too —
        otherwise the two backends disagree on whether existing rows get a
        value or NULL, which _ensure_columns' `nullable = oracle_default is
        None and pg_default is None` check assumes never happens."""
        bad = [
            (t, c) for (t, c, _ot, _pt, od, pd) in db._COLUMN_MIGRATIONS
            if (od is None) != (pd is None)
        ]
        assert bad == [], f"mixed None/non-None defaults: {bad}"

    def test_no_duplicate_table_column_entries(self):
        seen = [(t, c) for (t, c, *_r) in db._COLUMN_MIGRATIONS]
        assert len(seen) == len(set(seen))


# ══════════════════════════════════════════════════════════════════════════
# _ensure_hot_path_indexes
# ══════════════════════════════════════════════════════════════════════════

class TestEnsureHotPathIndexes:
    def test_creates_the_documented_index(self):
        eng = new_engine()
        md = MetaData()
        Table("scalp_positions", md, Column("id", Integer, primary_key=True), Column("opened_at", String(32)))
        md.create_all(eng)
        eng._sql.clear()
        db._ensure_hot_path_indexes(eng, "postgresql")
        stmts = [s for s in eng._sql if "INDEX" in s.upper()]
        assert any("ix_scalp_positions_opened_at" in s for s in stmts)

    def test_safe_to_call_twice(self):
        eng = new_engine()
        md = MetaData()
        Table("scalp_positions", md, Column("id", Integer, primary_key=True), Column("opened_at", String(32)))
        md.create_all(eng)
        db._ensure_hot_path_indexes(eng, "postgresql")
        db._ensure_hot_path_indexes(eng, "postgresql")  # must not raise


# ══════════════════════════════════════════════════════════════════════════
# _ensure_oracle_autoincrement
# ══════════════════════════════════════════════════════════════════════════

class _FakeOracleConn:
    """Stands in for an Oracle connection: answers the identity-check
    SELECT and the `SELECT NVL(MAX(id), 0) + 1 FROM {table}` SELECT from a
    small config dict, and records every exec_driver_sql / execute call.
    `next_id` values are the query's OWN return value (i.e. already
    MAX(id)+1, not the raw max id) — that's what `.scalar()` on a real
    connection would hand back."""

    def __init__(self, *, has_identity=None, next_id=None, id_check_raises=False, max_id_raises=False):
        self.has_identity = has_identity or {}
        self.next_id = next_id or {}
        self.id_check_raises = id_check_raises
        self.max_id_raises = max_id_raises
        self.driver_sql = []

    def execute(self, stmt, params=None):
        text_ = str(stmt)
        if "user_tab_identity_cols" in text_:
            if self.id_check_raises:
                raise RuntimeError("identity check failed")
            table = (params or {}).get("t", "").lower()
            return _Scalar(1 if self.has_identity.get(table) else 0)
        if "MAX(ID)" in text_.upper():
            if self.max_id_raises:
                raise RuntimeError("max(id) query failed")
            table = None
            for cand in self.next_id:
                if cand in text_:
                    table = cand
            return _Scalar(self.next_id.get(table))
        return _Scalar(None)

    def exec_driver_sql(self, sql):
        self.driver_sql.append(sql)


class _Scalar:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeOracleEngine:
    def __init__(self, conn):
        self._conn = conn
        self.ddl_safe_calls = []

    def connect(self):
        return _CtxWrap(self._conn)

    def begin(self):
        return _CtxWrap(self._conn)


class _CtxWrap:
    def __init__(self, obj):
        self._obj = obj

    def __enter__(self):
        return self._obj

    def __exit__(self, *a):
        return False


class _FakeBase:
    def __init__(self, tables):
        class _Table:
            def __init__(self, name, pk_cols):
                self.name = name
                self.primary_key = type("PK", (), {"columns": [type("C", (), {"name": n})() for n in pk_cols]})()

        class _Meta:
            sorted_tables = [_Table(name, pk) for name, pk in tables]

        self.metadata = _Meta()


class TestEnsureOracleAutoincrement:
    def test_skips_table_whose_pk_is_not_id(self, monkeypatch, caplog):
        base = _FakeBase([("scalp_intraday_restricted", ["symbol"])])
        conn = _FakeOracleConn()
        eng = _FakeOracleEngine(conn)
        monkeypatch.setattr(_oc, "exec_ddl_safe", lambda *a, **k: None)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            db._ensure_oracle_autoincrement(eng, base)
        assert "PK is not" in caplog.text
        assert conn.driver_sql == []

    def test_skips_table_that_already_has_identity(self, monkeypatch):
        base = _FakeBase([("scalp_positions", ["id"])])
        conn = _FakeOracleConn(has_identity={"scalp_positions": True})
        eng = _FakeOracleEngine(conn)
        monkeypatch.setattr(_oc, "exec_ddl_safe", lambda *a, **k: None)
        db._ensure_oracle_autoincrement(eng, base)
        assert conn.driver_sql == []

    def test_creates_sequence_and_trigger_for_a_table_needing_it(self, monkeypatch):
        base = _FakeBase([("scalp_positions", ["id"])])
        conn = _FakeOracleConn(has_identity={}, next_id={"scalp_positions": 42})  # MAX(id)=41, query returns 42
        eng = _FakeOracleEngine(conn)
        ddl_calls = []
        monkeypatch.setattr(_oc, "exec_ddl_safe", lambda engine, sql, dialect: ddl_calls.append(sql))
        db._ensure_oracle_autoincrement(eng, base)
        assert any("CREATE SEQUENCE scalp_positions_id_seq" in s and "START WITH 42" in s for s in ddl_calls)
        assert any("trg_scalp_positions_bi" in s and ":NEW.id" in s for s in conn.driver_sql)

    def test_max_id_query_failure_falls_back_to_starting_at_1(self, monkeypatch):
        base = _FakeBase([("scalp_positions", ["id"])])
        conn = _FakeOracleConn(has_identity={}, max_id_raises=True)
        eng = _FakeOracleEngine(conn)
        ddl_calls = []
        monkeypatch.setattr(_oc, "exec_ddl_safe", lambda engine, sql, dialect: ddl_calls.append(sql))
        db._ensure_oracle_autoincrement(eng, base)
        assert any("START WITH 1 " in s for s in ddl_calls)

    def test_identity_check_failure_warns_and_skips_the_table(self, monkeypatch, caplog):
        base = _FakeBase([("scalp_positions", ["id"])])
        conn = _FakeOracleConn(id_check_raises=True)
        eng = _FakeOracleEngine(conn)
        monkeypatch.setattr(_oc, "exec_ddl_safe", lambda *a, **k: None)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            db._ensure_oracle_autoincrement(eng, base)
        assert "identity check failed" in caplog.text
        assert conn.driver_sql == []

    def test_trigger_creation_failure_warns_but_does_not_raise(self, monkeypatch):
        base = _FakeBase([("scalp_positions", ["id"])])
        conn = _FakeOracleConn(has_identity={}, next_id={"scalp_positions": 6})

        def boom(sql):
            raise RuntimeError("trigger DDL rejected")

        conn.exec_driver_sql = boom
        eng = _FakeOracleEngine(conn)
        monkeypatch.setattr(_oc, "exec_ddl_safe", lambda *a, **k: None)
        db._ensure_oracle_autoincrement(eng, base)  # must not raise


# ── _FakeOracleConn.execute fallthrough path (round-30) ──────────────────────
class TestFakeOracleConnFallthrough:
    """The execute() fallthrough `return _Scalar(None)` on line 563 fires when
    the SQL matches neither `user_tab_identity_cols` nor `MAX(ID)`.  No
    existing test triggers it because every call goes through
    _ensure_oracle_autoincrement, which only issues those two query shapes.
    Call execute() directly with an unrecognised statement to hit the branch."""

    def test_execute_with_unknown_sql_returns_scalar_none(self):
        conn = _FakeOracleConn()
        result = conn.execute("SELECT 1 FROM dual")
        assert result.scalar() is None
