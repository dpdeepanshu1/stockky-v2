"""group 80: per-request Oracle work in the decision/training service.

/training-score is called twice per stock analysis. Each call built a TrainingScanner ->
ModelRegistry() -> get_engine() + create_all() + ensure_oracle_identity(), so the log showed
"Oracle Autonomous DB engine ready" plus ~8 identity-DDL lines on every request. Three causes:
  * ensure_oracle_identity() had no once-per-engine guard;
  * get_engine() stored the REWRITTEN url ("oracle+oracledb://") but compared the un-rewritten one,
    so on the Oracle VM (discrete ORACLE_* vars) the singleton never hit and a new pool was built;
  * app.py built a fresh TrainingScanner (model load + unpickle) per request.

Run: cd services/decision-prediction-service/training && python3 -m pytest tests/test_identity_once_and_engine_cache.py -q
"""
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import models  # noqa: E402


class _Result:
    def __init__(self, v):
        self._v = v

    def scalar(self):
        return self._v


class _Conn:
    def __init__(self, eng):
        self.eng = eng

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.eng.sql.append(sql)
        if self.eng.fail_sql and self.eng.fail_sql in sql:
            raise RuntimeError("ORA-boom")
        if "user_tables" in sql:
            return _Result(1)
        if "user_tab_identity_cols" in sql:
            return _Result(1)          # already an identity -> nothing to repair
        return _Result(0)


class FakeOracleEngine:
    def __init__(self, fail_sql=None):
        self.dialect = types.SimpleNamespace(name="oracle")
        self.sql = []
        self.fail_sql = fail_sql

    def connect(self):
        return _Conn(self)

    begin = connect


@pytest.fixture(autouse=True)
def _reset_state():
    models._IDENTITY_STATE.clear()
    models._SCHEMA_ENGINES.clear()
    yield
    models._IDENTITY_STATE.clear()
    models._SCHEMA_ENGINES.clear()


def test_identity_repair_runs_once_per_engine():
    eng = FakeOracleEngine()
    models.ensure_oracle_identity(eng)
    first = len(eng.sql)
    assert first >= 2 * len(models._PK_TABLES)         # one user_tables + one identity lookup per table
    for _ in range(5):
        models.ensure_oracle_identity(eng)
    assert len(eng.sql) == first                       # no further round trips


def test_each_engine_gets_its_own_pass():
    a, b = FakeOracleEngine(), FakeOracleEngine()
    models.ensure_oracle_identity(a)
    models.ensure_oracle_identity(b)
    assert a.sql and b.sql


def test_failed_pass_is_retried_only_after_the_cooldown(monkeypatch):
    eng = FakeOracleEngine(fail_sql="user_tables")
    clock = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    models.ensure_oracle_identity(eng)
    n = len(eng.sql)
    assert n > 0
    models.ensure_oracle_identity(eng)                 # immediately again: still cooling down
    assert len(eng.sql) == n
    clock[0] += models._IDENTITY_RETRY_SEC + 1
    eng.fail_sql = None                                # the transient error is gone
    models.ensure_oracle_identity(eng)
    assert len(eng.sql) > n                            # retried
    after = len(eng.sql)
    clock[0] += models._IDENTITY_RETRY_SEC + 1
    models.ensure_oracle_identity(eng)
    assert len(eng.sql) == after                       # clean pass is final


def test_non_oracle_engine_is_a_noop_and_records_nothing():
    eng = types.SimpleNamespace(dialect=types.SimpleNamespace(name="sqlite"))
    models.ensure_oracle_identity(eng)
    assert models._IDENTITY_STATE == {}


def test_broken_engine_object_does_not_raise():
    models.ensure_oracle_identity(object())            # no .dialect -> silently ignored


def test_create_all_once_per_engine(monkeypatch):
    calls = []
    monkeypatch.setattr(models.Base.metadata, "create_all", lambda e: calls.append(e))
    eng = object()
    models._create_all_once(eng)
    models._create_all_once(eng)
    assert calls == [eng]


def test_create_all_failure_is_not_remembered(monkeypatch):
    state = {"n": 0}

    def flaky(e):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("db down")

    monkeypatch.setattr(models.Base.metadata, "create_all", flaky)
    eng = object()
    with pytest.raises(RuntimeError):
        models._create_all_once(eng)
    models._create_all_once(eng)                       # retried
    models._create_all_once(eng)                       # now remembered
    assert state["n"] == 2


def test_get_engine_is_a_singleton_for_the_oracle_discrete_var_case(monkeypatch):
    """The regression: the cache key used to be the rewritten url, so this never matched."""
    monkeypatch.setenv("ORACLE_DSN", "stockkydb_high")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.delenv("TRAINING_DATABASE_URL", raising=False)
    monkeypatch.setattr(models, "_ENGINE", None)
    monkeypatch.setattr(models, "_ENGINE_URL", None)
    built = []
    monkeypatch.setattr(models, "_oracle_is_configured", lambda url: True)
    monkeypatch.setattr(models, "_oracle_engine_kwargs", lambda full: {})
    monkeypatch.setattr(models, "create_engine", lambda url, **kw: built.append(url) or object())
    e1 = models.get_engine()
    e2 = models.get_engine()
    e3 = models.get_engine()
    assert e1 is e2 is e3
    assert built == ["oracle+oracledb://"]             # one engine, built from the rewritten url
