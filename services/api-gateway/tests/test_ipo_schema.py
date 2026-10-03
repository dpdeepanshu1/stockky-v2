"""tests/test_ipo_schema.py — coverage for api-gateway/ipo_schema.py

ipo_static_feed: table name / columns / portable Postgres+Oracle SQL, the Oracle-only row adapter
(bool -> 1/0, blank listing_date -> real None, CLOB bind ceiling, VARCHAR2 byte budgets), the
legacy 1900-01-01 sentinel healer and ensure_ipo_schema().

No network, no database, no real sqlalchemy. Every test loads a FRESH copy of the module with
`sqlalchemy` and `oracle_compat` replaced by small fakes in sys.modules. The fake engines record every
SQL statement and bind, so both dialect branches are driven without a real database.

Run from services/api-gateway:
    python3 -m pytest tests/test_ipo_schema.py -v
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
_MOD_PATH = os.path.join(os.path.dirname(_HERE), "ipo_schema.py")

_ENV_KEYS = (
    "CACHE_DATABASE_URL", "DATABASE_URL", "TRAINING_DATABASE_URL", "ORACLE_DSN",
    "IPO_DB_POOL_SIZE",
    "IPO_DB_POOL_MAX_OVERFLOW",
    "IPO_DB_POOL_RECYCLE",
    "IPO_DB_POOL_TIMEOUT",
    "IPO_JSON_MAX_BYTES",
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
        spec = importlib.util.spec_from_file_location(f"ipo_schema_under_test_{self.n}", _MOD_PATH)
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
            "connect_args": {"connect_timeout": 8, "application_name": "stockky-ipo"},
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
        env.mp.setenv("IPO_DB_POOL_SIZE", "5")
        env.mp.setenv("IPO_DB_MAX_OVERFLOW", "6")
        env.mp.setenv("IPO_DB_POOL_RECYCLE", "7")
        env.mp.setenv("IPO_DB_POOL_TIMEOUT", "8")
        m.make_engine()
        assert env.oc.built[0][1] == {"db_pool_size": "5", "db_max_overflow": "6",
                                      "db_pool_recycle": "7", "db_pool_timeout": "8"}

    def test_oracle_without_oracle_compat_returns_none_and_warns(self, env, caplog):
        mod = env.load(no_oc=True)
        env.oracle()
        with caplog.at_level(logging.WARNING, logger="ipo-schema"):
            assert mod.make_engine() is None
        assert any("oracle_compat.py missing" in r.getMessage() for r in caplog.records)


# ── ensure_*_schema ───────────────────────────────────────────────────────────

class TestEnsureSchema:
    def test_no_backend_configured(self, env, m):
        assert m.ensure_ipo_schema() == {"ok": False, "error": "No DATABASE_URL / CACHE_DATABASE_URL configured"}
        assert env.sa.create_calls == []

    def test_postgres_runs_every_ddl_statement_in_one_transaction(self, env, m):
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")
        assert m.ensure_ipo_schema() == {"ok": True, "table": m.TABLE_NAME, "backend": "postgresql"}
        eng = env.sa.engine
        assert eng.begins == 1
        assert eng.sqls() == [s.strip() for s in m.DDL_STATEMENTS]
        assert eng.disposed == 1
        assert env.sa.create_calls[0][1]["connect_args"]["application_name"] == "stockky-ipo-schema"

    def test_postgres_skips_blank_statements(self, env, m):
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")
        env.mp.setattr(m, "ddl_statements", lambda dial=None: ["  \n ", "SELECT 1", ""])
        assert m.ensure_ipo_schema()["ok"] is True
        assert env.sa.engine.sqls() == ["SELECT 1"]

    def test_oracle_runs_each_statement_via_exec_ddl_safe(self, env, m):
        env.oracle()
        assert m.ensure_ipo_schema() == {"ok": True, "table": m.TABLE_NAME, "backend": "oracle"}
        assert env.oc.ddl == [(s.strip(), "oracle") for s in m.DDL_STATEMENTS_ORACLE]
        assert env.oc.engine.disposed == 1
        assert env.sa.create_calls == []

    def test_oracle_skips_blank_statements(self, env, m):
        env.oracle()
        env.mp.setattr(m, "ddl_statements", lambda dial=None: ["", "  ", "CREATE X"])
        assert m.ensure_ipo_schema()["ok"] is True
        assert env.oc.ddl == [("CREATE X", "oracle")]

    def test_oracle_without_oracle_compat_executes_no_ddl(self, env):
        mod = env.load(no_oc=True)
        env.oracle()
        eng = FakeEngine()
        env.mp.setattr(mod, "make_engine", lambda app_name="x": eng)
        assert mod.ensure_ipo_schema()["ok"] is True
        assert not [s for s in eng.sqls() if s.startswith("CREATE")]
        assert eng.disposed == 1

    def test_engine_could_not_be_built(self, env, m):
        env.mp.setattr(m, "make_engine", lambda app_name="x": None)
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")
        assert m.ensure_ipo_schema() == {"ok": False, "error": "Could not build a database engine"}

    def test_engine_build_failure_is_reported_not_raised(self, env, m):
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")
        env.sa.create_raises = RuntimeError("x" * 500)
        out = m.ensure_ipo_schema()
        assert out["ok"] is False
        assert out["error"] == ("x" * 240)

    def test_ddl_failure_reports_and_still_disposes(self, env, m):
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")
        env.sa.engine.execute_raises = RuntimeError("boom")
        assert m.ensure_ipo_schema() == {"ok": False, "error": "boom"}
        assert env.sa.engine.disposed == 1

    def test_oracle_ddl_failure_reports_and_still_disposes(self, env, m):
        env.oracle()
        env.oc.ddl_raises = RuntimeError("ORA-99999")
        assert m.ensure_ipo_schema() == {"ok": False, "error": "ORA-99999"}
        assert env.oc.engine.disposed == 1

    def test_dispose_failure_does_not_mask_success(self, env, m):
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")
        env.sa.engine.dispose_raises = True
        assert m.ensure_ipo_schema()["ok"] is True


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
    base = dict(
        symbol="ACMEIPO", company_name="Acme Ltd", issue_price=100.5, listing_date="2026-10-05",
        listing_date_estimated=False, issue_start_date="2026-09-29", issue_end_date="2026-10-01",
        stage="open", nse_status="Active", subscription_times=1.5, gmp=12.0, source="nse",
        ipo_score=6.5, decision="BUY", buy_suggestion_json="{}",
    )
    base.update(kw)
    return base


class TestConstants:
    def test_identity(self, m):
        assert m.TABLE_NAME == "ipo_static_feed"
        assert m._LEGACY_NULL_DATE_SENTINEL == "1900-01-01"

    def test_clob_bind_ceiling_default_and_env(self, env):
        assert env.load().CLOB_BIND_MAX_BYTES == 30000
        assert env.load(IPO_JSON_MAX_BYTES="77").CLOB_BIND_MAX_BYTES == 77

    def test_both_ddls_declare_the_same_columns_as_select_columns(self, m):
        want = [c.strip() for c in m.SELECT_COLUMNS.split(",")]
        assert _ddl_columns(m.DDL_STATEMENTS[0]) == want
        assert _ddl_columns(m.DDL_STATEMENTS_ORACLE[0]) == want

    def test_row_keys_are_select_columns_minus_sql_set_updated_at(self, m):
        assert m.ROW_KEYS + ("updated_at",) == tuple(c.strip() for c in m.SELECT_COLUMNS.split(","))

    def test_oracle_varchar_budgets_match_the_oracle_ddl(self, m):
        ddl = m.DDL_STATEMENTS_ORACLE[0]
        for col, limit in m._ORACLE_VARCHAR2_BYTES.items():
            assert re.search(rf"\b{col} VARCHAR2\({limit}\)", ddl), col

    def test_buy_suggestion_json_is_clob_on_oracle_and_text_on_postgres(self, m):
        assert "buy_suggestion_json CLOB" in m.DDL_STATEMENTS_ORACLE[0]
        assert "buy_suggestion_json TEXT" in m.DDL_STATEMENTS[0]

    def test_listing_date_is_a_real_date_column_in_both(self, m):
        assert "listing_date DATE" in m.DDL_STATEMENTS[0]
        assert "listing_date DATE" in m.DDL_STATEMENTS_ORACLE[0]


class TestClipUtf8:
    def test_none_passes_through(self, m):
        assert m._clip_utf8(None, 3) is None

    def test_fits_unchanged(self, m):
        assert m._clip_utf8("abc", 3) == "abc"

    def test_ascii_is_cut_to_byte_budget(self, m):
        assert m._clip_utf8("abcdef", 4) == "abcd"

    def test_never_splits_a_multibyte_character(self, m):
        assert m._clip_utf8("₹" * 5, 10) == "₹₹₹"
        assert m._clip_utf8("₹" * 5, 2) == ""


# ── SQL builders ──────────────────────────────────────────────────────────────

class TestUpsertSql:
    def test_postgres(self, m):
        sql = m.upsert_sql("postgresql")
        assert "ON CONFLICT (symbol) DO UPDATE" in sql
        assert "updated_at = NOW()" in sql
        assert "SYSTIMESTAMP" not in sql and "MERGE" not in sql
        assert _binds(sql) == set(m.ROW_KEYS)

    def test_postgres_blank_listing_date_becomes_null_not_a_cast_error(self, m):
        sql = m.upsert_sql("postgresql")
        assert "CASE WHEN :listing_date = '' THEN NULL ELSE :listing_date::date END" in sql

    def test_oracle(self, m):
        sql = m.upsert_sql("oracle")
        assert "MERGE INTO ipo_static_feed d" in sql and "FROM dual" in sql
        assert "TO_DATE(SUBSTR(:listing_date, 1, 10), 'YYYY-MM-DD')" in sql
        assert "TO_CLOB(:buy_suggestion_json)" in sql
        assert "SYSTIMESTAMP" in sql and "NOW()" not in sql
        assert _binds(sql) == set(m.ROW_KEYS)

    def test_oracle_matched_branch_never_updates_the_join_key(self, m):
        # ORA-38104: columns referenced in the ON clause cannot be updated
        sql = m.upsert_sql("oracle")
        matched = sql.split("WHEN MATCHED THEN UPDATE SET")[1].split("WHEN NOT MATCHED")[0]
        assert "d.symbol" not in matched
        for col in m.ROW_KEYS[1:]:
            assert f"d.{col} = s.{col}" in matched

    def test_default_follows_dialect(self, env, m):
        assert m.upsert_sql() == m.upsert_sql("postgresql")
        env.oracle()
        assert m.upsert_sql() == m.upsert_sql("oracle")


# ── adapt_rows ────────────────────────────────────────────────────────────────

class TestAdaptRows:
    def test_empty(self, m):
        assert m.adapt_rows([]) == []

    def test_does_not_mutate_input_rows(self, m):
        rows = [_row(listing_date_estimated=True, listing_date="")]
        m.adapt_rows(rows, "oracle")
        assert rows[0]["listing_date_estimated"] is True and rows[0]["listing_date"] == ""

    def test_postgres_defaults_missing_estimated_flag_to_false(self, m):
        row = _row()
        del row["listing_date_estimated"]
        assert m.adapt_rows([row], "postgresql")[0]["listing_date_estimated"] is False

    def test_postgres_leaves_everything_else_untouched(self, m):
        row = _row(listing_date_estimated=True, listing_date="", company_name="C" * 500,
                   buy_suggestion_json="j" * 40000)
        assert m.adapt_rows([row], "postgresql") == [row]

    def test_oracle_estimated_flag_is_1_or_0(self, m):
        out = m.adapt_rows([_row(listing_date_estimated=True), _row(listing_date_estimated=False)], "oracle")
        assert [r["listing_date_estimated"] for r in out] == [1, 0]
        assert all(type(r["listing_date_estimated"]) is int for r in out)

    def test_oracle_none_or_absent_estimated_flag_stays_as_is(self, m):
        none_row = m.adapt_rows([_row(listing_date_estimated=None)], "oracle")[0]
        assert none_row["listing_date_estimated"] is None
        row = _row()
        del row["listing_date_estimated"]
        assert "listing_date_estimated" not in m.adapt_rows([row], "oracle")[0]

    @pytest.mark.parametrize("missing", ["", None])
    def test_oracle_blank_listing_date_becomes_real_none(self, m, missing):
        assert m.adapt_rows([_row(listing_date=missing)], "oracle")[0]["listing_date"] is None

    def test_oracle_absent_listing_date_is_added_as_none(self, m):
        row = _row()
        del row["listing_date"]
        assert m.adapt_rows([row], "oracle")[0]["listing_date"] is None

    def test_oracle_real_listing_date_is_kept(self, m):
        assert m.adapt_rows([_row(listing_date="2026-10-05")], "oracle")[0]["listing_date"] == "2026-10-05"

    def test_oracle_clob_payload_clipped_to_bind_ceiling(self, env):
        mod = env.load(IPO_JSON_MAX_BYTES="10")
        out = mod.adapt_rows([_row(buy_suggestion_json="j" * 50)], "oracle")[0]
        assert out["buy_suggestion_json"] == "j" * 10

    def test_oracle_non_str_payload_left_alone(self, m):
        out = m.adapt_rows([_row(buy_suggestion_json=None)], "oracle")[0]
        assert out["buy_suggestion_json"] is None

    def test_oracle_varchar2_columns_clipped_by_utf8_bytes(self, m):
        out = m.adapt_rows([_row(
            symbol="É" * 40, company_name="₹" * 100, stage="s" * 50,
            issue_start_date="2026-09-29T00:00:00+05:30", decision=None,
        )], "oracle")[0]
        assert out["symbol"] == "É" * 15
        assert out["company_name"] == "₹" * 53          # 159 bytes <= 160
        assert len(out["stage"]) == 20
        assert len(out["issue_start_date"]) == 20
        assert out["decision"] is None
        for col, limit in m._ORACLE_VARCHAR2_BYTES.items():
            v = out.get(col)
            if isinstance(v, str):
                assert len(v.encode("utf-8")) <= limit

    def test_default_follows_dialect(self, env, m):
        env.oracle()
        assert m.adapt_rows([_row(listing_date_estimated=True)])[0]["listing_date_estimated"] == 1


# ── legacy 1900-01-01 sentinel healing ───────────────────────────────────────

class TestHealLegacyNullDates:
    def test_runs_update_with_sentinel_bind(self, env, m):
        eng = FakeEngine()
        m._heal_legacy_null_dates(eng)
        (sql, params), = eng.calls
        assert sql.startswith("UPDATE ipo_static_feed SET listing_date = NULL")
        assert "TO_DATE(:sentinel, 'YYYY-MM-DD')" in sql
        assert params == {"sentinel": "1900-01-01"}
        assert eng.begins == 1

    def test_logs_when_rows_were_healed(self, m, caplog):
        eng = FakeEngine()
        eng.result = SimpleNamespace(rowcount=3)
        with caplog.at_level(logging.INFO, logger="ipo-schema"):
            m._heal_legacy_null_dates(eng)
        assert any("healed 3 legacy sentinel" in r.getMessage() for r in caplog.records)

    def test_silent_when_nothing_healed(self, m, caplog):
        with caplog.at_level(logging.INFO, logger="ipo-schema"):
            m._heal_legacy_null_dates(FakeEngine())
        assert not caplog.records

    def test_missing_rowcount_counts_as_zero(self, m, caplog):
        eng = FakeEngine()
        eng.result = SimpleNamespace()               # no rowcount attribute at all
        with caplog.at_level(logging.DEBUG, logger="ipo-schema"):
            m._heal_legacy_null_dates(eng)
        assert not caplog.records                    # not even the DEBUG "heal skipped" line

    def test_none_rowcount_counts_as_zero(self, m, caplog):
        eng = FakeEngine()
        eng.result = SimpleNamespace(rowcount=None)
        with caplog.at_level(logging.INFO, logger="ipo-schema"):
            m._heal_legacy_null_dates(eng)
        assert not caplog.records

    def test_failure_is_swallowed_and_logged_at_debug(self, m, caplog):
        eng = FakeEngine()
        eng.execute_raises = RuntimeError("ORA-00942 table or view does not exist")
        with caplog.at_level(logging.DEBUG, logger="ipo-schema"):
            m._heal_legacy_null_dates(eng)           # first-ever run: table may not exist yet
        assert any("heal skipped" in r.getMessage() and "ORA-00942" in r.getMessage()
                   for r in caplog.records)

    def test_sqlalchemy_import_failure_is_swallowed(self, env, m):
        env.mp.setitem(sys.modules, "sqlalchemy", None)
        m._heal_legacy_null_dates(FakeEngine())      # must not raise


class TestEnsureHealsOnlyOnOracle:
    def test_oracle_path_heals_after_the_ddl(self, env, m):
        env.oracle()
        assert m.ensure_ipo_schema()["ok"] is True
        eng = env.oc.engine
        assert len(env.oc.ddl) == len(m.DDL_STATEMENTS_ORACLE)
        assert [s for s in eng.sqls() if s.startswith("UPDATE ipo_static_feed")]

    def test_postgres_path_never_touches_the_sentinel(self, env, m):
        env.mp.setenv("DATABASE_URL", "postgres://u@h/db")
        assert m.ensure_ipo_schema()["ok"] is True
        assert not [s for s in env.sa.engine.sqls() if "UPDATE" in s]
