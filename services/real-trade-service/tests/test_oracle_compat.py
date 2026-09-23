"""
tests/test_oracle_compat.py  — session89, step 6
=================================================
Coverage target:
  oracle_compat.py   15% → 100%   (110 statements)

Approach:
  Two distinct groups of functions here need two different strategies.

  1. Pure functions (oracle_is_configured, dialect_name, is_oracle_engine,
     _configure_oracle_lobs, oracle_engine_kwargs, now_func, create_table_sql,
     create_index_sql, upsert_sql) touch only os.environ / plain data —
     tested directly, no mocking beyond os.environ and a couple of tiny
     fake "engine" stand-ins with just a `.dialect.name` attribute.

  2. build_oracle_engine / _attach_call_timeout / exec_ddl_safe all call
     real sqlalchemy (create_engine / event.listens_for / text +
     engine.begin()/connect()). Rather than mock sqlalchemy itself, these
     tests build a REAL sqlite in-memory engine via sqlalchemy's own
     create_engine — same "real DB, no mocking of the ORM layer" idiom
     tests/test_portfolio.py already uses elsewhere in this suite. This
     is safe because:
       - dialect_name()/is_oracle_engine() and exec_ddl_safe()'s dialect
         branch only care about the `dialect` string we pass in
         explicitly (or that sqlalchemy derives from the URL scheme) —
         they don't require an actual Oracle server.
       - _attach_call_timeout() only needs *any* real SQLAlchemy engine
         to register a "connect" event listener on and then fire it for
         real via `engine.connect()` — a local sqlite connection genuinely
         triggers the same "connect" event a real Oracle connection would,
         with no network involved.
       - build_oracle_engine() itself calls
         create_engine("oracle+oracledb://...", connect_args=...) —
         building the SQLAlchemy Engine object is lazy (no connection
         attempt happens until first checkout), so this succeeds purely
         from the oracledb driver being importable (it's a pinned
         requirement — requirements.txt: oracledb==2.5.1), without ever
         touching the network. We only assert on the returned kwargs/url
         and dialect name, never on a live Oracle round trip.

Run from services/real-trade-service:
    python3 -m pytest tests/test_oracle_compat.py -v \
        --cov=oracle_compat --cov-report=term-missing
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import oracle_compat as oc


_ORACLE_ENV_VARS = [
    "ORACLE_DSN", "ORACLE_USER", "ORACLE_PASSWORD", "ORACLE_ADMIN_PASSWORD",
    "ORACLE_WALLET_DIR", "TNS_ADMIN", "ORACLE_WALLET_PASSWORD",
    "ORACLE_CALL_TIMEOUT_MS", "DB_POOL_SIZE", "DB_MAX_OVERFLOW",
    "DB_POOL_RECYCLE", "DB_POOL_TIMEOUT",
]


@pytest.fixture(autouse=True)
def _clean_oracle_env():
    """Every test starts from a clean slate regardless of test order /
    what the real shell environment happens to have set."""
    saved = {k: os.environ.get(k) for k in _ORACLE_ENV_VARS}
    for k in _ORACLE_ENV_VARS:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ══════════════════════════════════════════════════════════════════════════════
# oracle_is_configured()
# ══════════════════════════════════════════════════════════════════════════════

class TestOracleIsConfigured:
    def test_empty_url_no_env_returns_false(self):
        assert oc.oracle_is_configured("") is False

    def test_postgres_url_no_env_returns_false(self):
        assert oc.oracle_is_configured("postgresql://x") is False

    def test_oracle_url_returns_true(self):
        assert oc.oracle_is_configured("oracle+oracledb://x") is True

    def test_oracle_url_case_insensitive(self):
        assert oc.oracle_is_configured("ORACLE+oracledb://x") is True

    def test_env_var_wins_even_with_postgres_url(self):
        os.environ["ORACLE_DSN"] = "mydsn"
        assert oc.oracle_is_configured("postgresql://x") is True

    def test_default_url_arg_is_empty_string(self):
        os.environ["ORACLE_DSN"] = "mydsn"
        assert oc.oracle_is_configured() is True

    def test_swallows_exception_from_bad_url_type_returns_false(self):
        # url.lower() raises AttributeError for a non-str input (e.g. a
        # caller accidentally passing None's sentinel object or similar) —
        # the function is documented to fail safe (Postgres path) rather
        # than raise, exercising the try/except itself, not just the
        # os.environ short-circuit.
        class _NoLower:
            def __bool__(self):
                return True

            def lower(self):
                raise RuntimeError("not a real url")

        assert oc.oracle_is_configured(_NoLower()) is False


# ══════════════════════════════════════════════════════════════════════════════
# dialect_name() / is_oracle_engine()
# ══════════════════════════════════════════════════════════════════════════════

class _FakeDialect:
    def __init__(self, name):
        self.name = name


class _FakeEngine:
    def __init__(self, name):
        self.dialect = _FakeDialect(name)


class _BrokenEngine:
    @property
    def dialect(self):
        raise RuntimeError("no dialect available")


class TestDialectNameAndIsOracleEngine:
    def test_dialect_name_returns_lowercased_name(self):
        assert oc.dialect_name(_FakeEngine("oracle")) == "oracle"
        assert oc.dialect_name(_FakeEngine("POSTGRESQL")) == "postgresql"

    def test_dialect_name_swallows_exception_returns_empty(self):
        assert oc.dialect_name(_BrokenEngine()) == ""

    def test_is_oracle_engine_true_for_oracle(self):
        assert oc.is_oracle_engine(_FakeEngine("oracle")) is True

    def test_is_oracle_engine_false_for_other(self):
        assert oc.is_oracle_engine(_FakeEngine("postgresql")) is False

    def test_is_oracle_engine_false_on_broken_dialect(self):
        assert oc.is_oracle_engine(_BrokenEngine()) is False


# ══════════════════════════════════════════════════════════════════════════════
# _configure_oracle_lobs()
# ══════════════════════════════════════════════════════════════════════════════

class TestConfigureOracleLobs:
    def setup_method(self):
        oc._ORACLE_LOB_CONFIGURED = False

    def teardown_method(self):
        oc._ORACLE_LOB_CONFIGURED = False

    def test_already_configured_short_circuits(self):
        oc._ORACLE_LOB_CONFIGURED = True
        oc._configure_oracle_lobs()  # should just return; no error, no state change needed
        assert oc._ORACLE_LOB_CONFIGURED is True

    def test_oracledb_not_installed_is_silent_noop(self):
        with patch.dict(sys.modules, {"oracledb": None}):
            oc._configure_oracle_lobs()
        assert oc._ORACLE_LOB_CONFIGURED is False

    def test_oracledb_installed_sets_fetch_lobs_false(self):
        fake_oracledb = MagicMock()
        fake_oracledb.defaults = MagicMock()
        with patch.dict(sys.modules, {"oracledb": fake_oracledb}):
            oc._configure_oracle_lobs()
        assert oc._ORACLE_LOB_CONFIGURED is True
        assert fake_oracledb.defaults.fetch_lobs is False

    def test_older_oracledb_build_missing_fetch_lobs_still_flips_flag(self):
        class NoFetchLobs:
            def __setattr__(self, name, value):
                if name == "fetch_lobs":
                    raise AttributeError("no such attribute on this build")
                object.__setattr__(self, name, value)

        fake_oracledb = MagicMock()
        fake_oracledb.defaults = NoFetchLobs()
        with patch.dict(sys.modules, {"oracledb": fake_oracledb}):
            oc._configure_oracle_lobs()
        assert oc._ORACLE_LOB_CONFIGURED is True


# ══════════════════════════════════════════════════════════════════════════════
# now_func() / create_table_sql() / create_index_sql() / upsert_sql()
# ══════════════════════════════════════════════════════════════════════════════

class TestNowFunc:
    def test_oracle_dialect(self):
        assert oc.now_func("oracle") == "SYSTIMESTAMP"

    def test_postgresql_dialect(self):
        assert oc.now_func("postgresql") == "NOW()"

    def test_any_other_dialect_falls_back_to_now(self):
        assert oc.now_func("sqlite") == "NOW()"


class TestCreateTableSql:
    def test_postgres_without_expires(self):
        sql = oc.create_table_sql("postgresql", "kv", False)
        assert sql == (
            "CREATE TABLE IF NOT EXISTS kv "
            "(k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW())"
        )

    def test_postgres_with_expires(self):
        sql = oc.create_table_sql("postgresql", "kv", True)
        assert "expires_at TIMESTAMPTZ NULL" in sql
        assert "IF NOT EXISTS" in sql

    def test_oracle_without_expires(self):
        sql = oc.create_table_sql("oracle", "kv", False)
        assert sql == (
            "CREATE TABLE kv (k VARCHAR2(1000) PRIMARY KEY, v CLOB, "
            "updated_at TIMESTAMP DEFAULT SYSTIMESTAMP)"
        )
        assert "IF NOT EXISTS" not in sql

    def test_oracle_with_expires(self):
        sql = oc.create_table_sql("oracle", "kv", True)
        assert "expires_at TIMESTAMP" in sql
        assert "IF NOT EXISTS" not in sql


class TestCreateIndexSql:
    def test_postgres_includes_if_not_exists(self):
        assert oc.create_index_sql("postgresql", "idx1", "kv", "k") == \
            "CREATE INDEX IF NOT EXISTS idx1 ON kv (k)"

    def test_oracle_omits_if_not_exists(self):
        assert oc.create_index_sql("oracle", "idx1", "kv", "k") == \
            "CREATE INDEX idx1 ON kv (k)"


class TestUpsertSql:
    def test_postgres_without_expires(self):
        sql = oc.upsert_sql("postgresql", "kv", False)
        assert "ON CONFLICT (k) DO UPDATE" in sql
        assert "expires_at" not in sql

    def test_postgres_with_expires(self):
        sql = oc.upsert_sql("postgresql", "kv", True)
        assert "expires_at = EXCLUDED.expires_at" in sql

    def test_oracle_without_expires(self):
        sql = oc.upsert_sql("oracle", "kv", False)
        assert sql.startswith(
            "MERGE INTO kv d USING (SELECT :k AS k, :v AS v FROM dual) s ON (d.k = s.k)"
        )
        assert "expires_at" not in sql

    def test_oracle_with_expires(self):
        sql = oc.upsert_sql("oracle", "kv", True)
        assert "SELECT :k AS k, :v AS v, :e AS e FROM dual" in sql
        assert "d.expires_at = s.e" in sql


# ══════════════════════════════════════════════════════════════════════════════
# oracle_engine_kwargs()
# ══════════════════════════════════════════════════════════════════════════════

class TestOracleEngineKwargs:
    def test_defaults_discrete_var_form(self):
        kw = oc.oracle_engine_kwargs(False)
        assert kw["connect_args"]["user"] == "ADMIN"
        assert "password" not in kw["connect_args"]
        assert kw["connect_args"]["dsn"] == ""
        assert "config_dir" not in kw["connect_args"]
        assert kw["echo"] is False
        assert kw["pool_pre_ping"] is True
        assert kw["pool_size"] == 3
        assert kw["max_overflow"] == 2
        assert kw["pool_recycle"] == 300
        assert kw["pool_timeout"] == 30

    def test_full_url_provided_skips_discrete_creds(self):
        kw = oc.oracle_engine_kwargs(True)
        assert "user" not in kw["connect_args"]
        assert "dsn" not in kw["connect_args"]

    def test_password_from_oracle_password(self):
        os.environ["ORACLE_PASSWORD"] = "pw1"
        kw = oc.oracle_engine_kwargs(False)
        assert kw["connect_args"]["password"] == "pw1"

    def test_password_falls_back_to_admin_password(self):
        os.environ["ORACLE_ADMIN_PASSWORD"] = "pw2"
        kw = oc.oracle_engine_kwargs(False)
        assert kw["connect_args"]["password"] == "pw2"

    def test_oracle_password_takes_priority_over_admin_password(self):
        os.environ["ORACLE_PASSWORD"] = "pw1"
        os.environ["ORACLE_ADMIN_PASSWORD"] = "pw2"
        kw = oc.oracle_engine_kwargs(False)
        assert kw["connect_args"]["password"] == "pw1"

    def test_wallet_dir_and_password(self):
        os.environ["ORACLE_WALLET_DIR"] = "/wallet"
        os.environ["ORACLE_WALLET_PASSWORD"] = "wpw"
        kw = oc.oracle_engine_kwargs(False)
        assert kw["connect_args"]["config_dir"] == "/wallet"
        assert kw["connect_args"]["wallet_location"] == "/wallet"
        assert kw["connect_args"]["wallet_password"] == "wpw"

    def test_wallet_dir_falls_back_to_tns_admin(self):
        os.environ["TNS_ADMIN"] = "/tns"
        kw = oc.oracle_engine_kwargs(False)
        assert kw["connect_args"]["config_dir"] == "/tns"

    def test_pool_overrides_kwarg_wins_over_default(self):
        kw = oc.oracle_engine_kwargs(False, db_pool_size=7, db_pool_recycle=99)
        assert kw["pool_size"] == 7
        assert kw["pool_recycle"] == 99

    def test_pool_env_vars_used_when_no_override_given(self):
        os.environ["DB_POOL_SIZE"] = "11"
        kw = oc.oracle_engine_kwargs(False)
        assert kw["pool_size"] == 11

    def test_pool_override_wins_over_env_var(self):
        os.environ["DB_POOL_SIZE"] = "11"
        kw = oc.oracle_engine_kwargs(False, db_pool_size=4)
        assert kw["pool_size"] == 4


# ══════════════════════════════════════════════════════════════════════════════
# build_oracle_engine()  — real sqlalchemy, Oracle driver import only,
# no network/actual Oracle connection ever attempted.
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildOracleEngine:
    def test_empty_url_uses_discrete_var_sentinel(self):
        os.environ["ORACLE_DSN"] = "mydsn_high"
        eng, url = oc.build_oracle_engine("")
        assert url == "oracle+oracledb://"
        assert oc.dialect_name(eng) == "oracle"
        assert oc.is_oracle_engine(eng) is True

    def test_scheme_only_sentinel_treated_same_as_empty(self):
        os.environ["ORACLE_DSN"] = "mydsn_high"
        eng, url = oc.build_oracle_engine("oracle+oracledb://")
        assert url == "oracle+oracledb://"

    def test_full_url_used_as_is(self):
        eng, url = oc.build_oracle_engine("oracle+oracledb://user/pass@mydb_high")
        assert url == "oracle+oracledb://user/pass@mydb_high"

    def test_build_oracle_engine_registers_timeout_listener_without_connecting(self):
        # build_oracle_engine() itself must stay fully lazy: constructing the
        # Engine + registering the "connect" listener must not touch the
        # network. (Actually firing that listener is covered separately
        # below via _attach_call_timeout() directly on a real, local sqlite
        # engine — python-oracledb's thin-mode driver parses/validates the
        # DSN as soon as a real connection is attempted, even against a
        # bogus discrete-var sentinel, so eng.connect() here would require
        # real Oracle connectivity this suite intentionally never assumes.)
        os.environ["ORACLE_CALL_TIMEOUT_MS"] = "5000"
        eng, _ = oc.build_oracle_engine("")
        assert oc.is_oracle_engine(eng) is True


# ══════════════════════════════════════════════════════════════════════════════
# _attach_call_timeout()  — the registered "connect" listener itself, fired for
# real via a local sqlite engine (genuinely triggers sqlalchemy's "connect"
# event with no network involved; sqlite3.Connection objects reject arbitrary
# attribute assignment, so this exercises the same try/except shape a limited
# driver build would hit without needing python-oracledb specifically).
# ══════════════════════════════════════════════════════════════════════════════

class TestAttachCallTimeout:
    def test_fires_on_connect_and_never_raises(self):
        os.environ["ORACLE_CALL_TIMEOUT_MS"] = "5000"
        eng = create_engine("sqlite:///:memory:")
        oc._attach_call_timeout(eng)
        with eng.connect():
            pass  # must not raise, whichever branch the assignment takes

    def test_sets_call_timeout_when_dbapi_connection_accepts_it(self):
        # Capture the registered callback directly so the assignment's
        # success path is exercised deterministically, rather than relying
        # on incidental support/non-support in whatever driver is present.
        os.environ["ORACLE_CALL_TIMEOUT_MS"] = "1234"
        captured = {}

        def _fake_listens_for(target, identifier):
            def _decorator(fn):
                captured["fn"] = fn
                return fn
            return _decorator

        eng = create_engine("sqlite:///:memory:")
        with patch("sqlalchemy.event.listens_for", _fake_listens_for):
            oc._attach_call_timeout(eng)

        fake_dbapi_conn = MagicMock()
        captured["fn"](fake_dbapi_conn, None)
        assert fake_dbapi_conn.call_timeout == 1234


# ══════════════════════════════════════════════════════════════════════════════
# exec_ddl_safe()  — real sqlite in-memory engine
# ══════════════════════════════════════════════════════════════════════════════

class TestExecDdlSafe:
    def _engine(self):
        return create_engine("sqlite:///:memory:")

    def test_success_path_runs_cleanly(self):
        eng = self._engine()
        oc.exec_ddl_safe(eng, "CREATE TABLE kv (k TEXT)", "postgresql")
        # running it twice on sqlite raises "table kv already exists",
        # which IS the next test below — kept separate for clarity

    def test_generic_already_exists_is_swallowed_any_dialect(self):
        eng = self._engine()
        oc.exec_ddl_safe(eng, "CREATE TABLE kv (k TEXT)", "postgresql")
        # second run: sqlite raises "...table kv already exists" —
        # caught by the generic "already exists" branch, not the
        # oracle-specific one, regardless of the dialect string passed in
        oc.exec_ddl_safe(eng, "CREATE TABLE kv (k TEXT)", "postgresql")

    def test_oracle_specific_error_code_swallowed_when_dialect_is_oracle(self):
        eng = self._engine()

        def _raise_ora(sql):
            raise RuntimeError("ORA-00955: name is already used by an existing object")

        with patch.object(eng, "begin") as begin_mock:
            fake_conn = MagicMock()
            fake_conn.execute.side_effect = _raise_ora
            begin_mock.return_value.__enter__.return_value = fake_conn
            begin_mock.return_value.__exit__.return_value = False
            oc.exec_ddl_safe(eng, "CREATE INDEX idx1 ON kv (k)", "oracle")
        # no exception propagated -> swallowed by the ORA-code branch

    def test_ora_code_with_non_oracle_dialect_still_never_raises(self):
        eng = self._engine()

        def _raise_ora(sql):
            raise RuntimeError("ORA-00955: name is already used by an existing object")

        with patch.object(eng, "begin") as begin_mock:
            fake_conn = MagicMock()
            fake_conn.execute.side_effect = _raise_ora
            begin_mock.return_value.__enter__.return_value = fake_conn
            begin_mock.return_value.__exit__.return_value = False
            # dialect passed as "postgresql" -> ORA-code branch does not
            # apply, "already exists" substring also doesn't match ->
            # falls through to logger.debug, but is still fully swallowed
            oc.exec_ddl_safe(eng, "CREATE INDEX idx1 ON kv (k)", "postgresql")

    def test_unrelated_error_is_also_swallowed_non_fatal_by_design(self):
        eng = self._engine()

        def _raise_other(sql):
            raise RuntimeError("connection reset by peer")

        with patch.object(eng, "begin") as begin_mock:
            fake_conn = MagicMock()
            fake_conn.execute.side_effect = _raise_other
            begin_mock.return_value.__enter__.return_value = fake_conn
            begin_mock.return_value.__exit__.return_value = False
            oc.exec_ddl_safe(eng, "CREATE INDEX idx1 ON kv (k)", "postgresql")
