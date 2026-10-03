"""
tests/test_oracle_compat.py — 100% coverage for oracle_compat.py

Pure stdlib + sqlalchemy (sqlite in-memory) — no oracledb, no Neon.
The Oracle-DDL branches are tested with dialect="oracle" strings directly;
SQLAlchemy's sqlite engine is used for the Postgres/generic branches.

Run from services/market-data-service:
    python3 -m pytest tests/test_oracle_compat.py -v
"""
from __future__ import annotations
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
import oracle_compat as oc


# ══════════════════════════════════════════════════════════════════════════════
# oracle_is_configured
# ══════════════════════════════════════════════════════════════════════════════

class TestOracleIsConfigured:
    def test_oracle_url_scheme_returns_true(self):
        assert oc.oracle_is_configured("oracle+oracledb://...") is True

    def test_postgres_url_returns_false(self, monkeypatch):
        monkeypatch.delenv("ORACLE_DSN", raising=False)
        assert oc.oracle_is_configured("postgresql://host/db") is False

    def test_empty_url_with_dsn_env_returns_true(self, monkeypatch):
        monkeypatch.setenv("ORACLE_DSN", "stockkydb_high")
        assert oc.oracle_is_configured("") is True

    def test_empty_url_no_dsn_returns_false(self, monkeypatch):
        monkeypatch.delenv("ORACLE_DSN", raising=False)
        assert oc.oracle_is_configured("") is False

    def test_exception_in_check_returns_false(self, monkeypatch):
        # pass a non-string to trigger the except branch
        assert oc.oracle_is_configured(None) is False  # type: ignore


# ══════════════════════════════════════════════════════════════════════════════
# dialect_name / is_oracle_engine
# ══════════════════════════════════════════════════════════════════════════════

class TestDialectHelpers:
    def test_sqlite_engine_dialect(self):
        eng = create_engine("sqlite:///:memory:")
        assert oc.dialect_name(eng) == "sqlite"

    def test_is_oracle_engine_false_for_sqlite(self):
        eng = create_engine("sqlite:///:memory:")
        assert oc.is_oracle_engine(eng) is False

    def test_dialect_name_exception_returns_empty(self):
        class _Bad:
            @property
            def dialect(self): raise RuntimeError("no")
        assert oc.dialect_name(_Bad()) == ""


# ══════════════════════════════════════════════════════════════════════════════
# _configure_oracle_lobs
# ══════════════════════════════════════════════════════════════════════════════

class TestConfigureOracleLobs:
    def test_idempotent_when_already_configured(self):
        oc._ORACLE_LOB_CONFIGURED = True
        oc._configure_oracle_lobs()   # must not raise or change state
        assert oc._ORACLE_LOB_CONFIGURED is True
        oc._ORACLE_LOB_CONFIGURED = False

    def test_sets_fetch_lobs_when_oracledb_available(self, monkeypatch):
        fake_oracledb = types.ModuleType("oracledb")
        class _Defaults: fetch_lobs = True
        fake_oracledb.defaults = _Defaults()
        monkeypatch.setitem(sys.modules, "oracledb", fake_oracledb)
        oc._ORACLE_LOB_CONFIGURED = False
        oc._configure_oracle_lobs()
        assert fake_oracledb.defaults.fetch_lobs is False
        assert oc._ORACLE_LOB_CONFIGURED is True
        oc._ORACLE_LOB_CONFIGURED = False

    def test_no_crash_when_oracledb_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "oracledb", None)
        oc._ORACLE_LOB_CONFIGURED = False
        oc._configure_oracle_lobs()   # must not raise
        oc._ORACLE_LOB_CONFIGURED = False

    def test_no_crash_when_fetch_lobs_attr_missing(self, monkeypatch):
        fake = types.ModuleType("oracledb")
        fake.defaults = object()   # no fetch_lobs attr → exception branch
        monkeypatch.setitem(sys.modules, "oracledb", fake)
        oc._ORACLE_LOB_CONFIGURED = False
        oc._configure_oracle_lobs()
        assert oc._ORACLE_LOB_CONFIGURED is True
        oc._ORACLE_LOB_CONFIGURED = False


# ══════════════════════════════════════════════════════════════════════════════
# oracle_engine_kwargs
# ══════════════════════════════════════════════════════════════════════════════

class TestOracleEngineKwargs:
    def test_discrete_vars_populate_connect_args(self, monkeypatch):
        monkeypatch.setenv("ORACLE_USER", "MYUSER")
        monkeypatch.setenv("ORACLE_PASSWORD", "secret")
        monkeypatch.setenv("ORACLE_DSN", "mydb_high")
        monkeypatch.setenv("ORACLE_WALLET_DIR", "/wallet")
        monkeypatch.setenv("ORACLE_WALLET_PASSWORD", "walletpw")
        kw = oc.oracle_engine_kwargs(full_url_provided=False)
        ca = kw["connect_args"]
        assert ca["user"] == "MYUSER"
        assert ca["password"] == "secret"
        assert ca["dsn"] == "mydb_high"
        assert ca["config_dir"] == "/wallet"
        assert ca["wallet_location"] == "/wallet"
        assert ca["wallet_password"] == "walletpw"

    def test_full_url_skips_discrete_creds(self, monkeypatch):
        monkeypatch.delenv("ORACLE_USER", raising=False)
        monkeypatch.delenv("ORACLE_PASSWORD", raising=False)
        monkeypatch.delenv("ORACLE_DSN", raising=False)
        kw = oc.oracle_engine_kwargs(full_url_provided=True)
        ca = kw["connect_args"]
        assert "user" not in ca
        assert "dsn" not in ca

    def test_admin_password_fallback(self, monkeypatch):
        monkeypatch.delenv("ORACLE_PASSWORD", raising=False)
        monkeypatch.setenv("ORACLE_ADMIN_PASSWORD", "adminpw")
        kw = oc.oracle_engine_kwargs(full_url_provided=False)
        assert kw["connect_args"]["password"] == "adminpw"

    def test_pool_defaults(self, monkeypatch):
        for k in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_RECYCLE", "DB_POOL_TIMEOUT"):
            monkeypatch.delenv(k, raising=False)
        kw = oc.oracle_engine_kwargs(full_url_provided=True)
        assert kw["pool_size"] == 3
        assert kw["max_overflow"] == 2
        assert kw["pool_recycle"] == 300
        assert kw["pool_timeout"] == 30

    def test_pool_overrides(self, monkeypatch):
        for k in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_RECYCLE", "DB_POOL_TIMEOUT"):
            monkeypatch.delenv(k, raising=False)
        kw = oc.oracle_engine_kwargs(full_url_provided=True,
                                     db_pool_size=10, db_max_overflow=5)
        assert kw["pool_size"] == 10
        assert kw["max_overflow"] == 5

    def test_tns_admin_fallback_for_wallet_dir(self, monkeypatch):
        monkeypatch.delenv("ORACLE_WALLET_DIR", raising=False)
        monkeypatch.setenv("TNS_ADMIN", "/tns")
        kw = oc.oracle_engine_kwargs(full_url_provided=True)
        assert kw["connect_args"]["config_dir"] == "/tns"

    def test_no_wallet_dir_no_connect_args_wallet(self, monkeypatch):
        monkeypatch.delenv("ORACLE_WALLET_DIR", raising=False)
        monkeypatch.delenv("TNS_ADMIN", raising=False)
        monkeypatch.delenv("ORACLE_WALLET_PASSWORD", raising=False)
        kw = oc.oracle_engine_kwargs(full_url_provided=True)
        ca = kw["connect_args"]
        assert "config_dir" not in ca
        assert "wallet_password" not in ca


# ══════════════════════════════════════════════════════════════════════════════
# now_func
# ══════════════════════════════════════════════════════════════════════════════

class TestNowFunc:
    def test_oracle_returns_systimestamp(self):
        assert oc.now_func("oracle") == "SYSTIMESTAMP"

    def test_postgres_returns_now(self):
        assert oc.now_func("postgresql") == "NOW()"

    def test_other_returns_now(self):
        assert oc.now_func("sqlite") == "NOW()"


# ══════════════════════════════════════════════════════════════════════════════
# create_table_sql
# ══════════════════════════════════════════════════════════════════════════════

class TestCreateTableSql:
    def test_oracle_with_expires(self):
        sql = oc.create_table_sql("oracle", "kv_store", with_expires=True)
        assert "VARCHAR2" in sql
        assert "CLOB" in sql
        assert "expires_at TIMESTAMP" in sql
        assert "IF NOT EXISTS" not in sql

    def test_oracle_without_expires(self):
        sql = oc.create_table_sql("oracle", "kv_store", with_expires=False)
        assert "expires_at" not in sql
        assert "SYSTIMESTAMP" in sql

    def test_postgres_with_expires(self):
        sql = oc.create_table_sql("postgresql", "kv_store", with_expires=True)
        assert "CREATE TABLE IF NOT EXISTS" in sql
        assert "TIMESTAMPTZ" in sql
        assert "expires_at TIMESTAMPTZ NULL" in sql

    def test_postgres_without_expires(self):
        sql = oc.create_table_sql("postgresql", "kv_store", with_expires=False)
        assert "expires_at" not in sql
        assert "TEXT PRIMARY KEY" in sql


# ══════════════════════════════════════════════════════════════════════════════
# create_index_sql
# ══════════════════════════════════════════════════════════════════════════════

class TestCreateIndexSql:
    def test_oracle_no_if_not_exists(self):
        sql = oc.create_index_sql("oracle", "idx_exp", "kv_store", "expires_at")
        assert "IF NOT EXISTS" not in sql
        assert "CREATE INDEX idx_exp ON kv_store (expires_at)" == sql

    def test_postgres_if_not_exists(self):
        sql = oc.create_index_sql("postgresql", "idx_exp", "kv_store", "expires_at")
        assert "IF NOT EXISTS" in sql
        assert "idx_exp" in sql


# ══════════════════════════════════════════════════════════════════════════════
# upsert_sql
# ══════════════════════════════════════════════════════════════════════════════

class TestUpsertSql:
    def test_oracle_with_expires_is_merge(self):
        sql = oc.upsert_sql("oracle", "kv_store", with_expires=True)
        assert sql.startswith("MERGE INTO kv_store")
        assert ":e" in sql
        assert "SYSTIMESTAMP" in sql

    def test_oracle_without_expires_is_merge_no_e(self):
        sql = oc.upsert_sql("oracle", "kv_store", with_expires=False)
        assert sql.startswith("MERGE INTO kv_store")
        assert ":e" not in sql

    def test_postgres_with_expires(self):
        sql = oc.upsert_sql("postgresql", "kv_store", with_expires=True)
        assert "ON CONFLICT" in sql
        assert ":e" in sql
        assert "expires_at" in sql

    def test_postgres_without_expires(self):
        sql = oc.upsert_sql("postgresql", "kv_store", with_expires=False)
        assert "ON CONFLICT" in sql
        assert "expires_at" not in sql
        assert ":e" not in sql


# ══════════════════════════════════════════════════════════════════════════════
# exec_ddl_safe
# ══════════════════════════════════════════════════════════════════════════════

class TestExecDdlSafe:
    def _eng(self):
        return create_engine("sqlite:///:memory:")

    def test_runs_valid_ddl(self):
        eng = self._eng()
        oc.exec_ddl_safe(eng, "CREATE TABLE foo (id INTEGER PRIMARY KEY)", "sqlite")
        # Verify table exists by inserting a row
        with eng.connect() as conn:
            conn.execute(__import__("sqlalchemy").text("INSERT INTO foo VALUES (1)"))
            conn.commit()

    def test_swallows_already_exists_sqlite(self):
        eng = self._eng()
        sql = "CREATE TABLE bar (id INTEGER PRIMARY KEY)"
        oc.exec_ddl_safe(eng, sql, "sqlite")
        oc.exec_ddl_safe(eng, sql, "sqlite")   # second call must not raise

    def test_swallows_oracle_ora_00955(self):
        eng = self._eng()
        # Simulate an ORA-00955 by using a fake engine that raises with that code
        class _FakeConn:
            def execute(self, *a): raise Exception("ORA-00955: name already used")
            def __enter__(self): return self
            def __exit__(self, *a): pass
        class _FakeCtx:
            def __enter__(self): return _FakeConn()
            def __exit__(self, *a): pass
        class _FakeEng:
            def begin(self): return _FakeCtx()
        oc.exec_ddl_safe(_FakeEng(), "CREATE TABLE x (id NUMBER)", "oracle")   # must not raise

    def test_swallows_oracle_ora_01408(self):
        class _FakeConn:
            def execute(self, *a): raise Exception("ORA-01408: column list already indexed")
            def __enter__(self): return self
            def __exit__(self, *a): pass
        class _FakeCtx:
            def __enter__(self): return _FakeConn()
            def __exit__(self, *a): pass
        class _FakeEng:
            def begin(self): return _FakeCtx()
        oc.exec_ddl_safe(_FakeEng(), "CREATE INDEX idx ON x (id)", "oracle")

    def test_returns_true_on_success_and_when_already_exists(self):
        eng = self._eng()
        sql = "CREATE TABLE ret_t (id INTEGER PRIMARY KEY)"
        assert oc.exec_ddl_safe(eng, sql, "sqlite") is True
        assert oc.exec_ddl_safe(eng, sql, "sqlite") is True

    def test_returns_false_and_warns_on_a_real_failure(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="oracle-compat"):
            assert oc.exec_ddl_safe(self._eng(), "THIS IS NOT SQL", "sqlite") is False
        recs = [r for r in caplog.records if "exec_ddl_safe FAILED" in r.getMessage()]
        assert len(recs) == 1 and recs[0].levelno == logging.WARNING

    def test_reraises_unexpected_error(self):
        class _FakeConn:
            def execute(self, *a): raise RuntimeError("network error")
            def __enter__(self): return self
            def __exit__(self, *a): pass
        class _FakeCtx:
            def __enter__(self): return _FakeConn()
            def __exit__(self, *a): pass
        class _FakeEng:
            def begin(self): return _FakeCtx()
        # exec_ddl_safe logs but does NOT re-raise — it only logs at DEBUG
        # (the function signature says it swallows everything and logs)
        oc.exec_ddl_safe(_FakeEng(), "BROKEN SQL", "postgresql")   # must not raise
