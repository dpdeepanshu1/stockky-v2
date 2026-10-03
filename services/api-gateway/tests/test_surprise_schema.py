"""tests/test_surprise_schema.py — coverage for api-gateway/surprise_schema.py

surprise_static_feed: table name / columns / portable Postgres+Oracle SQL, the Oracle-only is_liquid
adapter, the process-wide engine cache and ensure_surprise_schema().

No network, no database, no real sqlalchemy. Every test loads a FRESH copy of the module (so
_ENGINE_CACHE never leaks between tests) with `sqlalchemy` and `oracle_compat` replaced by small
fakes in sys.modules. The fake engines record every SQL statement and bind, so both dialect
branches (postgresql / oracle) are driven without a real database.

Run from services/api-gateway:
    python3 -m pytest tests/test_surprise_schema.py -v
"""
from __future__ import annotations

import importlib.util
import logging
import os
import re
import sys
import types
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.join(os.path.dirname(_HERE), "surprise_schema.py")

_ENV_KEYS = (
    "CACHE_DATABASE_URL", "DATABASE_URL", "TRAINING_DATABASE_URL", "ORACLE_DSN",
    "SURPRISE_DB_POOL_SIZE",
    "SURPRISE_DB_POOL_MAX_OVERFLOW",
    "SURPRISE_DB_POOL_RECYCLE",
    "SURPRISE_DB_POOL_TIMEOUT",
)


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeText:
    """Stand-in for sqlalchemy.text(): remembers the SQL, str() gives it back."""

    def __init__(self, sql):
        self.sql = sql

    def __str__(self):
        return self.sql


class FakeConn:
    def __init__(self, eng):
        self.eng = eng

    def execute(self, stmt, params=None):
        self.eng.calls.append((str(stmt), params))
        if self.eng.execute_raises is not None:
            raise self.eng.execute_raises
        return self.eng.result


class _Ctx:
    def __init__(self, eng):
        self.eng = eng

    def __enter__(self):
        self.eng.begins += 1
        return FakeConn(self.eng)

    def __exit__(self, *a):
        return False


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.begins = 0
        self.disposed = 0
        self.dispose_raises = False
        self.execute_raises = None
        self.result = SimpleNamespace(rowcount=0)

    def begin(self):
        return _Ctx(self)

    def dispose(self):
        self.disposed += 1
        if self.dispose_raises:
            raise RuntimeError("dispose boom")

    def sqls(self):
        return [c[0] for c in self.calls]


class FakeOC:
    """Stand-in for oracle_compat (same oracle_is_configured contract as the real one)."""

    def __init__(self):
        self.engine = FakeEngine()
        self.built = []
        self.ddl = []
        self.configured_raises = False
        self.ddl_raises = None

    def oracle_is_configured(self, url=""):
        if self.configured_raises:
            raise RuntimeError("oc broken")
        return (url or "").lower().startswith("oracle") or bool(os.environ.get("ORACLE_DSN"))

    def build_oracle_engine(self, url="", **kw):
        self.built.append((url, kw))
        return self.engine, url

    def exec_ddl_safe(self, eng, sql, dialect):
        if self.ddl_raises is not None:
            raise self.ddl_raises
        self.ddl.append((sql, dialect))


class FakeSA:
    """Stand-in for the `sqlalchemy` module (only text / create_engine are used)."""

    def __init__(self):
        self.engine = FakeEngine()
        self.create_calls = []
        self.create_raises = None

    def module(self):
        m = types.ModuleType("sqlalchemy")
        m.text = FakeText

        def create_engine(url, **kw):
            self.create_calls.append((url, kw))
            if self.create_raises is not None:
                raise self.create_raises
            return self.engine

        m.create_engine = create_engine
        return m


class Env:
    """Loads a FRESH copy of the module per call (so _ENGINE_CACHE etc. never leak between
    tests) with `oracle_compat` and `sqlalchemy` replaced by fakes in sys.modules."""

    def __init__(self, monkeypatch):
        self.mp = monkeypatch
        self.oc = FakeOC()
        self.sa = FakeSA()
        self.n = 0

    def load(self, no_oc=False, **env):
        for k, v in env.items():
            self.mp.setenv(k, v)
        self.mp.setitem(sys.modules, "oracle_compat", None if no_oc else self.oc)
        self.mp.setitem(sys.modules, "sqlalchemy", self.sa.module())
        self.n += 1
        spec = importlib.util.spec_from_file_location(f"surprise_schema_under_test_{self.n}", _MOD_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def oracle(self):
        self.mp.setenv("ORACLE_DSN", "adb_high")


def _binds(sql):
    """Named binds in a SQL string (ignores '::' casts)."""
    return set(re.findall(r"(?<!:):([A-Za-z_][A-Za-z_0-9]*)", sql))


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return Env(monkeypatch)


@pytest.fixture
def m(env):
    return env.load()


# ── URL / backend selection ───────────────────────────────────────────────────

class TestNormalizeDbUrl:
    @pytest.mark.parametrize("raw, expected", [
        # postgres:// -> postgresql://, sslmode appended with '?'
        ("postgres://u:p@h/db", "postgresql://u:p@h/db?sslmode=require"),
        # already postgresql:// and already has a query string -> '&'
        ("postgresql://u@h/db?application_name=x",
         "postgresql://u@h/db?application_name=x&sslmode=require"),
        # sslmode=required (psycopg2 rejects it) is corrected to require
        ("postgresql://u@h/db?sslmode=required", "postgresql://u@h/db?sslmode=require"),
        ("postgresql://u@h/db?SSLMODE=REQUIRED", "postgresql://u@h/db?SSLMODE=require"),
        # an explicit sslmode (any value) is kept, nothing appended
        ("postgresql://u@h/db?sslmode=disable", "postgresql://u@h/db?sslmode=disable"),
        ("postgresql://u@h/db?sslmode=verify-full", "postgresql://u@h/db?sslmode=verify-full"),
        # channel_binding is stripped wherever it sits next to the '?'
        ("postgresql://u@h/db?channel_binding=require",
         "postgresql://u@h/db?sslmode=require"),
        ("postgresql://u@h/db?channel_binding=require&sslmode=require",
         "postgresql://u@h/db?sslmode=require"),
        ("postgresql://u@h/db?sslmode=require&channel_binding=require",
         "postgresql://u@h/db?sslmode=require"),
        # in the MIDDLE: used to leave "a=1&&b=2", which libpq rejects
        ("postgresql://u@h/db?a=1&channel_binding=require&b=2",
         "postgresql://u@h/db?a=1&b=2&sslmode=require"),
    ])
    def test_shapes(self, m, raw, expected):
        assert m._normalize_db_url(raw) == expected

    def test_idempotent(self, m):
        once = m._normalize_db_url("postgres://u@h/db?channel_binding=require&sslmode=required")
        assert m._normalize_db_url(once) == once


class TestRawUrl:
    def test_empty_when_nothing_set(self, m):
        assert m._raw_url() == ""

    def test_precedence_cache_then_database_then_training(self, env, m):
        env.mp.setenv("TRAINING_DATABASE_URL", "postgres://training")
        assert m._raw_url() == "postgres://training"
        env.mp.setenv("DATABASE_URL", "postgres://database")
        assert m._raw_url() == "postgres://database"
        env.mp.setenv("CACHE_DATABASE_URL", "postgres://cache")
        assert m._raw_url() == "postgres://cache"


class TestIsOracleAndDialect:
    def test_postgres_by_default(self, m):
        assert m.is_oracle() is False
        assert m.dialect() == "postgresql"

    def test_oracle_dsn_env(self, env, m):
        env.oracle()
        assert m.is_oracle() is True
        assert m.dialect() == "oracle"

    def test_oracle_url_scheme(self, env, m):
        env.mp.setenv("DATABASE_URL", "oracle+oracledb://u:p@h/svc")
        assert m.is_oracle() is True

    def test_oc_raising_falls_back_to_env_dsn(self, env, m):
        env.oc.configured_raises = True
        assert m.is_oracle() is False
        env.oracle()
        assert m.is_oracle() is True

    def test_without_oracle_compat_module(self, env):
        mod = env.load(no_oc=True)
        assert mod._oc is None
        assert mod.is_oracle() is False
        env.oracle()
        assert mod.is_oracle() is True


class TestDatabaseUrl:
    def test_none_when_unconfigured(self, m):
        assert m.database_url() is None

    def test_postgres_url_is_normalized(self, env, m):
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")
        assert m.database_url() == "postgresql://u@h/db?sslmode=require"

    def test_oracle_without_url_returns_sentinel(self, env, m):
        env.oracle()
        assert m.database_url() == "oracle+oracledb://"

    def test_oracle_with_non_oracle_url_still_sentinel(self, env, m):
        env.oracle()
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")
        assert m.database_url() == "oracle+oracledb://"

    def test_oracle_url_passes_through_untouched(self, env, m):
        env.mp.setenv("DATABASE_URL", "Oracle+oracledb://u:p@h/svc")
        assert m.database_url() == "Oracle+oracledb://u:p@h/svc"


class TestDdlStatements:
    def test_default_follows_dialect(self, env, m):
        assert m.ddl_statements() is m.DDL_STATEMENTS
        env.oracle()
        assert m.ddl_statements() is m.DDL_STATEMENTS_ORACLE

    def test_explicit_dialect_wins(self, env, m):
        env.oracle()
        assert m.ddl_statements("postgresql") is m.DDL_STATEMENTS
        assert m.ddl_statements("oracle") is m.DDL_STATEMENTS_ORACLE

    def test_first_statement_creates_the_table_in_both_dialects(self, m):
        assert "CREATE TABLE IF NOT EXISTS " + m.TABLE_NAME in m.DDL_STATEMENTS[0]
        assert "CREATE TABLE " + m.TABLE_NAME in m.DDL_STATEMENTS_ORACLE[0]
        # Oracle has no IF NOT EXISTS before 23c
        assert "IF NOT EXISTS" not in " ".join(m.DDL_STATEMENTS_ORACLE)
        assert all(s.startswith("CREATE INDEX") for s in m.DDL_STATEMENTS[1:])
        assert all(s.startswith("CREATE INDEX") for s in m.DDL_STATEMENTS_ORACLE[1:])


class TestTableExistsSql:
    def test_postgres(self, m):
        sql = m.table_exists_sql()
        assert "information_schema.tables" in sql
        assert _binds(sql) == {"tbl"}

    def test_oracle_uppercases_the_bind(self, m):
        sql = m.table_exists_sql("oracle")
        assert "user_tables" in sql and "UPPER(:tbl)" in sql
        assert _binds(sql) == {"tbl"}

    def test_default_follows_dialect(self, env, m):
        env.oracle()
        assert m.table_exists_sql() == m.table_exists_sql("oracle")


class TestMakeEngine:
    def test_none_when_unconfigured(self, env, m):
        assert m.make_engine() is None
        assert env.sa.create_calls == []
        assert env.oc.built == []

    def test_postgres_engine_kwargs(self, env, m):
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")
        assert m.make_engine() is env.sa.engine
        (url, kw), = env.sa.create_calls
        assert url == "postgresql://u@h/db?sslmode=require"
        assert kw == {
            "pool_pre_ping": True, "pool_size": 1, "max_overflow": 1, "pool_timeout": 8,
            "connect_args": {"connect_timeout": 8, "application_name": "stockky-surprise"},
        }

    def test_postgres_custom_app_name(self, env, m):
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")
        m.make_engine("custom-app")
        assert env.sa.create_calls[0][1]["connect_args"]["application_name"] == "custom-app"

    def test_oracle_uses_oracle_compat_with_default_pool(self, env, m):
        env.oracle()
        assert m.make_engine() is env.oc.engine
        assert env.sa.create_calls == []          # psycopg2 kwargs never reach Oracle
        (url, kw), = env.oc.built
        assert url == "oracle+oracledb://"
        assert kw == {"db_pool_size": "2", "db_max_overflow": "1",
                      "db_pool_recycle": "300", "db_pool_timeout": "15"}

    def test_oracle_pool_env_overrides(self, env, m):
        env.oracle()
        env.mp.setenv("SURPRISE_DB_POOL_SIZE", "5")
        env.mp.setenv("SURPRISE_DB_MAX_OVERFLOW", "6")
        env.mp.setenv("SURPRISE_DB_POOL_RECYCLE", "7")
        env.mp.setenv("SURPRISE_DB_POOL_TIMEOUT", "8")
        m.make_engine()
        assert env.oc.built[0][1] == {"db_pool_size": "5", "db_max_overflow": "6",
                                      "db_pool_recycle": "7", "db_pool_timeout": "8"}

    def test_oracle_without_oracle_compat_returns_none_and_warns(self, env, caplog):
        mod = env.load(no_oc=True)
        env.oracle()
        with caplog.at_level(logging.WARNING, logger="surprise-schema"):
            assert mod.make_engine() is None
        assert any("oracle_compat.py missing" in r.getMessage() for r in caplog.records)


# ── shared engine cache / now_func ────────────────────────────────────────────

class TestNowFunc:
    def test_postgres_and_oracle(self, m):
        assert m.now_func() == "NOW()"
        assert m.now_func("postgresql") == "NOW()"
        assert m.now_func("oracle") == "SYSTIMESTAMP"

    def test_default_follows_dialect(self, env, m):
        env.oracle()
        assert m.now_func() == "SYSTIMESTAMP"


class TestSharedEngine:
    def _pg(self, env):
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")

    def test_builds_once_and_reuses(self, env, m):
        self._pg(env)
        a = m.shared_engine()
        b = m.shared_engine()
        assert a is b is env.sa.engine
        assert len(env.sa.create_calls) == 1

    def test_keyed_by_app_name(self, env, m):
        self._pg(env)
        m.shared_engine("one")
        m.shared_engine("two")
        m.shared_engine("one")
        assert len(env.sa.create_calls) == 2

    def test_rebuilds_when_url_changes(self, env, m):
        self._pg(env)
        m.shared_engine()
        env.mp.setenv("DATABASE_URL", "postgres://u@other/db")
        m.shared_engine()
        assert len(env.sa.create_calls) == 2

    def test_none_is_not_cached(self, env, m):
        assert m.shared_engine() is None
        assert m._ENGINE_CACHE == {}
        self._pg(env)
        assert m.shared_engine() is env.sa.engine

    def test_oracle_none_when_oracle_compat_missing(self, env):
        mod = env.load(no_oc=True)
        env.oracle()
        assert mod.shared_engine() is None
        assert mod._ENGINE_CACHE == {}

    def test_oracle_engine_is_cached_too(self, env, m):
        env.oracle()
        m.shared_engine()
        m.shared_engine()
        assert len(env.oc.built) == 1


class TestDisposeSharedEngines:
    def test_disposes_everything_and_clears(self, env, m):
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")
        eng = m.shared_engine()
        m.dispose_shared_engines()
        assert eng.disposed == 1
        assert m._ENGINE_CACHE == {}

    def test_dispose_failure_is_swallowed_and_cache_still_cleared(self, env, m):
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")
        eng = m.shared_engine()
        eng.dispose_raises = True
        m.dispose_shared_engines()               # must not raise
        assert m._ENGINE_CACHE == {}

    def test_noop_when_empty(self, m):
        m.dispose_shared_engines()
        assert m._ENGINE_CACHE == {}


# ── ensure_*_schema ───────────────────────────────────────────────────────────

class TestEnsureSchema:
    def test_no_backend_configured(self, env, m):
        assert m.ensure_surprise_schema() == {"ok": False, "error": "No DATABASE_URL / CACHE_DATABASE_URL configured"}
        assert env.sa.create_calls == []

    def test_postgres_runs_every_ddl_statement_in_one_transaction(self, env, m):
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")
        assert m.ensure_surprise_schema() == {"ok": True, "table": m.TABLE_NAME, "backend": "postgresql"}
        eng = env.sa.engine
        assert eng.begins == 1
        assert eng.sqls() == [s.strip() for s in m.DDL_STATEMENTS]
        assert eng.disposed == 1
        assert env.sa.create_calls[0][1]["connect_args"]["application_name"] == "stockky-surprise-schema"

    def test_postgres_skips_blank_statements(self, env, m):
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")
        env.mp.setattr(m, "ddl_statements", lambda dial=None: ["  \n ", "SELECT 1", ""])
        assert m.ensure_surprise_schema()["ok"] is True
        assert env.sa.engine.sqls() == ["SELECT 1"]

    def test_oracle_runs_each_statement_via_exec_ddl_safe(self, env, m):
        env.oracle()
        assert m.ensure_surprise_schema() == {"ok": True, "table": m.TABLE_NAME, "backend": "oracle"}
        assert env.oc.ddl == [(s.strip(), "oracle") for s in m.DDL_STATEMENTS_ORACLE]
        assert env.oc.engine.disposed == 1
        assert env.sa.create_calls == []

    def test_oracle_skips_blank_statements(self, env, m):
        env.oracle()
        env.mp.setattr(m, "ddl_statements", lambda dial=None: ["", "  ", "CREATE X"])
        assert m.ensure_surprise_schema()["ok"] is True
        assert env.oc.ddl == [("CREATE X", "oracle")]

    def test_oracle_without_oracle_compat_executes_no_ddl(self, env):
        mod = env.load(no_oc=True)
        env.oracle()
        eng = FakeEngine()
        env.mp.setattr(mod, "make_engine", lambda app_name="x": eng)
        assert mod.ensure_surprise_schema()["ok"] is True
        assert not [s for s in eng.sqls() if s.startswith("CREATE")]
        assert eng.disposed == 1

    def test_engine_could_not_be_built(self, env, m):
        env.mp.setattr(m, "make_engine", lambda app_name="x": None)
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")
        assert m.ensure_surprise_schema() == {"ok": False, "error": "Could not build a database engine"}

    def test_engine_build_failure_is_reported_not_raised(self, env, m):
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")
        env.sa.create_raises = RuntimeError("x" * 500)
        out = m.ensure_surprise_schema()
        assert out["ok"] is False
        assert out["error"] == ("x" * 240)

    def test_ddl_failure_reports_and_still_disposes(self, env, m):
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")
        env.sa.engine.execute_raises = RuntimeError("boom")
        assert m.ensure_surprise_schema() == {"ok": False, "error": "boom"}
        assert env.sa.engine.disposed == 1

    def test_oracle_ddl_failure_reports_and_still_disposes(self, env, m):
        env.oracle()
        env.oc.ddl_raises = RuntimeError("ORA-99999")
        assert m.ensure_surprise_schema() == {"ok": False, "error": "ORA-99999"}
        assert env.oc.engine.disposed == 1

    def test_dispose_failure_does_not_mask_success(self, env, m):
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")
        env.sa.engine.dispose_raises = True
        assert m.ensure_surprise_schema()["ok"] is True


# ── constants / drift guards ──────────────────────────────────────────────────

def _ddl_columns(ddl):
    cols = []
    for line in ddl.strip().splitlines()[1:]:
        tok = line.strip().split()
        if not tok or tok[0] in (")", "PRIMARY", "CONSTRAINT"):
            continue
        cols.append(tok[0])
    return cols


def _row(**kw):
    base = dict(symbol="INFY", prev_close=1500.25, avg_15m_volume=25000, daily_atr=22.5,
                high_52w=1800.0, dist_52w_pct=16.7, sector="IT", is_liquid=True)
    base.update(kw)
    return base


class TestConstants:
    def test_identity(self, m):
        assert m.TABLE_NAME == "surprise_static_feed"

    def test_insert_and_select_columns_agree(self, m):
        assert m.INSERT_COLUMNS == m.SELECT_COLUMNS

    def test_both_ddls_declare_the_same_columns_as_select_columns(self, m):
        want = [c.strip() for c in m.SELECT_COLUMNS.split(",")]
        assert _ddl_columns(m.DDL_STATEMENTS[0]) == want
        assert _ddl_columns(m.DDL_STATEMENTS_ORACLE[0]) == want

    def test_row_keys_are_select_columns_minus_sql_set_updated_at(self, m):
        assert m.ROW_KEYS + ("updated_at",) == tuple(c.strip() for c in m.SELECT_COLUMNS.split(","))

    def test_metric_columns_have_not_null_defaults_so_a_missing_metric_still_inserts(self, m):
        for ddl in (m.DDL_STATEMENTS[0], m.DDL_STATEMENTS_ORACLE[0]):
            for col in ("prev_close", "avg_15m_volume", "daily_atr", "high_52w", "dist_52w_pct"):
                line = [l for l in ddl.splitlines() if l.strip().startswith(col + " ")][0]
                assert "NOT NULL" in line and "DEFAULT" in line, (col, line)

    def test_oracle_puts_default_before_not_null(self, m):
        for line in m.DDL_STATEMENTS_ORACLE[0].splitlines():
            if "NOT NULL" in line and "DEFAULT" in line:
                assert line.index("DEFAULT") < line.index("NOT NULL"), line

    def test_redundant_symbol_index_exists_on_postgres_only(self, m):
        assert any("idx_surprise_static_sym" in s for s in m.DDL_STATEMENTS)
        assert not any("idx_surprise_static_sym" in s for s in m.DDL_STATEMENTS_ORACLE)


# ── SQL builders ──────────────────────────────────────────────────────────────

class TestUpsertSql:
    def test_postgres(self, m):
        sql = m.upsert_sql("postgresql")
        assert "ON CONFLICT (symbol) DO UPDATE" in sql
        assert "updated_at = NOW()" in sql
        assert "sector = COALESCE(EXCLUDED.sector, surprise_static_feed.sector)" in sql
        assert "SYSTIMESTAMP" not in sql and "MERGE" not in sql
        assert _binds(sql) == set(m.ROW_KEYS)

    def test_oracle(self, m):
        sql = m.upsert_sql("oracle")
        assert "MERGE INTO surprise_static_feed d" in sql and "FROM dual" in sql
        assert "d.sector = COALESCE(s.sector, d.sector)" in sql
        assert "SYSTIMESTAMP" in sql and "NOW()" not in sql
        assert _binds(sql) == set(m.ROW_KEYS)

    def test_oracle_matched_branch_never_updates_the_join_key(self, m):
        sql = m.upsert_sql("oracle")
        matched = sql.split("WHEN MATCHED THEN UPDATE SET")[1].split("WHEN NOT MATCHED")[0]
        assert "d.symbol" not in matched
        for col in m.ROW_KEYS[1:]:
            assert f"d.{col} = " in matched

    def test_default_follows_dialect(self, env, m):
        assert m.upsert_sql() == m.upsert_sql("postgresql")
        env.oracle()
        assert m.upsert_sql() == m.upsert_sql("oracle")


# ── adapt_rows ────────────────────────────────────────────────────────────────

class TestAdaptRows:
    def test_postgres_returns_the_same_list_untouched(self, m):
        rows = [_row(is_liquid=True)]
        out = m.adapt_rows(rows, "postgresql")
        assert out is rows
        assert out[0]["is_liquid"] is True

    def test_default_follows_dialect(self, env, m):
        rows = [_row(is_liquid=True)]
        assert m.adapt_rows(rows) is rows
        env.oracle()
        assert m.adapt_rows(rows)[0]["is_liquid"] == 1

    def test_oracle_is_liquid_is_1_or_0(self, m):
        out = m.adapt_rows([_row(is_liquid=True), _row(is_liquid=False), _row(is_liquid=0),
                            _row(is_liquid="")], "oracle")
        assert [r["is_liquid"] for r in out] == [1, 0, 0, 0]
        assert all(type(r["is_liquid"]) is int for r in out)

    def test_oracle_none_or_absent_is_liquid_is_left_alone(self, m):
        assert m.adapt_rows([_row(is_liquid=None)], "oracle")[0]["is_liquid"] is None
        row = _row()
        del row["is_liquid"]
        assert "is_liquid" not in m.adapt_rows([row], "oracle")[0]

    def test_oracle_copies_rows_and_leaves_other_columns_alone(self, m):
        rows = [_row(is_liquid=True, sector=None)]
        out = m.adapt_rows(rows, "oracle")
        assert out[0] is not rows[0]
        assert rows[0]["is_liquid"] is True                       # input not mutated
        assert {k: v for k, v in out[0].items() if k != "is_liquid"} == \
               {k: v for k, v in rows[0].items() if k != "is_liquid"}

    def test_oracle_empty(self, m):
        assert m.adapt_rows([], "oracle") == []
