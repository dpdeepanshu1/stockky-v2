"""
group357 (14 Oct log: "QueuePool limit of size 1 overflow 1 reached" on the Dhan live_quotes write, plus a trade_credentials
read TimeoutError): the live_quotes writers get their own small engine instead of borrowing kv_cache's shared pool.

Run from services/market-data-service:
    python3 -m pytest tests/test_group357_live_quotes_own_pool.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import angelone_ws_feed as f
import kv_cache as kv


class _Pool:
    def __init__(self, size, overflow):
        self._size, self._max_overflow = size, overflow

    def size(self):
        return self._size


class _Eng:
    def __init__(self, tag, size=2, overflow=1):
        self.tag = tag
        self.pool = _Pool(size, overflow)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    f._LQ_STATE.update({"shared_id": None, "engine": None, "retry_at": 0.0, "kind": "shared", "error": None})
    for k in ("LIVE_QUOTES_DB_SEPARATE_ENGINE", "LIVE_QUOTES_DB_POOL_SIZE", "LIVE_QUOTES_DB_MAX_OVERFLOW",
              "LIVE_QUOTES_DB_POOL_TIMEOUT", "LIVE_QUOTES_DB_POOL_RECYCLE"):
        monkeypatch.delenv(k, raising=False)
    yield
    f._LQ_STATE.update({"shared_id": None, "engine": None, "retry_at": 0.0, "kind": "shared", "error": None})


def _patch(monkeypatch, shared, dialect="oracle"):
    monkeypatch.setattr(kv, "_get_neon", lambda: shared)
    monkeypatch.setattr(kv, "_dialect", lambda: dialect)


def test_writers_get_a_dedicated_engine_not_the_shared_one(monkeypatch):
    shared = _Eng("shared", 1, 1)
    _patch(monkeypatch, shared)
    built = []
    monkeypatch.setattr(f, "_build_live_quotes_engine", lambda d: built.append(d) or _Eng("own"))
    eng, dialect = f._get_live_quotes_engine()
    assert eng.tag == "own" and eng is not shared and dialect == "oracle"
    assert f.live_quotes_engine_status()["kind"] == "dedicated"


def test_dedicated_engine_is_built_once(monkeypatch):
    _patch(monkeypatch, _Eng("shared"))
    built = []
    monkeypatch.setattr(f, "_build_live_quotes_engine", lambda d: built.append(d) or _Eng("own"))
    a, _ = f._get_live_quotes_engine()
    b, _ = f._get_live_quotes_engine()
    assert a is b and len(built) == 1


def test_switch_off_returns_the_shared_engine(monkeypatch):
    shared = _Eng("shared")
    _patch(monkeypatch, shared)
    monkeypatch.setenv("LIVE_QUOTES_DB_SEPARATE_ENGINE", "0")
    monkeypatch.setattr(f, "_build_live_quotes_engine", lambda d: pytest.fail("must not build"))
    eng, _ = f._get_live_quotes_engine()
    assert eng is shared
    assert f.live_quotes_engine_status()["kind"] == "shared"


def test_blank_switch_means_on(monkeypatch):
    monkeypatch.setenv("LIVE_QUOTES_DB_SEPARATE_ENGINE", "  ")
    assert f.live_quotes_separate_engine() is True


def test_build_failure_falls_back_to_shared_and_retries_later(monkeypatch):
    shared = _Eng("shared")
    _patch(monkeypatch, shared)
    calls = []

    def boom(d):
        calls.append(1)
        raise RuntimeError("no wallet")

    monkeypatch.setattr(f, "_build_live_quotes_engine", boom)
    eng, _ = f._get_live_quotes_engine()
    assert eng is shared and len(calls) == 1
    assert "RuntimeError" in f.live_quotes_engine_status()["error"]
    eng, _ = f._get_live_quotes_engine()            # inside the retry window: no second build attempt
    assert eng is shared and len(calls) == 1
    f._LQ_STATE["retry_at"] = 0.0
    monkeypatch.setattr(f, "_build_live_quotes_engine", lambda d: _Eng("own"))
    eng, _ = f._get_live_quotes_engine()
    assert eng.tag == "own" and f.live_quotes_engine_status()["error"] is None


def test_no_durable_database_returns_none(monkeypatch):
    _patch(monkeypatch, None, "postgresql")
    eng, dialect = f._get_live_quotes_engine()
    assert eng is None and dialect == "postgresql"


def test_new_shared_engine_rebuilds_the_dedicated_one(monkeypatch):
    built = []
    monkeypatch.setattr(f, "_build_live_quotes_engine", lambda d: built.append(d) or _Eng(f"own{len(built)}"))
    _patch(monkeypatch, _Eng("s1"))
    a, _ = f._get_live_quotes_engine()
    _patch(monkeypatch, _Eng("s2"))
    b, _ = f._get_live_quotes_engine()
    assert a is not b and len(built) == 2


def test_oracle_build_uses_live_quotes_pool_env(monkeypatch):
    seen = {}

    class _OC:
        @staticmethod
        def build_oracle_engine(url, **kw):
            seen.update(kw)
            return _Eng("ora"), url

    monkeypatch.setattr(kv, "_neon_url", lambda: "oracle+oracledb://")
    monkeypatch.setattr(kv, "_oc", _OC, raising=False)
    monkeypatch.setenv("LIVE_QUOTES_DB_POOL_SIZE", "4")
    monkeypatch.setenv("LIVE_QUOTES_DB_MAX_OVERFLOW", "")      # blank -> default 1
    monkeypatch.setenv("LIVE_QUOTES_DB_POOL_TIMEOUT", "7")
    assert f._build_live_quotes_engine("oracle").tag == "ora"
    assert seen["db_pool_size"] == "4" and seen["db_max_overflow"] == "1" and seen["db_pool_timeout"] == "7"


def test_oracle_defaults_are_two_plus_one(monkeypatch):
    seen = {}

    class _OC:
        @staticmethod
        def build_oracle_engine(url, **kw):
            seen.update(kw)
            return _Eng("ora"), url

    monkeypatch.setattr(kv, "_neon_url", lambda: "oracle+oracledb://")
    monkeypatch.setattr(kv, "_oc", _OC, raising=False)
    f._build_live_quotes_engine("oracle")
    assert seen["db_pool_size"] == "2" and seen["db_max_overflow"] == "1"


def test_no_url_means_no_dedicated_engine(monkeypatch):
    monkeypatch.setattr(kv, "_neon_url", lambda: None)
    assert f._build_live_quotes_engine("oracle") is None


def test_postgres_pool_is_capped_for_the_free_tier(monkeypatch):
    pytest.importorskip("sqlalchemy")
    monkeypatch.setattr(kv, "_neon_url", lambda: "sqlite://")
    monkeypatch.setenv("LIVE_QUOTES_DB_POOL_SIZE", "9")
    monkeypatch.setenv("LIVE_QUOTES_DB_MAX_OVERFLOW", "9")
    seen = {}
    import sqlalchemy

    def fake_create(url, **kw):
        seen.update(kw)
        return _Eng("pg")

    monkeypatch.setattr(sqlalchemy, "create_engine", fake_create)
    assert f._build_live_quotes_engine("postgresql").tag == "pg"
    assert seen["pool_size"] == 2 and seen["max_overflow"] == 1


def test_dhan_store_status_reports_the_engine(monkeypatch):
    from dhan_data import live_store
    assert "engine" in live_store.status()
