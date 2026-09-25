"""
tests/test_oracle_compat.py — 100 % statement + branch coverage of
position-stocks-service/oracle_compat.py (was 15 %).

Strategy
--------
* Pure functions (oracle_is_configured, dialect_name, oracle_engine_kwargs,
  now_func, create_*_sql, upsert_sql) are tested directly against os.environ
  (via monkeypatch — nothing leaks between tests) and against *exact* expected
  strings, so any change to the emitted SQL is caught.
* Everything that needs a SQLAlchemy engine uses a REAL in-memory SQLite engine
  (same "real DB, no ORM mocking" idiom the rest of this suite uses).  The
  Postgres upsert SQL is even executed for real on SQLite (which supports
  ON CONFLICT ... EXCLUDED) with a NOW() function registered on the connection.
* Oracle itself is never dialled.  `build_oracle_engine` is exercised two ways:
  with `sqlalchemy.create_engine` replaced by a recorder (to assert exactly what
  is passed), and — when python-oracledb is installed — with the REAL
  create_engine, which builds the engine lazily without connecting.
* The `connect` listener installed by `_attach_call_timeout` is fired through
  the pool's event dispatch (looked up by name) against a fake DBAPI connection.

The module has no module-level state except `_ORACLE_LOB_CONFIGURED`, which the
`m` fixture resets around every test (no sys.modules juggling).
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

import oracle_compat  # noqa: E402

_ENV_KEYS = (
    "ORACLE_DSN", "ORACLE_USER", "ORACLE_PASSWORD", "ORACLE_ADMIN_PASSWORD",
    "ORACLE_WALLET_DIR", "TNS_ADMIN", "ORACLE_WALLET_PASSWORD",
    "ORACLE_CALL_TIMEOUT_MS",
    "DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_RECYCLE", "DB_POOL_TIMEOUT",
)


@pytest.fixture
def m(monkeypatch):
    """The module under test with a clean environment and LOB flag reset."""
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(oracle_compat, "_ORACLE_LOB_CONFIGURED", False)
    return oracle_compat


def _sqlite():
    return create_engine("sqlite:///:memory:")


def _raising_engine(message):
    """Fake engine whose begin()/execute() raises Exception(message)."""
    eng = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.execute.side_effect = Exception(message)
    eng.begin.return_value = ctx
    return eng, ctx


# ─── oracle_is_configured ───────────────────────────────────────────────────

class TestOracleIsConfigured:
    def test_true_for_oracle_url(self, m):
        assert m.oracle_is_configured("oracle+oracledb://host/db") is True

    def test_true_for_bare_oracle_scheme(self, m):
        assert m.oracle_is_configured("oracle://host/db") is True

    def test_true_for_uppercase_url(self, m):
        assert m.oracle_is_configured("ORACLE+ORACLEDB://host/db") is True

    def test_false_for_postgres_url(self, m):
        assert m.oracle_is_configured("postgresql://host/db") is False

    def test_false_for_url_merely_containing_oracle(self, m):
        # must be the *scheme* (startswith), not a substring match
        assert m.oracle_is_configured("postgresql://oracle-host/db") is False

    def test_false_for_empty_string(self, m):
        assert m.oracle_is_configured("") is False

    def test_false_for_no_arg(self, m):
        assert m.oracle_is_configured() is False

    def test_false_for_none_url(self, m):
        assert m.oracle_is_configured(None) is False

    def test_true_when_dsn_env_set_even_with_postgres_url(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_DSN", "stockkydb_high")
        assert m.oracle_is_configured("postgresql://host/db") is True

    def test_true_when_dsn_env_set_and_no_url(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_DSN", "stockkydb_high")
        assert m.oracle_is_configured() is True

    def test_empty_dsn_env_is_not_configured(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_DSN", "")
        assert m.oracle_is_configured() is False

    def test_exception_is_swallowed_and_returns_false(self, m):
        def boom(key, default=None):
            raise RuntimeError("env boom")

        with patch.object(os.environ, "get", side_effect=boom):
            assert m.oracle_is_configured() is False

    def test_non_string_url_returns_false_instead_of_raising(self, m):
        # .lower() on a non-str raises AttributeError -> caught -> False
        assert m.oracle_is_configured(12345) is False


# ─── dialect_name / is_oracle_engine ────────────────────────────────────────

class TestDialectHelpers:
    def test_dialect_name_sqlite(self, m):
        assert m.dialect_name(_sqlite()) == "sqlite"

    def test_dialect_name_lowercases(self, m):
        eng = MagicMock()
        eng.dialect.name = "ORACLE"
        assert m.dialect_name(eng) == "oracle"

    def test_dialect_name_none_becomes_empty_string(self, m):
        eng = MagicMock()
        eng.dialect.name = None
        assert m.dialect_name(eng) == ""

    def test_dialect_name_missing_dialect_attr_returns_empty(self, m):
        eng = MagicMock()
        del eng.dialect  # AttributeError on access
        assert m.dialect_name(eng) == ""

    def test_dialect_name_for_none_engine_returns_empty(self, m):
        assert m.dialect_name(None) == ""

    def test_is_oracle_engine_true(self, m):
        eng = MagicMock()
        eng.dialect.name = "oracle"
        assert m.is_oracle_engine(eng) is True

    def test_is_oracle_engine_false_for_sqlite(self, m):
        assert m.is_oracle_engine(_sqlite()) is False

    def test_is_oracle_engine_false_for_broken_engine(self, m):
        assert m.is_oracle_engine(None) is False


# ─── _configure_oracle_lobs ─────────────────────────────────────────────────

class _RecordingDefaults:
    """Stands in for oracledb.defaults; records every attribute write."""

    def __init__(self):
        object.__setattr__(self, "writes", [])

    def __setattr__(self, name, value):
        self.writes.append((name, value))


class _FakeOracledb:
    def __init__(self):
        self.defaults = _RecordingDefaults()


class TestConfigureOracleLobs:
    def test_sets_fetch_lobs_false_and_marks_configured(self, m):
        fake = _FakeOracledb()
        with patch.dict(sys.modules, {"oracledb": fake}):
            m._configure_oracle_lobs()
        assert fake.defaults.writes == [("fetch_lobs", False)]
        assert m._ORACLE_LOB_CONFIGURED is True

    def test_second_call_is_a_noop(self, m):
        fake = _FakeOracledb()
        with patch.dict(sys.modules, {"oracledb": fake}):
            m._configure_oracle_lobs()
            m._configure_oracle_lobs()
            m._configure_oracle_lobs()
        assert fake.defaults.writes == [("fetch_lobs", False)]  # exactly once

    def test_already_configured_never_touches_oracledb(self, m, monkeypatch):
        monkeypatch.setattr(m, "_ORACLE_LOB_CONFIGURED", True)
        fake = _FakeOracledb()
        with patch.dict(sys.modules, {"oracledb": fake}):
            m._configure_oracle_lobs()
        assert fake.defaults.writes == []

    def test_oracledb_not_installed_is_swallowed_and_not_marked(self, m):
        # sys.modules[name] = None makes `import oracledb` raise ImportError
        with patch.dict(sys.modules, {"oracledb": None}):
            m._configure_oracle_lobs()  # must not raise
        assert m._ORACLE_LOB_CONFIGURED is False  # retried on a later call

    def test_not_installed_then_installed_configures_on_retry(self, m):
        with patch.dict(sys.modules, {"oracledb": None}):
            m._configure_oracle_lobs()
        assert m._ORACLE_LOB_CONFIGURED is False
        fake = _FakeOracledb()
        with patch.dict(sys.modules, {"oracledb": fake}):
            m._configure_oracle_lobs()
        assert m._ORACLE_LOB_CONFIGURED is True
        assert fake.defaults.writes == [("fetch_lobs", False)]

    def test_old_oracledb_without_defaults_still_marks_configured(self, m):
        class _Old:  # no .defaults attribute at all
            pass

        with patch.dict(sys.modules, {"oracledb": _Old()}):
            m._configure_oracle_lobs()  # AttributeError swallowed by inner try
        assert m._ORACLE_LOB_CONFIGURED is True

    def test_fetch_lobs_setter_raising_is_swallowed_and_still_configured(self, m):
        class _ReadOnlyDefaults:
            def __setattr__(self, name, value):
                raise AttributeError("read-only")

        fake = MagicMock()
        fake.defaults = _ReadOnlyDefaults()
        with patch.dict(sys.modules, {"oracledb": fake}):
            m._configure_oracle_lobs()
        assert m._ORACLE_LOB_CONFIGURED is True

    def test_real_oracledb_gets_fetch_lobs_false(self, m):
        odb = pytest.importorskip("oracledb")
        before = odb.defaults.fetch_lobs
        try:
            odb.defaults.fetch_lobs = True
            m._configure_oracle_lobs()
            assert odb.defaults.fetch_lobs is False
            assert m._ORACLE_LOB_CONFIGURED is True
        finally:
            odb.defaults.fetch_lobs = before


# ─── oracle_engine_kwargs ────────────────────────────────────────────────────

class TestOracleEngineKwargs:
    def test_full_url_skips_user_password_dsn(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_USER", "SOMEONE")
        monkeypatch.setenv("ORACLE_PASSWORD", "pw")
        monkeypatch.setenv("ORACLE_DSN", "dsn")
        ca = m.oracle_engine_kwargs(full_url_provided=True)["connect_args"]
        assert "user" not in ca
        assert "password" not in ca
        assert "dsn" not in ca

    def test_discrete_vars_with_password(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_USER", "MYUSER")
        monkeypatch.setenv("ORACLE_PASSWORD", "secret")
        monkeypatch.setenv("ORACLE_DSN", "stockkydb_high")
        ca = m.oracle_engine_kwargs(full_url_provided=False)["connect_args"]
        assert ca == {"user": "MYUSER", "password": "secret", "dsn": "stockkydb_high"}

    def test_defaults_user_admin_empty_dsn_and_no_password_key(self, m):
        ca = m.oracle_engine_kwargs(full_url_provided=False)["connect_args"]
        assert ca == {"user": "ADMIN", "dsn": ""}
        assert "password" not in ca

    def test_admin_password_fallback(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_ADMIN_PASSWORD", "adminpass")
        ca = m.oracle_engine_kwargs(full_url_provided=False)["connect_args"]
        assert ca["password"] == "adminpass"

    def test_oracle_password_beats_admin_password(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_PASSWORD", "primary")
        monkeypatch.setenv("ORACLE_ADMIN_PASSWORD", "fallback")
        ca = m.oracle_engine_kwargs(full_url_provided=False)["connect_args"]
        assert ca["password"] == "primary"

    def test_empty_oracle_password_falls_back_to_admin_password(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_PASSWORD", "")
        monkeypatch.setenv("ORACLE_ADMIN_PASSWORD", "fallback")
        ca = m.oracle_engine_kwargs(full_url_provided=False)["connect_args"]
        assert ca["password"] == "fallback"

    def test_wallet_dir_and_password(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_WALLET_DIR", "/wallets/dir")
        monkeypatch.setenv("ORACLE_WALLET_PASSWORD", "wp123")
        ca = m.oracle_engine_kwargs(full_url_provided=False)["connect_args"]
        assert ca["config_dir"] == "/wallets/dir"
        assert ca["wallet_location"] == "/wallets/dir"
        assert ca["wallet_password"] == "wp123"

    def test_wallet_settings_apply_to_full_url_form_too(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_WALLET_DIR", "/w")
        monkeypatch.setenv("ORACLE_WALLET_PASSWORD", "wp")
        ca = m.oracle_engine_kwargs(full_url_provided=True)["connect_args"]
        assert ca == {"config_dir": "/w", "wallet_location": "/w", "wallet_password": "wp"}

    def test_wallet_dir_falls_back_to_tns_admin(self, m, monkeypatch):
        monkeypatch.setenv("TNS_ADMIN", "/tns/admin")
        ca = m.oracle_engine_kwargs(full_url_provided=False)["connect_args"]
        assert ca["config_dir"] == "/tns/admin"
        assert ca["wallet_location"] == "/tns/admin"

    def test_oracle_wallet_dir_beats_tns_admin(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_WALLET_DIR", "/primary")
        monkeypatch.setenv("TNS_ADMIN", "/fallback")
        ca = m.oracle_engine_kwargs(full_url_provided=False)["connect_args"]
        assert ca["config_dir"] == "/primary"

    def test_wallet_password_without_wallet_dir(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_WALLET_PASSWORD", "wp")
        ca = m.oracle_engine_kwargs(full_url_provided=True)["connect_args"]
        assert ca == {"wallet_password": "wp"}

    def test_no_wallet_env_no_wallet_keys(self, m):
        ca = m.oracle_engine_kwargs(full_url_provided=True)["connect_args"]
        assert ca == {}

    def test_pool_defaults(self, m):
        kw = m.oracle_engine_kwargs(full_url_provided=True)
        assert kw["pool_size"] == 3
        assert kw["max_overflow"] == 2
        assert kw["pool_recycle"] == 300
        assert kw["pool_timeout"] == 30

    def test_pool_values_from_env_are_ints(self, m, monkeypatch):
        monkeypatch.setenv("DB_POOL_SIZE", "10")
        monkeypatch.setenv("DB_MAX_OVERFLOW", "5")
        monkeypatch.setenv("DB_POOL_RECYCLE", "600")
        monkeypatch.setenv("DB_POOL_TIMEOUT", "45")
        kw = m.oracle_engine_kwargs(full_url_provided=True)
        assert (kw["pool_size"], kw["max_overflow"], kw["pool_recycle"], kw["pool_timeout"]) \
            == (10, 5, 600, 45)

    def test_pool_overrides_from_kwargs(self, m):
        kw = m.oracle_engine_kwargs(
            full_url_provided=True,
            db_pool_size=7, db_max_overflow=1, db_pool_recycle=99, db_pool_timeout=60,
        )
        assert (kw["pool_size"], kw["max_overflow"], kw["pool_recycle"], kw["pool_timeout"]) \
            == (7, 1, 99, 60)

    def test_kwarg_override_beats_env(self, m, monkeypatch):
        monkeypatch.setenv("DB_POOL_SIZE", "10")
        kw = m.oracle_engine_kwargs(full_url_provided=True, db_pool_size=2)
        assert kw["pool_size"] == 2

    def test_echo_false_and_pre_ping_true(self, m):
        kw = m.oracle_engine_kwargs(full_url_provided=True)
        assert kw["echo"] is False
        assert kw["pool_pre_ping"] is True


# ─── build_oracle_engine ─────────────────────────────────────────────────────

class TestBuildOracleEngine:
    @pytest.fixture
    def rec(self, m):
        """Patch sqlalchemy.create_engine + the two side-effect helpers."""
        eng = MagicMock(name="engine")
        with patch("sqlalchemy.create_engine", return_value=eng) as ce, \
             patch.object(m, "_configure_oracle_lobs") as lobs, \
             patch.object(m, "_attach_call_timeout") as attach:
            yield type("Rec", (), {"engine": eng, "create": ce, "lobs": lobs, "attach": attach})

    def test_empty_url_uses_sentinel_and_connect_args(self, m, rec, monkeypatch):
        monkeypatch.setenv("ORACLE_DSN", "stockkydb_high")
        monkeypatch.setenv("ORACLE_PASSWORD", "pw")
        eng, url = m.build_oracle_engine("")
        assert eng is rec.engine
        assert url == "oracle+oracledb://"
        assert rec.create.call_args[0][0] == "oracle+oracledb://"
        ca = rec.create.call_args[1]["connect_args"]
        assert ca["dsn"] == "stockkydb_high" and ca["password"] == "pw"

    def test_no_arg_behaves_like_empty(self, m, rec):
        _, url = m.build_oracle_engine()
        assert url == "oracle+oracledb://"

    def test_none_url_behaves_like_empty(self, m, rec):
        _, url = m.build_oracle_engine(None)
        assert url == "oracle+oracledb://"

    def test_full_oracle_url_passed_through_without_user_dsn(self, m, rec, monkeypatch):
        monkeypatch.setenv("ORACLE_DSN", "should_be_ignored")
        full = "oracle+oracledb://user:pass@mydsn"
        eng, url = m.build_oracle_engine(full)
        assert url == full
        assert rec.create.call_args[0][0] == full
        ca = rec.create.call_args[1]["connect_args"]
        assert "user" not in ca and "dsn" not in ca and "password" not in ca

    def test_scheme_only_oracle_url_is_not_full(self, m, rec):
        _, url = m.build_oracle_engine("oracle+oracledb://")
        assert url == "oracle+oracledb://"
        assert "user" in rec.create.call_args[1]["connect_args"]

    def test_whitespace_body_is_not_full(self, m, rec):
        _, url = m.build_oracle_engine("oracle+oracledb://   ")
        assert url == "oracle+oracledb://"
        assert "dsn" in rec.create.call_args[1]["connect_args"]

    def test_non_oracle_url_is_replaced_by_sentinel(self, m, rec):
        _, url = m.build_oracle_engine("postgresql://user:pw@host/db")
        assert url == "oracle+oracledb://"
        assert rec.create.call_args[0][0] == "oracle+oracledb://"
        assert "user" in rec.create.call_args[1]["connect_args"]

    def test_url_without_scheme_separator_is_replaced_by_sentinel(self, m, rec):
        _, url = m.build_oracle_engine("just-a-dsn-alias")
        assert url == "oracle+oracledb://"

    def test_uppercase_oracle_url_counts_as_full(self, m, rec):
        full = "ORACLE+ORACLEDB://u:p@dsn"
        _, url = m.build_oracle_engine(full)
        assert url == full

    def test_pool_overrides_reach_create_engine(self, m, rec):
        m.build_oracle_engine("", db_pool_size=9, db_pool_timeout=11)
        kw = rec.create.call_args[1]
        assert kw["pool_size"] == 9 and kw["pool_timeout"] == 11
        assert kw["pool_pre_ping"] is True

    def test_configures_lobs_and_attaches_timeout_to_created_engine(self, m, rec):
        m.build_oracle_engine("")
        rec.lobs.assert_called_once_with()
        rec.attach.assert_called_once_with(rec.engine)

    def test_logs_dsn_and_wallet(self, m, rec, monkeypatch, caplog):
        monkeypatch.setenv("ORACLE_DSN", "stockkydb_high")
        monkeypatch.setenv("TNS_ADMIN", "/tns")
        with caplog.at_level(logging.INFO, logger="oracle-compat"):
            m.build_oracle_engine("")
        assert "dsn=stockkydb_high" in caplog.text
        assert "wallet=/tns" in caplog.text

    def test_logs_from_url_and_no_wallet_defaults(self, m, rec, caplog):
        with caplog.at_level(logging.INFO, logger="oracle-compat"):
            m.build_oracle_engine("oracle+oracledb://u:p@dsn")
        assert "dsn=from-url" in caplog.text
        assert "wallet=none" in caplog.text

    def test_real_create_engine_builds_lazily_without_connecting(self, m, monkeypatch):
        pytest.importorskip("oracledb")
        monkeypatch.setenv("ORACLE_DSN", "stockkydb_high")
        monkeypatch.setenv("ORACLE_PASSWORD", "pw")
        monkeypatch.setattr(m, "_configure_oracle_lobs", lambda: None)
        eng, url = m.build_oracle_engine("", db_pool_size=4, db_max_overflow=1)
        try:
            assert url == "oracle+oracledb://"
            assert eng.dialect.name == "oracle"
            assert m.is_oracle_engine(eng) is True
            assert eng.pool.size() == 4
            assert eng.pool._max_overflow == 1
            assert len(eng.pool.dispatch.connect) >= 1  # timeout listener attached
        finally:
            eng.dispose()


# ─── _attach_call_timeout ────────────────────────────────────────────────────

class TestAttachCallTimeout:
    @staticmethod
    def _fire_connect(eng, dbapi_conn):
        """Fire the listener `_attach_call_timeout` registered through
        SQLAlchemy's event API (engine-level 'connect' listeners live on the
        pool's dispatch).  The dialect's own connect hooks are skipped — they
        expect a real DBAPI connection — so pick ours out by name."""
        ours = [fn for fn in eng.pool.dispatch.connect.listeners
                if getattr(fn, "__name__", "") == "_set_call_timeout"]
        assert len(ours) == 1, "expected exactly one call-timeout listener"
        ours[0](dbapi_conn, MagicMock())

    def test_registers_one_connect_listener(self, m):
        eng = _sqlite()
        before = len(eng.pool.dispatch.connect)
        m._attach_call_timeout(eng)
        assert len(eng.pool.dispatch.connect) == before + 1

    def test_default_timeout_8000ms_set_on_dbapi_connection(self, m):
        eng = _sqlite()
        m._attach_call_timeout(eng)
        conn = MagicMock()
        self._fire_connect(eng, conn)
        assert conn.call_timeout == 8000

    def test_custom_timeout_from_env(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_CALL_TIMEOUT_MS", "5000")
        eng = _sqlite()
        m._attach_call_timeout(eng)
        conn = MagicMock()
        self._fire_connect(eng, conn)
        assert conn.call_timeout == 5000

    def test_timeout_is_read_when_attached_not_when_fired(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_CALL_TIMEOUT_MS", "1234")
        eng = _sqlite()
        m._attach_call_timeout(eng)
        monkeypatch.setenv("ORACLE_CALL_TIMEOUT_MS", "9999")
        conn = MagicMock()
        self._fire_connect(eng, conn)
        assert conn.call_timeout == 1234

    def test_setter_raising_is_swallowed_and_logged_at_debug(self, m, caplog):
        eng = _sqlite()
        m._attach_call_timeout(eng)

        class _RaisingSetterDescriptor:
            """Only the setter path is ever exercised (the listener writes
            conn.call_timeout, never reads it) -- a plain data descriptor
            with just __set__ avoids needing a dead getter."""
            def __set__(self, obj, value):
                raise AttributeError("not supported")

        class _NoTimeout:
            call_timeout = _RaisingSetterDescriptor()

        with caplog.at_level(logging.DEBUG, logger="oracle-compat"):
            self._fire_connect(eng, _NoTimeout())  # must not raise
        assert "could not set oracledb call_timeout" in caplog.text

    def test_real_sqlite_connection_still_works_after_attach(self, m):
        # sqlite3.Connection has no call_timeout -> AttributeError swallowed
        eng = _sqlite()
        m._attach_call_timeout(eng)
        with eng.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1

    def test_invalid_timeout_env_is_swallowed_and_registers_nothing(self, m, monkeypatch, caplog):
        monkeypatch.setenv("ORACLE_CALL_TIMEOUT_MS", "not-a-number")
        eng = _sqlite()
        before = len(eng.pool.dispatch.connect)
        with caplog.at_level(logging.WARNING, logger="oracle-compat"):
            m._attach_call_timeout(eng)  # must not raise
        assert len(eng.pool.dispatch.connect) == before
        assert "_attach_call_timeout setup failed" in caplog.text

    def test_non_engine_target_is_swallowed(self, m, caplog):
        with caplog.at_level(logging.WARNING, logger="oracle-compat"):
            m._attach_call_timeout(object())  # event.listens_for rejects it
        assert "_attach_call_timeout setup failed" in caplog.text


# ─── now_func ────────────────────────────────────────────────────────────────

class TestNowFunc:
    def test_oracle_returns_systimestamp(self, m):
        assert m.now_func("oracle") == "SYSTIMESTAMP"

    def test_postgres_returns_now(self, m):
        assert m.now_func("postgresql") == "NOW()"

    def test_sqlite_returns_now(self, m):
        assert m.now_func("sqlite") == "NOW()"

    def test_empty_dialect_returns_now(self, m):
        assert m.now_func("") == "NOW()"


# ─── create_table_sql ────────────────────────────────────────────────────────

class TestCreateTableSql:
    def test_oracle_without_expires(self, m):
        assert m.create_table_sql("oracle", "my_kv", with_expires=False) == (
            "CREATE TABLE my_kv (k VARCHAR2(1000) PRIMARY KEY, v CLOB, "
            "updated_at TIMESTAMP DEFAULT SYSTIMESTAMP)"
        )

    def test_oracle_with_expires(self, m):
        assert m.create_table_sql("oracle", "my_kv", with_expires=True) == (
            "CREATE TABLE my_kv (k VARCHAR2(1000) PRIMARY KEY, v CLOB, "
            "expires_at TIMESTAMP, updated_at TIMESTAMP DEFAULT SYSTIMESTAMP)"
        )

    def test_oracle_has_no_if_not_exists_and_clob_is_nullable(self, m):
        sql = m.create_table_sql("oracle", "t", with_expires=True)
        assert "IF NOT EXISTS" not in sql
        assert "CLOB NOT NULL" not in sql

    def test_postgres_without_expires(self, m):
        assert m.create_table_sql("postgresql", "my_kv", with_expires=False) == (
            "CREATE TABLE IF NOT EXISTS my_kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, "
            "updated_at TIMESTAMPTZ DEFAULT NOW())"
        )

    def test_postgres_with_expires(self, m):
        assert m.create_table_sql("postgresql", "my_kv", with_expires=True) == (
            "CREATE TABLE IF NOT EXISTS my_kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, "
            "expires_at TIMESTAMPTZ NULL, updated_at TIMESTAMPTZ DEFAULT NOW())"
        )

    def test_any_non_oracle_dialect_gets_postgres_flavour(self, m):
        assert m.create_table_sql("sqlite", "t", False) == m.create_table_sql("postgresql", "t", False)


# ─── create_index_sql ────────────────────────────────────────────────────────

class TestCreateIndexSql:
    def test_oracle_no_if_not_exists(self, m):
        assert m.create_index_sql("oracle", "idx_k", "my_kv", "k") == "CREATE INDEX idx_k ON my_kv (k)"

    def test_postgres_has_if_not_exists(self, m):
        assert m.create_index_sql("postgresql", "idx_k", "my_kv", "k") == \
            "CREATE INDEX IF NOT EXISTS idx_k ON my_kv (k)"

    def test_index_sql_really_creates_an_index_on_sqlite(self, m):
        eng = _sqlite()
        with eng.begin() as c:
            c.execute(text("CREATE TABLE my_kv (k TEXT PRIMARY KEY, v TEXT)"))
            c.execute(text(m.create_index_sql("sqlite", "idx_v", "my_kv", "v")))
            c.execute(text(m.create_index_sql("sqlite", "idx_v", "my_kv", "v")))  # idempotent
            names = [r[0] for r in c.execute(text(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_v'"))]
        assert names == ["idx_v"]


# ─── upsert_sql ──────────────────────────────────────────────────────────────

class TestUpsertSql:
    def test_oracle_without_expires(self, m):
        assert m.upsert_sql("oracle", "my_kv", with_expires=False) == (
            "MERGE INTO my_kv d USING (SELECT :k AS k, :v AS v FROM dual) s ON (d.k = s.k) "
            "WHEN MATCHED THEN UPDATE SET d.v = s.v, d.updated_at = SYSTIMESTAMP "
            "WHEN NOT MATCHED THEN INSERT (k, v, updated_at) VALUES (s.k, s.v, SYSTIMESTAMP)"
        )

    def test_oracle_with_expires(self, m):
        assert m.upsert_sql("oracle", "my_kv", with_expires=True) == (
            "MERGE INTO my_kv d USING (SELECT :k AS k, :v AS v, :e AS e FROM dual) s ON (d.k = s.k) "
            "WHEN MATCHED THEN UPDATE SET d.v = s.v, d.expires_at = s.e, d.updated_at = SYSTIMESTAMP "
            "WHEN NOT MATCHED THEN INSERT (k, v, expires_at, updated_at) "
            "VALUES (s.k, s.v, s.e, SYSTIMESTAMP)"
        )

    def test_postgres_without_expires(self, m):
        assert m.upsert_sql("postgresql", "my_kv", with_expires=False) == (
            "INSERT INTO my_kv (k, v, updated_at) VALUES (:k, :v, NOW()) "
            "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated_at = NOW()"
        )

    def test_postgres_with_expires(self, m):
        assert m.upsert_sql("postgresql", "my_kv", with_expires=True) == (
            "INSERT INTO my_kv (k, v, expires_at, updated_at) VALUES (:k, :v, :e, NOW()) "
            "ON CONFLICT (k) DO UPDATE "
            "SET v = EXCLUDED.v, expires_at = EXCLUDED.expires_at, updated_at = NOW()"
        )

    def test_oracle_sql_only_binds_e_when_with_expires(self, m):
        assert ":e" not in m.upsert_sql("oracle", "t", False)
        assert ":e" in m.upsert_sql("oracle", "t", True)

    @pytest.fixture
    def kv_engine(self):
        """SQLite engine with the NOW() function Postgres SQL expects."""
        from sqlalchemy import event

        eng = _sqlite()

        @event.listens_for(eng, "connect")
        def _reg(dbapi_conn, _rec):
            dbapi_conn.create_function("NOW", 0, lambda: "2026-01-01 00:00:00")

        with eng.begin() as c:
            c.execute(text("CREATE TABLE my_kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, "
                           "expires_at TEXT NULL, updated_at TEXT)"))
        return eng

    def test_postgres_upsert_really_inserts_then_updates_on_sqlite(self, m, kv_engine):
        sql = text(m.upsert_sql("postgresql", "my_kv", with_expires=False))
        with kv_engine.begin() as c:
            c.execute(sql, {"k": "a", "v": "1"})
            c.execute(sql, {"k": "a", "v": "2"})
            c.execute(sql, {"k": "b", "v": "3"})
            rows = c.execute(text("SELECT k, v, updated_at FROM my_kv ORDER BY k")).all()
        assert rows == [("a", "2", "2026-01-01 00:00:00"), ("b", "3", "2026-01-01 00:00:00")]

    def test_postgres_upsert_with_expires_really_updates_expiry_on_sqlite(self, m, kv_engine):
        sql = text(m.upsert_sql("postgresql", "my_kv", with_expires=True))
        with kv_engine.begin() as c:
            c.execute(sql, {"k": "a", "v": "1", "e": "2026-02-01"})
            c.execute(sql, {"k": "a", "v": "9", "e": "2026-03-01"})
            row = c.execute(text("SELECT v, expires_at FROM my_kv WHERE k='a'")).one()
        assert tuple(row) == ("9", "2026-03-01")


# ─── exec_ddl_safe ───────────────────────────────────────────────────────────

class TestExecDdlSafe:
    def test_executes_ddl_for_real_on_sqlite(self, m):
        eng = _sqlite()
        m.exec_ddl_safe(eng, "CREATE TABLE test_kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)", "sqlite")
        with eng.begin() as conn:
            conn.execute(text("INSERT INTO test_kv(k, v) VALUES ('a', 'b')"))
            assert conn.execute(text("SELECT v FROM test_kv WHERE k='a'")).scalar() == "b"

    def test_runs_in_its_own_begin_transaction(self, m):
        eng, ctx = _raising_engine("x")
        ctx.execute.side_effect = None
        m.exec_ddl_safe(eng, "CREATE TABLE x (k TEXT)", "postgresql")
        eng.begin.assert_called_once_with()
        assert ctx.execute.call_count == 1
        assert str(ctx.execute.call_args[0][0]) == "CREATE TABLE x (k TEXT)"

    def test_swallows_already_exists_on_real_sqlite_second_run(self, m, caplog):
        eng = _sqlite()
        sql = "CREATE TABLE test_kv2 (k TEXT PRIMARY KEY)"
        m.exec_ddl_safe(eng, sql, "sqlite")
        with caplog.at_level(logging.DEBUG, logger="oracle-compat"):
            m.exec_ddl_safe(eng, sql, "sqlite")  # 'table ... already exists'
        assert "exec_ddl_safe skip" not in caplog.text  # benign -> silent
        with eng.connect() as conn:  # table intact, engine still usable
            assert conn.execute(text("SELECT COUNT(*) FROM test_kv2")).scalar() == 0

    @pytest.mark.parametrize("code, msg", [
        ("ORA-00955", "name is already used by an existing object"),
        ("ORA-01408", "such column list already indexed"),
        ("ORA-00957", "duplicate column name"),
        ("ORA-02260", "table can have only one primary key"),
        ("ORA-02264", "name already used by an existing constraint"),
    ])
    def test_swallows_benign_oracle_codes_silently(self, m, caplog, code, msg):
        eng, _ = _raising_engine(f"{code}: {msg}")
        with caplog.at_level(logging.DEBUG, logger="oracle-compat"):
            m.exec_ddl_safe(eng, "DDL", "oracle")
        assert "exec_ddl_safe skip" not in caplog.text

    def test_oracle_code_on_non_oracle_dialect_is_logged_not_silent(self, m, caplog):
        # the ORA-* shortcut is gated on dialect == 'oracle'
        eng, _ = _raising_engine("ORA-00955: name is already used")
        with caplog.at_level(logging.DEBUG, logger="oracle-compat"):
            m.exec_ddl_safe(eng, "DDL", "postgresql")
        assert "exec_ddl_safe skip (postgresql)" in caplog.text

    def test_already_exists_message_swallowed_for_postgres(self, m, caplog):
        eng, _ = _raising_engine('relation "x" ALREADY EXISTS')  # case-insensitive
        with caplog.at_level(logging.DEBUG, logger="oracle-compat"):
            m.exec_ddl_safe(eng, "DDL", "postgresql")
        assert "exec_ddl_safe skip" not in caplog.text

    def test_already_exists_message_swallowed_for_oracle_dialect_too(self, m, caplog):
        eng, _ = _raising_engine("object already exists")
        with caplog.at_level(logging.DEBUG, logger="oracle-compat"):
            m.exec_ddl_safe(eng, "DDL", "oracle")
        assert "exec_ddl_safe skip" not in caplog.text

    def test_other_oracle_error_logged_at_debug_and_swallowed(self, m, caplog):
        eng, _ = _raising_engine("ORA-00001: unique constraint violated")
        with caplog.at_level(logging.DEBUG, logger="oracle-compat"):
            m.exec_ddl_safe(eng, "INSERT INTO x VALUES (1)", "oracle")  # no raise
        assert "exec_ddl_safe skip (oracle)" in caplog.text
        assert "ORA-00001" in caplog.text

    def test_unknown_postgres_error_logged_at_debug_and_swallowed(self, m, caplog):
        eng, _ = _raising_engine("syntax error near SELECT")
        with caplog.at_level(logging.DEBUG, logger="oracle-compat"):
            m.exec_ddl_safe(eng, "BAD SQL", "postgresql")
        assert "exec_ddl_safe skip (postgresql): syntax error near SELECT" in caplog.text

    def test_logged_message_truncated_to_160_chars(self, m, caplog):
        eng, _ = _raising_engine("E" * 500)
        with caplog.at_level(logging.DEBUG, logger="oracle-compat"):
            m.exec_ddl_safe(eng, "BAD", "postgresql")
        assert "E" * 160 in caplog.text
        assert "E" * 161 not in caplog.text

    def test_real_sqlite_syntax_error_is_swallowed(self, m):
        m.exec_ddl_safe(_sqlite(), "THIS IS NOT SQL", "sqlite")  # must not raise

    def test_error_raised_by_begin_itself_is_swallowed(self, m):
        eng = MagicMock()
        eng.begin.side_effect = Exception("connection refused")
        m.exec_ddl_safe(eng, "DDL", "postgresql")  # must not raise
