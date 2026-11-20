"""round 662 (D8): ensure_hotpicks_schema / ensure_surprise_schema build their engine and run the DDL once per process, not once per call.
A failed or skipped DDL run is never remembered; force=True re-runs."""
from __future__ import annotations

import importlib.util
import os
import sys
import types

import pytest

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _DIR)


class _Conn:
    def __init__(self, log): self.log = log
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql): self.log.append(str(sql))


class _Engine:
    def __init__(self, log): self.log = log; self.disposed = 0
    def begin(self): return _Conn(self.log)
    def dispose(self): self.disposed += 1


class _OC:
    def __init__(self, ok=True): self.ok = ok; self.engines = 0; self.ddl = 0
    def oracle_is_configured(self, url=""): return bool(os.environ.get("ORACLE_DSN"))
    def build_oracle_engine(self, url="", **kw): self.engines += 1; return _Engine([]), url
    def exec_ddl_safe(self, eng, sql, dialect): self.ddl += 1; return self.ok


def _load(monkeypatch, fname, oc, sa_log):
    sa = types.ModuleType("sqlalchemy")
    sa.text = lambda s: s
    def create_engine(url, **kw):
        sa_log["engines"] += 1
        return _Engine(sa_log["sql"])
    sa.create_engine = create_engine
    monkeypatch.setitem(sys.modules, "sqlalchemy", sa)
    monkeypatch.setitem(sys.modules, "oracle_compat", oc)
    spec = importlib.util.spec_from_file_location("r662_" + fname[:-3], os.path.join(_DIR, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CASES = [("hotpicks_schema.py", "ensure_hotpicks_schema"), ("surprise_schema.py", "ensure_surprise_schema")]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("DATABASE_URL", "CACHE_DATABASE_URL", "ORACLE_DSN", "ORACLE_USER"):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize("fname,fn", CASES)
def test_postgres_second_call_builds_no_engine(monkeypatch, fname, fn):
    monkeypatch.setenv("DATABASE_URL", "postgres://u@h/db")
    log = {"engines": 0, "sql": []}
    m = _load(monkeypatch, fname, _OC(), log)
    first = getattr(m, fn)()
    n_sql = len(log["sql"])
    assert first["ok"] is True and log["engines"] == 1 and n_sql > 0
    again = getattr(m, fn)()
    assert again["ok"] is True and again["cached"] is True
    assert log["engines"] == 1 and len(log["sql"]) == n_sql


@pytest.mark.parametrize("fname,fn", CASES)
def test_force_reruns(monkeypatch, fname, fn):
    monkeypatch.setenv("DATABASE_URL", "postgres://u@h/db")
    log = {"engines": 0, "sql": []}
    m = _load(monkeypatch, fname, _OC(), log)
    getattr(m, fn)()
    getattr(m, fn)(force=True)
    assert log["engines"] == 2


@pytest.mark.parametrize("fname,fn", CASES)
def test_oracle_ok_is_cached(monkeypatch, fname, fn):
    monkeypatch.setenv("ORACLE_DSN", "adb_high")
    oc = _OC(ok=True)
    m = _load(monkeypatch, fname, oc, {"engines": 0, "sql": []})
    getattr(m, fn)(); ddl = oc.ddl
    getattr(m, fn)()
    assert oc.engines == 1 and oc.ddl == ddl and ddl > 0


@pytest.mark.parametrize("fname,fn", CASES)
def test_oracle_failed_ddl_is_not_cached(monkeypatch, fname, fn):
    monkeypatch.setenv("ORACLE_DSN", "adb_high")
    oc = _OC(ok=False)
    m = _load(monkeypatch, fname, oc, {"engines": 0, "sql": []})
    getattr(m, fn)(); getattr(m, fn)()
    assert oc.engines == 2


@pytest.mark.parametrize("fname,fn", CASES)
def test_engine_error_is_not_cached(monkeypatch, fname, fn):
    monkeypatch.setenv("DATABASE_URL", "postgres://u@h/db")
    log = {"engines": 0, "sql": []}
    m = _load(monkeypatch, fname, _OC(), log)
    monkeypatch.setattr(m, "make_engine", lambda app_name="x": (_ for _ in ()).throw(RuntimeError("boom")))
    assert getattr(m, fn)()["ok"] is False
    assert not m._SCHEMA_DONE
