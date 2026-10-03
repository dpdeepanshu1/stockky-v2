"""
tests/test_oracle_compat.py — coverage for api-gateway/oracle_compat.py

Ported from analysis-intelligence-service/tests/test_oracle_compat.py after confirming the
gateway copy is byte-identical to that one (and to market-data-service's). Only the module
path and the run-from directory differ.

No database, no oracledb, no network. Every test loads a FRESH copy of the module
(so the module-level _ORACLE_LOB_CONFIGURED flag never leaks between tests) with
`sqlalchemy` and `oracledb` replaced by tiny fakes in sys.modules:

  * FakeSA      — text(), create_engine() (records url + kwargs) and
                  event.listens_for() (records the "connect" listener so the test
                  can fire it against a fake DBAPI connection).
  * FakeOracledb — a module with a `defaults` object, to observe fetch_lobs.

The SQL builders are pure functions and are pinned against EXACT expected strings,
so any change to the SQL this shim emits (which kv_cache.py binds :k/:v/:e against)
is caught here.

Run from services/api-gateway:
    python3 -m pytest tests/test_oracle_compat.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(os.path.dirname(_HERE), "oracle_compat.py")

_ENV_KEYS = (
    "ORACLE_DSN", "ORACLE_USER", "ORACLE_PASSWORD", "ORACLE_ADMIN_PASSWORD",
    "ORACLE_WALLET_DIR", "TNS_ADMIN", "ORACLE_WALLET_PASSWORD",
    "ORACLE_CALL_TIMEOUT_MS",
    "DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_RECYCLE", "DB_POOL_TIMEOUT",
)


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeText:
    def __init__(self, sql):
        self.sql = sql

    def __str__(self):
        return self.sql


class FakeSA:
    """Stand-in for the `sqlalchemy` module (+ `sqlalchemy.event`)."""

    def __init__(self):
        self.engine = object()
        self.create_calls = []
        self.create_raises = None
        self.listeners = []          # [(target, event_name, fn)]
        self.listens_for_raises = None

    def module(self):
        m = types.ModuleType("sqlalchemy")
        m.text = FakeText
        outer = self

        def create_engine(url, **kw):
            outer.create_calls.append((url, kw))
            if outer.create_raises:
                raise outer.create_raises
            return outer.engine

        def listens_for(target, name):
            if outer.listens_for_raises:
                raise outer.listens_for_raises

            def deco(fn):
                outer.listeners.append((target, name, fn))
                return fn

            return deco

        m.create_engine = create_engine
        m.event = SimpleNamespace(listens_for=listens_for)
        return m


class DdlEngine:
    """Engine whose begin() context yields a connection that records / raises."""

    def __init__(self, raises=None, begin_raises=None):
        self.executed = []
        self.opened = 0
        self.raises = raises
        self.begin_raises = begin_raises

    def begin(self):
        if self.begin_raises:
            raise self.begin_raises
        self.opened += 1
        return _Ctx(self)


class _Ctx:
    def __init__(self, eng):
        self.eng = eng

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt):
        self.eng.executed.append(str(stmt))
        if self.eng.raises:
            raise self.eng.raises


class LogSpy:
    def __init__(self):
        self.rec = []

    def _mk(self, level):
        return lambda msg, *a, **k: self.rec.append((level, msg % a if a else msg))

    def __getattr__(self, name):
        if name in ("debug", "info", "warning", "error", "exception"):
            return self._mk(name)
        raise AttributeError(name)

    def levels(self):
        return [r[0] for r in self.rec]

    def text(self):
        return "\n".join(r[1] for r in self.rec)


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def sa(monkeypatch):
    fake = FakeSA()
    monkeypatch.setitem(sys.modules, "sqlalchemy", fake.module())
    return fake


@pytest.fixture
def m(sa):
    """A fresh copy of oracle_compat (fresh _ORACLE_LOB_CONFIGURED)."""
    spec = importlib.util.spec_from_file_location("oracle_compat_under_test", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ora(monkeypatch):
    """Install a fake `oracledb` with a `defaults` object."""
    mod = types.ModuleType("oracledb")
    mod.defaults = SimpleNamespace(fetch_lobs=True)
    monkeypatch.setitem(sys.modules, "oracledb", mod)
    return mod


# ── oracle_is_configured ──────────────────────────────────────────────────────

class TestOracleIsConfigured:
    @pytest.mark.parametrize("url", [
        "oracle://u:p@h/db", "oracle+oracledb://", "ORACLE+ORACLEDB://x", "Oracle://x",
    ])
    def test_oracle_scheme(self, m, url):
        assert m.oracle_is_configured(url) is True

    @pytest.mark.parametrize("url", [
        "", "postgresql://h/db", "postgres://h/db", "sqlite:///x.db",
        "https://oracle.example.com",   # "oracle" must be the URL PREFIX
        " oracle://x",                  # leading space is not stripped
    ])
    def test_non_oracle_url(self, m, url):
        assert m.oracle_is_configured(url) is False

    def test_default_arg_is_empty(self, m):
        assert m.oracle_is_configured() is False

    def test_none_url(self, m):
        assert m.oracle_is_configured(None) is False

    def test_dsn_env_alone_is_enough(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_DSN", "stockkydb_high")
        assert m.oracle_is_configured() is True
        assert m.oracle_is_configured("postgresql://h/db") is True

    def test_empty_dsn_env_does_not_count(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_DSN", "")
        assert m.oracle_is_configured("postgresql://h/db") is False

    def test_never_raises_on_non_string_url(self, m):
        assert m.oracle_is_configured(12345) is False

    def test_never_raises_when_environ_is_broken(self, m, monkeypatch):
        class BadEnv:
            def get(self, *a, **k):
                raise RuntimeError("environ exploded")

        monkeypatch.setattr(m, "os", SimpleNamespace(environ=BadEnv()))
        assert m.oracle_is_configured("postgresql://h/db") is False


# ── dialect_name / is_oracle_engine ───────────────────────────────────────────

class TestDialect:
    @pytest.mark.parametrize("name,expected", [
        ("oracle", "oracle"), ("PostgreSQL", "postgresql"), ("sqlite", "sqlite"),
        ("", ""), (None, ""),
    ])
    def test_dialect_name(self, m, name, expected):
        eng = SimpleNamespace(dialect=SimpleNamespace(name=name))
        assert m.dialect_name(eng) == expected

    def test_engine_without_dialect(self, m):
        assert m.dialect_name(object()) == ""
        assert m.dialect_name(None) == ""

    def test_is_oracle_engine(self, m):
        assert m.is_oracle_engine(SimpleNamespace(dialect=SimpleNamespace(name="oracle"))) is True
        assert m.is_oracle_engine(SimpleNamespace(dialect=SimpleNamespace(name="ORACLE"))) is True
        assert m.is_oracle_engine(SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))) is False
        assert m.is_oracle_engine(object()) is False


# ── _configure_oracle_lobs ────────────────────────────────────────────────────

class TestConfigureOracleLobs:
    def test_sets_fetch_lobs_false_and_marks_done(self, m, ora):
        assert ora.defaults.fetch_lobs is True
        m._configure_oracle_lobs()
        assert ora.defaults.fetch_lobs is False
        assert m._ORACLE_LOB_CONFIGURED is True

    def test_only_runs_once(self, m, ora):
        m._configure_oracle_lobs()
        ora.defaults.fetch_lobs = True   # someone flips it back
        m._configure_oracle_lobs()
        assert ora.defaults.fetch_lobs is True  # second call is a no-op

    def test_old_oracledb_without_defaults_is_a_noop(self, m, ora):
        del ora.defaults                      # no .defaults at all
        m._configure_oracle_lobs()            # AttributeError swallowed
        assert m._ORACLE_LOB_CONFIGURED is True

    def test_failing_setattr_is_swallowed(self, m, monkeypatch):
        class Frozen:
            @property
            def fetch_lobs(self):
                return True

            @fetch_lobs.setter
            def fetch_lobs(self, v):
                raise RuntimeError("read-only")

        mod = types.ModuleType("oracledb")
        mod.defaults = Frozen()
        monkeypatch.setitem(sys.modules, "oracledb", mod)
        m._configure_oracle_lobs()
        assert m._ORACLE_LOB_CONFIGURED is True

    def test_oracledb_not_installed_leaves_flag_unset(self, m, monkeypatch):
        monkeypatch.setitem(sys.modules, "oracledb", None)   # import -> ImportError
        m._configure_oracle_lobs()
        assert m._ORACLE_LOB_CONFIGURED is False

    def test_retries_after_missing_driver_is_installed(self, m, monkeypatch):
        monkeypatch.setitem(sys.modules, "oracledb", None)
        m._configure_oracle_lobs()
        assert m._ORACLE_LOB_CONFIGURED is False
        mod = types.ModuleType("oracledb")
        mod.defaults = SimpleNamespace(fetch_lobs=True)
        monkeypatch.setitem(sys.modules, "oracledb", mod)
        m._configure_oracle_lobs()
        assert mod.defaults.fetch_lobs is False and m._ORACLE_LOB_CONFIGURED is True


# ── oracle_engine_kwargs ──────────────────────────────────────────────────────

class TestOracleEngineKwargs:
    def test_discrete_defaults(self, m):
        kw = m.oracle_engine_kwargs(False)
        assert kw == {
            "echo": False, "pool_pre_ping": True,
            "pool_recycle": 300, "pool_size": 3, "max_overflow": 2, "pool_timeout": 30,
            "connect_args": {"user": "ADMIN", "dsn": ""},
        }

    def test_discrete_full_credentials_and_wallet(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_USER", "STOCKKY")
        monkeypatch.setenv("ORACLE_PASSWORD", "pw1")
        monkeypatch.setenv("ORACLE_DSN", "stockkydb_high")
        monkeypatch.setenv("ORACLE_WALLET_DIR", "/wallet")
        monkeypatch.setenv("ORACLE_WALLET_PASSWORD", "wpw")
        assert m.oracle_engine_kwargs(False)["connect_args"] == {
            "user": "STOCKKY", "password": "pw1", "dsn": "stockkydb_high",
            "config_dir": "/wallet", "wallet_location": "/wallet", "wallet_password": "wpw",
        }

    def test_password_precedence(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_ADMIN_PASSWORD", "admin-pw")
        assert m.oracle_engine_kwargs(False)["connect_args"]["password"] == "admin-pw"
        monkeypatch.setenv("ORACLE_PASSWORD", "main-pw")
        assert m.oracle_engine_kwargs(False)["connect_args"]["password"] == "main-pw"

    def test_empty_password_is_omitted(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_PASSWORD", "")
        assert "password" not in m.oracle_engine_kwargs(False)["connect_args"]

    def test_full_url_keeps_credentials_out_of_connect_args(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_USER", "X")
        monkeypatch.setenv("ORACLE_PASSWORD", "pw")
        monkeypatch.setenv("ORACLE_DSN", "dsn")
        assert m.oracle_engine_kwargs(True)["connect_args"] == {}

    def test_wallet_applies_to_url_form_too(self, m, monkeypatch):
        monkeypatch.setenv("ORACLE_WALLET_DIR", "/w")
        monkeypatch.setenv("ORACLE_WALLET_PASSWORD", "wp")
        assert m.oracle_engine_kwargs(True)["connect_args"] == {
            "config_dir": "/w", "wallet_location": "/w", "wallet_password": "wp",
        }

    def test_wallet_dir_beats_tns_admin(self, m, monkeypatch):
        monkeypatch.setenv("TNS_ADMIN", "/tns")
        assert m.oracle_engine_kwargs(True)["connect_args"]["config_dir"] == "/tns"
        monkeypatch.setenv("ORACLE_WALLET_DIR", "/wallet")
        assert m.oracle_engine_kwargs(True)["connect_args"]["config_dir"] == "/wallet"

    def test_no_wallet_no_wallet_keys(self, m):
        ca = m.oracle_engine_kwargs(True)["connect_args"]
        assert "config_dir" not in ca and "wallet_password" not in ca

    def test_pool_env_vars(self, m, monkeypatch):
        monkeypatch.setenv("DB_POOL_SIZE", "10")
        monkeypatch.setenv("DB_MAX_OVERFLOW", "4")
        monkeypatch.setenv("DB_POOL_RECYCLE", "120")
        monkeypatch.setenv("DB_POOL_TIMEOUT", "15")
        kw = m.oracle_engine_kwargs(True)
        assert (kw["pool_size"], kw["max_overflow"], kw["pool_recycle"], kw["pool_timeout"]) == (10, 4, 120, 15)

    def test_overrides_beat_env_and_accept_strings(self, m, monkeypatch):
        monkeypatch.setenv("DB_POOL_SIZE", "10")
        kw = m.oracle_engine_kwargs(True, db_pool_size="5", db_max_overflow=3,
                                    db_pool_recycle="99", db_pool_timeout="7")
        assert (kw["pool_size"], kw["max_overflow"], kw["pool_recycle"], kw["pool_timeout"]) == (5, 3, 99, 7)

    def test_override_zero_is_respected(self, m, monkeypatch):
        monkeypatch.setenv("DB_MAX_OVERFLOW", "9")
        assert m.oracle_engine_kwargs(True, db_max_overflow=0)["max_overflow"] == 0

    def test_partial_override_falls_back_to_env_then_default(self, m, monkeypatch):
        monkeypatch.setenv("DB_POOL_TIMEOUT", "11")
        kw = m.oracle_engine_kwargs(True, db_pool_size=8)
        assert kw["pool_size"] == 8 and kw["pool_timeout"] == 11 and kw["pool_recycle"] == 300

    def test_non_numeric_pool_value_raises(self, m):
        """Pinned: a bad number is NOT swallowed here (unlike ORACLE_CALL_TIMEOUT_MS)."""
        with pytest.raises(ValueError):
            m.oracle_engine_kwargs(True, db_pool_size="lots")

    def test_unknown_override_is_ignored(self, m):
        assert "bogus" not in m.oracle_engine_kwargs(True, bogus=1)


# ── build_oracle_engine ───────────────────────────────────────────────────────

class TestBuildOracleEngine:
    def test_full_url_used_as_is(self, m, sa, ora):
        eng, url = m.build_oracle_engine("oracle+oracledb://u:p@host:1522/svc")
        assert eng is sa.engine and url == "oracle+oracledb://u:p@host:1522/svc"
        called_url, kw = sa.create_calls[0]
        assert called_url == "oracle+oracledb://u:p@host:1522/svc"
        assert kw["connect_args"] == {}

    @pytest.mark.parametrize("url", ["", "oracle+oracledb://", "oracle://", "oracle+oracledb://   "])
    def test_sentinel_or_empty_uses_connect_args(self, m, sa, ora, monkeypatch, url):
        monkeypatch.setenv("ORACLE_DSN", "stockkydb_high")
        monkeypatch.setenv("ORACLE_PASSWORD", "pw")
        eng, out_url = m.build_oracle_engine(url)
        assert out_url == "oracle+oracledb://"
        called_url, kw = sa.create_calls[0]
        assert called_url == "oracle+oracledb://"
        assert kw["connect_args"]["dsn"] == "stockkydb_high" and kw["connect_args"]["password"] == "pw"

    def test_default_argument(self, m, sa, ora):
        assert m.build_oracle_engine()[1] == "oracle+oracledb://"

    def test_none_url(self, m, sa, ora):
        assert m.build_oracle_engine(None)[1] == "oracle+oracledb://"

    def test_non_oracle_url_is_replaced_by_sentinel(self, m, sa, ora):
        """Pinned: a postgres URL handed to the Oracle builder is discarded, not passed through."""
        _, url = m.build_oracle_engine("postgresql://u:p@h/db")
        assert url == "oracle+oracledb://"
        assert sa.create_calls[0][0] == "oracle+oracledb://"

    def test_pool_overrides_forwarded(self, m, sa, ora):
        m.build_oracle_engine("oracle+oracledb://x", db_pool_size="5", db_max_overflow="3",
                              db_pool_recycle="300", db_pool_timeout="10")
        kw = sa.create_calls[0][1]
        assert (kw["pool_size"], kw["max_overflow"], kw["pool_recycle"], kw["pool_timeout"]) == (5, 3, 300, 10)
        assert kw["echo"] is False and kw["pool_pre_ping"] is True

    def test_configures_lobs_first(self, m, sa, ora):
        m.build_oracle_engine("oracle+oracledb://x")
        assert ora.defaults.fetch_lobs is False

    def test_works_without_oracledb_installed(self, m, sa, monkeypatch):
        monkeypatch.setitem(sys.modules, "oracledb", None)
        eng, _ = m.build_oracle_engine("oracle+oracledb://x")
        assert eng is sa.engine and m._ORACLE_LOB_CONFIGURED is False

    def test_installs_call_timeout_listener_on_the_engine(self, m, sa, ora):
        m.build_oracle_engine("oracle+oracledb://x")
        assert len(sa.listeners) == 1
        target, name, _fn = sa.listeners[0]
        assert target is sa.engine and name == "connect"

    def test_logs_dsn_and_wallet(self, m, sa, ora, monkeypatch):
        spy = LogSpy()
        monkeypatch.setattr(m, "_log", spy)
        monkeypatch.setenv("ORACLE_DSN", "stockkydb_high")
        monkeypatch.setenv("TNS_ADMIN", "/tns")
        m.build_oracle_engine("")
        assert "dsn=stockkydb_high" in spy.text() and "wallet=/tns" in spy.text()

    def test_logs_placeholders_when_unset(self, m, sa, ora, monkeypatch):
        spy = LogSpy()
        monkeypatch.setattr(m, "_log", spy)
        m.build_oracle_engine("oracle+oracledb://x")
        assert "dsn=from-url" in spy.text() and "wallet=none" in spy.text()

    def test_create_engine_failure_propagates(self, m, sa, ora):
        """The caller (kv_cache._get_neon) owns the try/except; this must not swallow."""
        sa.create_raises = RuntimeError("bad url")
        with pytest.raises(RuntimeError, match="bad url"):
            m.build_oracle_engine("oracle+oracledb://x")


# ── _attach_call_timeout ──────────────────────────────────────────────────────

class FakeDbapi:
    def __init__(self):
        self.call_timeout = None


class TestAttachCallTimeout:
    def test_default_8000ms(self, m, sa):
        eng = object()
        m._attach_call_timeout(eng)
        (target, name, fn), = sa.listeners
        assert target is eng and name == "connect"
        conn = FakeDbapi()
        fn(conn, object())
        assert conn.call_timeout == 8000

    def test_env_override(self, m, sa, monkeypatch):
        monkeypatch.setenv("ORACLE_CALL_TIMEOUT_MS", "2500")
        m._attach_call_timeout(object())
        conn = FakeDbapi()
        sa.listeners[0][2](conn, None)
        assert conn.call_timeout == 2500

    def test_timeout_is_read_when_attached_not_per_connection(self, m, sa, monkeypatch):
        m._attach_call_timeout(object())
        monkeypatch.setenv("ORACLE_CALL_TIMEOUT_MS", "1")
        conn = FakeDbapi()
        sa.listeners[0][2](conn, None)
        assert conn.call_timeout == 8000

    def test_listener_never_raises_when_driver_rejects_timeout(self, m, sa, monkeypatch):
        spy = LogSpy()
        monkeypatch.setattr(m, "_log", spy)

        class Strict:
            @property
            def call_timeout(self):
                return None

            @call_timeout.setter
            def call_timeout(self, v):
                raise RuntimeError("driver says no")

        m._attach_call_timeout(object())
        sa.listeners[0][2](Strict(), None)   # must not raise -> connection stays usable
        assert "driver says no" in spy.text()

    @pytest.mark.parametrize("raw", ["8s", "abc", "1.5", "-5", "-1"])
    def test_bad_timeout_env_falls_back_to_default_with_warning(self, m, sa, monkeypatch, raw):
        """Fixed: a typo (or negative value) in ORACLE_CALL_TIMEOUT_MS used to leave every
        Oracle round trip UNBOUNDED. It now keeps the 8000 ms default and warns."""
        spy = LogSpy()
        monkeypatch.setattr(m, "_log", spy)
        monkeypatch.setenv("ORACLE_CALL_TIMEOUT_MS", raw)
        m._attach_call_timeout(object())
        (target, name, fn), = sa.listeners
        conn = FakeDbapi()
        fn(conn, object())
        assert conn.call_timeout == 8000
        assert "warning" in spy.levels() and "ORACLE_CALL_TIMEOUT_MS" in spy.text()

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_blank_timeout_env_uses_default_silently(self, m, sa, monkeypatch, raw):
        spy = LogSpy()
        monkeypatch.setattr(m, "_log", spy)
        monkeypatch.setenv("ORACLE_CALL_TIMEOUT_MS", raw)
        m._attach_call_timeout(object())
        conn = FakeDbapi()
        sa.listeners[0][2](conn, None)
        assert conn.call_timeout == 8000 and "warning" not in spy.levels()

    def test_explicit_zero_is_still_honoured_as_no_timeout(self, m, sa, monkeypatch):
        monkeypatch.setenv("ORACLE_CALL_TIMEOUT_MS", "0")
        m._attach_call_timeout(object())
        conn = FakeDbapi()
        sa.listeners[0][2](conn, None)
        assert conn.call_timeout == 0

    def test_whitespace_around_a_valid_number_is_accepted(self, m, sa, monkeypatch):
        monkeypatch.setenv("ORACLE_CALL_TIMEOUT_MS", " 3000 ")
        m._attach_call_timeout(object())
        conn = FakeDbapi()
        sa.listeners[0][2](conn, None)
        assert conn.call_timeout == 3000

    def test_listener_registration_failure_is_non_fatal(self, m, sa, monkeypatch):
        spy = LogSpy()
        monkeypatch.setattr(m, "_log", spy)
        sa.listens_for_raises = RuntimeError("no such event")
        m._attach_call_timeout(object())     # must not raise
        assert "no such event" in spy.text()

    def test_sqlalchemy_event_import_failure_is_non_fatal(self, m, monkeypatch):
        bare = types.ModuleType("sqlalchemy")      # no `event` attribute
        monkeypatch.setitem(sys.modules, "sqlalchemy", bare)
        m._attach_call_timeout(object())           # must not raise


# ── SQL builders: exact strings ───────────────────────────────────────────────

class TestNowFunc:
    def test_values(self, m):
        assert m.now_func("oracle") == "SYSTIMESTAMP"
        assert m.now_func("postgresql") == "NOW()"
        assert m.now_func("sqlite") == "NOW()"
        assert m.now_func("") == "NOW()"


class TestCreateTableSql:
    def test_oracle_with_expires(self, m):
        assert m.create_table_sql("oracle", "stockky_kv", True) == (
            "CREATE TABLE stockky_kv (k VARCHAR2(1000) PRIMARY KEY, v CLOB, "
            "expires_at TIMESTAMP, updated_at TIMESTAMP DEFAULT SYSTIMESTAMP)"
        )

    def test_oracle_without_expires(self, m):
        assert m.create_table_sql("oracle", "stockky_watchlist", False) == (
            "CREATE TABLE stockky_watchlist (k VARCHAR2(1000) PRIMARY KEY, v CLOB, "
            "updated_at TIMESTAMP DEFAULT SYSTIMESTAMP)"
        )

    def test_postgres_with_expires(self, m):
        assert m.create_table_sql("postgresql", "stockky_kv", True) == (
            "CREATE TABLE IF NOT EXISTS stockky_kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, "
            "expires_at TIMESTAMPTZ NULL, updated_at TIMESTAMPTZ DEFAULT NOW())"
        )

    def test_postgres_without_expires(self, m):
        assert m.create_table_sql("postgresql", "stockky_notification", False) == (
            "CREATE TABLE IF NOT EXISTS stockky_notification (k TEXT PRIMARY KEY, "
            "v TEXT NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW())"
        )

    def test_unknown_dialect_gets_postgres_flavour(self, m):
        assert m.create_table_sql("sqlite", "t", False) == m.create_table_sql("postgresql", "t", False)

    def test_oracle_clob_is_nullable_on_purpose(self, m):
        assert "NOT NULL" not in m.create_table_sql("oracle", "t", True)


class TestCreateIndexSql:
    def test_oracle_has_no_if_not_exists(self, m):
        assert m.create_index_sql("oracle", "idx_a", "stockky_kv", "k") == "CREATE INDEX idx_a ON stockky_kv (k)"

    def test_postgres(self, m):
        assert m.create_index_sql("postgresql", "idx_a", "stockky_kv", "expires_at") == \
            "CREATE INDEX IF NOT EXISTS idx_a ON stockky_kv (expires_at)"


class TestUpsertSql:
    def test_oracle_with_expires(self, m):
        assert m.upsert_sql("oracle", "stockky_kv", True) == (
            "MERGE INTO stockky_kv d USING (SELECT :k AS k, :v AS v, :e AS e FROM dual) s "
            "ON (d.k = s.k) "
            "WHEN MATCHED THEN UPDATE SET d.v = s.v, d.expires_at = s.e, d.updated_at = SYSTIMESTAMP "
            "WHEN NOT MATCHED THEN INSERT (k, v, expires_at, updated_at) "
            "VALUES (s.k, s.v, s.e, SYSTIMESTAMP)"
        )

    def test_oracle_without_expires(self, m):
        assert m.upsert_sql("oracle", "stockky_watchlist", False) == (
            "MERGE INTO stockky_watchlist d USING (SELECT :k AS k, :v AS v FROM dual) s "
            "ON (d.k = s.k) "
            "WHEN MATCHED THEN UPDATE SET d.v = s.v, d.updated_at = SYSTIMESTAMP "
            "WHEN NOT MATCHED THEN INSERT (k, v, updated_at) VALUES (s.k, s.v, SYSTIMESTAMP)"
        )

    def test_postgres_with_expires(self, m):
        assert m.upsert_sql("postgresql", "stockky_kv", True) == (
            "INSERT INTO stockky_kv (k, v, expires_at, updated_at) VALUES (:k, :v, :e, NOW()) "
            "ON CONFLICT (k) DO UPDATE "
            "SET v = EXCLUDED.v, expires_at = EXCLUDED.expires_at, updated_at = NOW()"
        )

    def test_postgres_without_expires(self, m):
        assert m.upsert_sql("postgresql", "stockky_notification", False) == (
            "INSERT INTO stockky_notification (k, v, updated_at) VALUES (:k, :v, NOW()) "
            "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated_at = NOW()"
        )

    @pytest.mark.parametrize("dialect", ["oracle", "postgresql"])
    def test_bind_names_match_what_kv_cache_passes(self, m, dialect):
        """kv_cache binds {k, v, e} for the KV table and {k, v} for settings tables."""
        with_e = m.upsert_sql(dialect, "t", True)
        without_e = m.upsert_sql(dialect, "t", False)
        for bind in (":k", ":v", ":e"):
            assert bind in with_e
        assert ":k" in without_e and ":v" in without_e and ":e" not in without_e

    def test_unknown_dialect_gets_postgres_flavour(self, m):
        assert m.upsert_sql("sqlite", "t", True) == m.upsert_sql("postgresql", "t", True)


# ── exec_ddl_safe ─────────────────────────────────────────────────────────────

class TestExecDdlSafe:
    def test_success_runs_in_its_own_transaction(self, m, sa):
        eng = DdlEngine()
        m.exec_ddl_safe(eng, "CREATE TABLE t (k int)", "postgresql")
        assert eng.executed == ["CREATE TABLE t (k int)"] and eng.opened == 1

    def test_each_call_gets_a_fresh_transaction(self, m, sa):
        eng = DdlEngine()
        m.exec_ddl_safe(eng, "A", "oracle")
        m.exec_ddl_safe(eng, "B", "oracle")
        assert eng.opened == 2 and eng.executed == ["A", "B"]

    @pytest.mark.parametrize("code", ["ORA-00955", "ORA-01408", "ORA-00957", "ORA-02260", "ORA-02264"])
    def test_oracle_already_exists_codes_are_swallowed_silently(self, m, sa, monkeypatch, code):
        spy = LogSpy()
        monkeypatch.setattr(m, "_log", spy)
        eng = DdlEngine(raises=Exception(f"{code}: name is already used by an existing object"))
        m.exec_ddl_safe(eng, "CREATE TABLE t (k int)", "oracle")
        assert spy.rec == []                     # benign: not even a debug line

    def test_oracle_code_inside_longer_message(self, m, sa, monkeypatch):
        spy = LogSpy()
        monkeypatch.setattr(m, "_log", spy)
        eng = DdlEngine(raises=Exception("(oracledb.exceptions.DatabaseError) ORA-00955: x\n[SQL: ...]"))
        m.exec_ddl_safe(eng, "CREATE TABLE t (k int)", "oracle")
        assert spy.rec == []

    def test_oracle_codes_are_not_special_on_postgres(self, m, sa, monkeypatch):
        spy = LogSpy()
        monkeypatch.setattr(m, "_log", spy)
        eng = DdlEngine(raises=Exception("ORA-00955: something"))
        m.exec_ddl_safe(eng, "CREATE TABLE t (k int)", "postgresql")
        assert spy.levels() == ["warning"] and "ORA-00955" in spy.text()

    @pytest.mark.parametrize("dialect", ["postgresql", "oracle"])
    def test_already_exists_text_is_swallowed_silently_any_dialect(self, m, sa, monkeypatch, dialect):
        spy = LogSpy()
        monkeypatch.setattr(m, "_log", spy)
        eng = DdlEngine(raises=Exception('relation "stockky_kv" ALREADY EXISTS'))
        m.exec_ddl_safe(eng, "CREATE TABLE t (k int)", dialect)
        assert spy.rec == []

    def test_other_errors_are_swallowed_but_logged_at_warning(self, m, sa, monkeypatch):
        spy = LogSpy()
        monkeypatch.setattr(m, "_log", spy)
        eng = DdlEngine(raises=Exception("ORA-01031: insufficient privileges"))
        m.exec_ddl_safe(eng, "CREATE TABLE t (k int)", "oracle")   # must not raise
        assert spy.levels() == ["warning"]
        assert "oracle" in spy.text() and "ORA-01031" in spy.text()

    def test_logged_message_is_truncated_to_160_chars(self, m, sa, monkeypatch):
        spy = LogSpy()
        monkeypatch.setattr(m, "_log", spy)
        eng = DdlEngine(raises=Exception("z" * 500))
        m.exec_ddl_safe(eng, "X", "postgresql")
        assert spy.text().count("z") == 160

    def test_failure_to_open_a_transaction_is_swallowed_too(self, m, sa, monkeypatch):
        spy = LogSpy()
        monkeypatch.setattr(m, "_log", spy)
        eng = DdlEngine(begin_raises=Exception("connection refused"))
        m.exec_ddl_safe(eng, "X", "oracle")     # must not raise
        assert "connection refused" in spy.text()

    def test_returns_true_on_success_and_on_benign_already_exists(self, m, sa):
        assert m.exec_ddl_safe(DdlEngine(), "CREATE TABLE t (k int)", "oracle") is True
        eng = DdlEngine(raises=Exception("ORA-00955: name is already used by an existing object"))
        assert m.exec_ddl_safe(eng, "CREATE TABLE t (k int)", "oracle") is True

    def test_returns_false_on_a_real_failure_and_logs_the_statement(self, m, sa, monkeypatch):
        spy = LogSpy()
        monkeypatch.setattr(m, "_log", spy)
        eng = DdlEngine(raises=Exception("ORA-01031: insufficient privileges"))
        assert m.exec_ddl_safe(eng, "CREATE INDEX ix_a ON t (k)", "oracle") is False
        assert "CREATE INDEX ix_a ON t (k)" in spy.text()

    def test_returns_false_when_a_transaction_cannot_be_opened(self, m, sa):
        eng = DdlEngine(begin_raises=Exception("connection refused"))
        assert m.exec_ddl_safe(eng, "X", "oracle") is False

    def test_duplicate_column_name_text_is_benign_on_any_dialect(self, m, sa, monkeypatch):
        spy = LogSpy()
        monkeypatch.setattr(m, "_log", spy)
        eng = DdlEngine(raises=Exception("duplicate column name: v"))
        assert m.exec_ddl_safe(eng, "ALTER TABLE t ADD COLUMN v TEXT", "sqlite") is True
        assert spy.rec == []

    def test_one_failure_does_not_poison_the_next_statement(self, m, sa):
        bad = DdlEngine(raises=Exception("ORA-00955"))
        good = DdlEngine()
        m.exec_ddl_safe(bad, "A", "oracle")
        m.exec_ddl_safe(good, "B", "oracle")
        assert good.executed == ["B"]
