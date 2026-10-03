"""
tests/test_kv_cache.py — coverage for api-gateway/kv_cache.py

Ported from analysis-intelligence-service/tests/test_kv_cache.py (100% there). The gateway copy
differs from that one only by: extra durable key prefixes, kv_get_stale()/get_stale(), and a
function-local `text` import in _get_neon's create_engine branch. The ported tests are unchanged
apart from the module path; the gateway-only behaviour is covered in the last section.

No network, no Redis, no database. Every test loads a FRESH copy of the module
(so module-level globals — _mem, _neon_engine, _redis, _SETTINGS_MEM — never leak
between tests), with `sqlalchemy` and `oracle_compat` replaced by small fakes in
sys.modules. The fake engine records every SQL statement + params and answers
from a scripted responder, so both dialect branches (postgresql / oracle) are
driven without a real database.

Run from services/api-gateway:
    python3 -m pytest tests/test_kv_cache.py -v
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sys
import types
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_KV_PATH = os.path.join(os.path.dirname(_HERE), "kv_cache.py")

_ENV_KEYS = (
    "USE_REDIS", "DISABLE_UPSTASH", "DISABLE_REDIS", "KV_MEMORY_MAX_KEYS",
    "CACHE_DATABASE_URL", "KV_DATABASE_URL", "DATABASE_URL", "TRAINING_DATABASE_URL",
    "ORACLE_DSN", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
    "CACHE_DB_POOL_SIZE", "CACHE_DB_MAX_OVERFLOW", "CACHE_DB_POOL_RECYCLE",
    "CACHE_DB_CONNECT_TIMEOUT", "CACHE_DB_POOL_SIZE_ORACLE",
    "CACHE_DB_MAX_OVERFLOW_ORACLE", "CACHE_DB_POOL_TIMEOUT",
)


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeText:
    def __init__(self, sql):
        self.sql = sql
        self.bound = []

    def bindparams(self, *a):
        self.bound.extend(a)
        return self

    def __str__(self):
        return self.sql


class FakeResult:
    def __init__(self, rows=None):
        self._rows = list(rows or [])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, eng):
        self.eng = eng

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.eng.calls.append((sql, params))
        return self.eng.responder(sql, params) or FakeResult()


class _Ctx:
    def __init__(self, eng, kind):
        self.eng, self.kind = eng, kind

    def __enter__(self):
        self.eng.opened.append(self.kind)
        return FakeConn(self.eng)

    def __exit__(self, *a):
        return False


class FakeEngine:
    def __init__(self, responder=None):
        self.calls = []
        self.opened = []
        self.responder = responder or (lambda sql, params: None)

    def connect(self):
        return _Ctx(self, "connect")

    def begin(self):
        return _Ctx(self, "begin")

    def sqls(self):
        return [c[0] for c in self.calls]

    def find(self, needle):
        return [c for c in self.calls if needle in c[0]]


class FakeRedis:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.ops = []
        self.fail = False

    def get(self, k):
        if self.fail:
            raise RuntimeError("redis down")
        self.ops.append(("get", k))
        return self.data.get(k)

    def set(self, k, v):
        if self.fail:
            raise RuntimeError("redis down")
        self.ops.append(("set", k, v))
        self.data[k] = v

    def setex(self, k, ttl, v):
        if self.fail:
            raise RuntimeError("redis down")
        self.ops.append(("setex", k, ttl, v))
        self.data[k] = v

    def delete(self, k):
        if self.fail:
            raise RuntimeError("redis down")
        self.ops.append(("delete", k))
        self.data.pop(k, None)


class FakeOC:
    """Stand-in for oracle_compat."""

    def __init__(self):
        self.ddl = []
        self.built = []
        self.engine = FakeEngine()
        self.configured = False

    def oracle_is_configured(self, url=""):
        return self.configured or (url or "").lower().startswith("oracle")

    def build_oracle_engine(self, url="", **kw):
        self.built.append((url, kw))
        return self.engine, None

    def exec_ddl_safe(self, eng, sql, dialect):
        self.ddl.append((sql, dialect))

    def create_table_sql(self, dialect, table, with_expires):
        return f"CT[{dialect}:{table}:{with_expires}]"

    def create_index_sql(self, dialect, index, table, col):
        return f"CI[{dialect}:{index}:{table}:{col}]"

    def upsert_sql(self, dialect, table, with_expires):
        return f"UPSERT[{dialect}:{table}:{with_expires}]"


class FakeSA:
    """Stand-in for the `sqlalchemy` module (text / create_engine / bindparam)."""

    def __init__(self):
        self.engine = FakeEngine()
        self.create_calls = []
        self.create_raises = None

    def module(self):
        m = types.ModuleType("sqlalchemy")
        m.text = FakeText
        m.bindparam = lambda name, **kw: ("bindparam", name, kw)

        def create_engine(url, **kw):
            self.create_calls.append((url, kw))
            if self.create_raises:
                raise self.create_raises
            return self.engine

        m.create_engine = create_engine
        return m


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def oc():
    return FakeOC()


@pytest.fixture
def sa(monkeypatch):
    fake = FakeSA()
    monkeypatch.setitem(sys.modules, "sqlalchemy", fake.module())
    return fake


@pytest.fixture
def load(monkeypatch, oc, sa):
    """load(**env) -> a fresh kv_cache module object."""

    def _load(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setitem(sys.modules, "oracle_compat", oc)
        spec = importlib.util.spec_from_file_location("kv_cache_under_test", _KV_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    return _load


@pytest.fixture
def kv(load):
    return load()


def install(mod, eng, dialect="postgresql"):
    """Pretend _get_neon() already built `eng`."""
    mod._neon_init = True
    mod._neon_engine = eng
    mod._neon_dialect = dialect
    return eng


def install_redis(mod, r=None):
    r = r if r is not None else FakeRedis()
    mod._redis = r
    mod._redis_init = True
    return r


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


# ── import-time config ────────────────────────────────────────────────────────

class TestImportConfig:
    def test_defaults(self, kv):
        assert kv.USE_REDIS is False
        assert kv.KV_MEMORY_MAX_KEYS == 8000
        assert kv._mem._max == 8000

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes"])
    def test_use_redis_truthy(self, load, val):
        assert load(USE_REDIS=val).USE_REDIS is True

    def test_use_redis_falsey(self, load):
        assert load(USE_REDIS="0").USE_REDIS is False
        assert load(USE_REDIS="no").USE_REDIS is False

    def test_disable_upstash_overrides(self, load):
        assert load(USE_REDIS="1", DISABLE_UPSTASH="1").USE_REDIS is False

    def test_disable_redis_overrides(self, load):
        assert load(USE_REDIS="1", DISABLE_REDIS="true").USE_REDIS is False

    def test_memory_max_keys_env(self, load):
        m = load(KV_MEMORY_MAX_KEYS="500")
        assert m.KV_MEMORY_MAX_KEYS == 500 and m._mem._max == 500

    def test_memory_max_keys_floor_is_100(self, load):
        assert load(KV_MEMORY_MAX_KEYS="5")._mem._max == 100

    def test_oracle_compat_missing_leaves_oc_none(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "oracle_compat", None)  # import -> ImportError
        spec = importlib.util.spec_from_file_location("kv_cache_no_oc", _KV_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod._oc is None

    def test_initial_globals(self, kv):
        assert kv._neon_engine is None and kv._neon_init is False
        assert kv._neon_dialect == "postgresql"
        assert kv._redis is None and kv._redis_init is False
        assert kv._dialect() == "postgresql"


# ── MemoryTTLCache ────────────────────────────────────────────────────────────

class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch, kv):
    c = Clock()
    monkeypatch.setattr(kv, "time", SimpleNamespace(time=c))
    return c


class TestMemoryTTLCache:
    def test_get_missing(self, kv):
        assert kv.MemoryTTLCache().get("nope") is None

    def test_set_get_no_ttl(self, kv):
        c = kv.MemoryTTLCache()
        c.set("a", {"x": 1})
        assert c.get("a") == {"x": 1}
        assert c._store["a"].expires_at is None

    def test_zero_ttl_means_no_expiry(self, kv):
        c = kv.MemoryTTLCache()
        c.set("a", 1, ttl=0)
        assert c._store["a"].expires_at is None

    def test_ttl_expiry_removes_entry(self, kv, clock):
        c = kv.MemoryTTLCache()
        c.set("a", 1, ttl=10)
        clock.now += 5
        assert c.get("a") == 1
        clock.now += 6
        assert c.get("a") is None
        assert "a" not in c._store

    def test_falsy_values_are_stored(self, kv):
        c = kv.MemoryTTLCache()
        c.set("z", 0)
        assert c.get("z") == 0

    def test_delete(self, kv):
        c = kv.MemoryTTLCache()
        c.set("a", 1)
        c.delete("a")
        c.delete("a")  # deleting a missing key is fine
        assert c.get("a") is None

    def test_ttl_missing(self, kv):
        assert kv.MemoryTTLCache().ttl("nope") == -2

    def test_ttl_no_expiry(self, kv):
        c = kv.MemoryTTLCache()
        c.set("a", 1)
        assert c.ttl("a") == -1

    def test_ttl_remaining_and_expired(self, kv, clock):
        c = kv.MemoryTTLCache()
        c.set("a", 1, ttl=30)
        clock.now += 10
        assert c.ttl("a") == 20
        clock.now += 25
        assert c.ttl("a") == -2

    def test_max_keys_floor(self, kv):
        assert kv.MemoryTTLCache(max_keys=1)._max == 100

    def test_full_evicts_expired_first_capped_at_tenth(self, kv, clock):
        c = kv.MemoryTTLCache(max_keys=100)
        for i in range(100):
            c.set(f"k{i}", i, ttl=5)  # all expire
        clock.now += 10
        c.set("new", "v")
        # 100 // 10 = 10 expired entries dropped, then the new key added
        assert len(c._store) == 91
        assert "new" in c._store and "k0" not in c._store and "k10" in c._store

    def test_full_expired_eviction_can_be_enough(self, kv, clock):
        c = kv.MemoryTTLCache(max_keys=100)
        for i in range(95):
            c.set(f"p{i}", i)  # permanent
        for i in range(5):
            c.set(f"e{i}", i, ttl=1)
        clock.now += 5
        c.set("new", 1)
        # 5 expired removed -> 95 < max, so no oldest-first eviction
        assert len(c._store) == 96
        assert "p0" in c._store

    def test_full_no_expired_drops_oldest_twentieth(self, kv):
        c = kv.MemoryTTLCache(max_keys=100)
        for i in range(100):
            c.set(f"k{i}", i)
        c.set("new", 1)
        # nothing expired -> oldest 100 // 20 = 5 dropped (insertion order, not LRU)
        assert len(c._store) == 96
        assert all(f"k{i}" not in c._store for i in range(5))
        assert "k5" in c._store and "new" in c._store

    def test_overwrite_at_capacity_does_not_evict(self, kv):
        c = kv.MemoryTTLCache(max_keys=100)
        for i in range(100):
            c.set(f"k{i}", i)
        c.set("k0", "changed")
        assert len(c._store) == 100
        assert c.get("k0") == "changed"


# ── _is_durable / _normalize_db_url / _neon_url ───────────────────────────────

class TestIsDurable:
    @pytest.mark.parametrize("key", [
        "stockky:watchlist", "stockky:watchlist:extra", "feed:RELIANCE",
        "data_feed:x", "stockky:lock:abc", "indianapi:fundamentals:TCS",
        "stockky:rate_limit_stats", "system:rate_limit", "stockky:data_feed:sym:TCS",
        "stockky:notification:x", "stockky:batch_result:1",
    ])
    def test_durable(self, kv, key):
        assert kv._is_durable(key) is True

    @pytest.mark.parametrize("key", ["", "random", "stockky:other", "xfeed:1", "cache:stockky:watchlist"])
    def test_not_durable(self, kv, key):
        assert kv._is_durable(key) is False


class TestNormalizeDbUrl:
    def test_postgres_scheme_and_sslmode_appended(self, kv):
        assert kv._normalize_db_url("postgres://u:p@h/db") == "postgresql://u:p@h/db?sslmode=require"

    def test_sslmode_appended_with_ampersand_when_query_exists(self, kv):
        assert kv._normalize_db_url("postgresql://h/db?application_name=x") == \
            "postgresql://h/db?application_name=x&sslmode=require"

    def test_channel_binding_only(self, kv):
        assert kv._normalize_db_url("postgresql://h/db?channel_binding=require") == \
            "postgresql://h/db?sslmode=require"

    def test_channel_binding_last(self, kv):
        assert kv._normalize_db_url("postgresql://h/db?sslmode=require&channel_binding=require") == \
            "postgresql://h/db?sslmode=require"

    def test_channel_binding_first(self, kv):
        assert kv._normalize_db_url("postgresql://h/db?channel_binding=require&sslmode=require") == \
            "postgresql://h/db?sslmode=require"

    def test_channel_binding_middle(self, kv):
        assert kv._normalize_db_url("postgresql://h/db?a=1&channel_binding=require&sslmode=require") == \
            "postgresql://h/db?a=1&&sslmode=require"

    def test_sslmode_required_is_fixed(self, kv):
        assert kv._normalize_db_url("postgresql://h/db?sslmode=required") == "postgresql://h/db?sslmode=require"

    def test_sslmode_required_case_insensitive(self, kv):
        assert kv._normalize_db_url("postgresql://h/db?SSLMODE=Required") == "postgresql://h/db?SSLMODE=require"

    def test_existing_valid_sslmode_untouched(self, kv):
        assert kv._normalize_db_url("postgresql://h/db?sslmode=verify-full") == "postgresql://h/db?sslmode=verify-full"


class TestNeonUrl:
    def test_none_when_unset(self, kv):
        assert kv._neon_url() is None

    def test_env_priority(self, kv, monkeypatch):
        monkeypatch.setenv("TRAINING_DATABASE_URL", "postgresql://train/db")
        assert kv._neon_url() == "postgresql://train/db?sslmode=require"
        monkeypatch.setenv("DATABASE_URL", "postgresql://main/db")
        assert kv._neon_url() == "postgresql://main/db?sslmode=require"
        monkeypatch.setenv("KV_DATABASE_URL", "postgresql://kv/db")
        assert kv._neon_url() == "postgresql://kv/db?sslmode=require"
        monkeypatch.setenv("CACHE_DATABASE_URL", "postgresql://cache/db")
        assert kv._neon_url() == "postgresql://cache/db?sslmode=require"

    def test_oracle_url_returned_untouched(self, kv, monkeypatch):
        monkeypatch.setenv("CACHE_DATABASE_URL", "oracle+oracledb://u:p@dsn")
        assert kv._neon_url() == "oracle+oracledb://u:p@dsn"

    def test_oracle_env_only_returns_sentinel(self, kv, oc):
        oc.configured = True  # e.g. ORACLE_DSN set, no URL at all
        assert kv._neon_url() == "oracle+oracledb://"

    def test_oracle_env_with_postgres_url_keeps_url(self, kv, oc, monkeypatch):
        oc.configured = True
        monkeypatch.setenv("DATABASE_URL", "postgres://x/y?channel_binding=require")
        assert kv._neon_url() == "postgres://x/y?channel_binding=require"

    def test_without_oracle_compat(self, kv, monkeypatch):
        monkeypatch.setattr(kv, "_oc", None)
        monkeypatch.setenv("DATABASE_URL", "postgres://h/d")
        assert kv._neon_url() == "postgresql://h/d?sslmode=require"


# ── _init_durable_schema ──────────────────────────────────────────────────────

class TestInitDurableSchema:
    def test_postgres_creates_three_tables_two_indexes_in_one_txn(self, kv):
        eng = FakeEngine()
        kv._init_durable_schema(eng)
        assert eng.opened == ["begin"]
        sqls = " ".join(eng.sqls())
        for name in ("stockky_kv", "stockky_notification", "stockky_watchlist",
                     "stockky_kv_expires_idx", "idx_stockky_kv_k"):
            assert name in sqls
        assert len(eng.calls) == 5

    def test_oracle_uses_exec_ddl_safe(self, kv, oc):
        kv._neon_dialect = "oracle"
        eng = FakeEngine()
        kv._init_durable_schema(eng)
        assert eng.calls == []
        assert oc.ddl == [
            ("CT[oracle:stockky_kv:True]", "oracle"),
            ("CI[oracle:stockky_kv_expires_idx:stockky_kv:expires_at]", "oracle"),
            ("CI[oracle:idx_stockky_kv_k:stockky_kv:k]", "oracle"),
            ("CT[oracle:stockky_notification:False]", "oracle"),
            ("CT[oracle:stockky_watchlist:False]", "oracle"),
        ]

    def test_oracle_dialect_without_shim_falls_back_to_postgres_ddl(self, kv, monkeypatch):
        kv._neon_dialect = "oracle"
        monkeypatch.setattr(kv, "_oc", None)
        eng = FakeEngine()
        kv._init_durable_schema(eng)
        assert len(eng.calls) == 5


# ── _get_neon ─────────────────────────────────────────────────────────────────

class TestGetNeon:
    def test_returns_cached_engine(self, kv):
        sentinel = object()
        kv._neon_init, kv._neon_engine = True, sentinel
        assert kv._get_neon() is sentinel

    def test_no_url_gives_none_and_is_remembered(self, kv, sa, monkeypatch):
        spy = LogSpy()
        monkeypatch.setattr(kv, "logger", spy)
        assert kv._get_neon() is None
        assert kv._neon_init is True
        assert sa.create_calls == []
        assert "memory-only" in spy.text()
        assert kv._get_neon() is None  # second call short-circuits

    def test_double_checked_lock_inner_return(self, kv):
        sentinel = object()

        class RacingLock:
            def __enter__(self):
                # another thread finished initialising while we waited for the lock
                kv._neon_init = True
                kv._neon_engine = sentinel

            def __exit__(self, *a):
                return False

        kv._neon_lock = RacingLock()
        assert kv._get_neon() is sentinel

    def test_postgres_engine_built(self, kv, sa, monkeypatch):
        monkeypatch.setenv("CACHE_DATABASE_URL", "postgres://u:p@h/db?channel_binding=require")
        eng = kv._get_neon()
        assert eng is sa.engine
        url, kw = sa.create_calls[0]
        assert url == "postgresql://u:p@h/db?sslmode=require"
        assert kw["pool_pre_ping"] is True
        assert kw["pool_size"] == 1 and kw["max_overflow"] == 1
        assert kw["pool_recycle"] == 180 and kw["pool_use_lifo"] is True
        assert kw["pool_timeout"] == 8
        assert kw["connect_args"] == {"connect_timeout": 6, "application_name": "stockky-kv-cache"}
        assert kv._neon_dialect == "postgresql"
        assert len(sa.engine.calls) == 5  # schema created

    @pytest.mark.parametrize("size,overflow,exp_size,exp_over", [
        ("5", "5", 2, 1),   # hard-capped for Neon free tier
        ("0", "0", 1, 0),   # pool never below 1, overflow never below 0
        ("2", "1", 2, 1),
    ])
    def test_pool_clamping(self, kv, sa, monkeypatch, size, overflow, exp_size, exp_over):
        monkeypatch.setenv("DATABASE_URL", "postgresql://h/db")
        monkeypatch.setenv("CACHE_DB_POOL_SIZE", size)
        monkeypatch.setenv("CACHE_DB_MAX_OVERFLOW", overflow)
        monkeypatch.setenv("CACHE_DB_POOL_RECYCLE", "60")
        monkeypatch.setenv("CACHE_DB_CONNECT_TIMEOUT", "3")
        kv._get_neon()
        kw = sa.create_calls[0][1]
        assert kw["pool_size"] == exp_size and kw["max_overflow"] == exp_over
        assert kw["pool_recycle"] == 60
        assert kw["connect_args"]["connect_timeout"] == 3

    def test_create_engine_failure_is_memory_only(self, kv, sa, monkeypatch):
        spy = LogSpy()
        monkeypatch.setattr(kv, "logger", spy)
        monkeypatch.setenv("DATABASE_URL", "postgresql://h/db")
        sa.create_raises = RuntimeError("boom")
        assert kv._get_neon() is None
        assert kv._neon_engine is None
        assert "warning" in spy.levels() and "boom" in spy.text()
        assert kv._get_neon() is None  # failure is remembered, no retry storm
        assert len(sa.create_calls) == 1

    def test_schema_failure_is_memory_only(self, kv, sa, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://h/db")

        def bad(*a, **k):
            raise RuntimeError("ddl failed")

        monkeypatch.setattr(kv, "_init_durable_schema", bad)
        assert kv._get_neon() is None
        assert kv._neon_engine is None

    def test_sqlalchemy_import_failure(self, kv, monkeypatch):
        monkeypatch.setitem(sys.modules, "sqlalchemy", None)
        monkeypatch.setenv("DATABASE_URL", "postgresql://h/db")
        assert kv._get_neon() is None

    def test_oracle_engine_built_with_defaults(self, kv, oc, sa, monkeypatch):
        monkeypatch.setenv("CACHE_DATABASE_URL", "oracle+oracledb://u:p@dsn")
        eng = kv._get_neon()
        assert eng is oc.engine
        url, kw = oc.built[0]
        assert url == "oracle+oracledb://u:p@dsn"
        assert kw == {"db_pool_size": "5", "db_max_overflow": "3",
                      "db_pool_recycle": "300", "db_pool_timeout": "10"}
        assert kv._neon_dialect == "oracle"
        assert len(oc.ddl) == 5
        assert sa.create_calls == []

    def test_oracle_env_overrides_and_fallbacks(self, kv, oc, monkeypatch):
        monkeypatch.setenv("CACHE_DATABASE_URL", "oracle+oracledb://x")
        monkeypatch.setenv("CACHE_DB_POOL_SIZE", "7")          # shared fallback
        monkeypatch.setenv("CACHE_DB_MAX_OVERFLOW_ORACLE", "9")  # dedicated wins
        monkeypatch.setenv("CACHE_DB_POOL_RECYCLE", "111")
        monkeypatch.setenv("CACHE_DB_POOL_TIMEOUT", "42")
        kv._get_neon()
        kw = oc.built[0][1]
        assert kw == {"db_pool_size": "7", "db_max_overflow": "9",
                      "db_pool_recycle": "111", "db_pool_timeout": "42"}

    def test_dedicated_oracle_pool_size_beats_shared(self, kv, oc, monkeypatch):
        monkeypatch.setenv("CACHE_DATABASE_URL", "oracle+oracledb://x")
        monkeypatch.setenv("CACHE_DB_POOL_SIZE", "7")
        monkeypatch.setenv("CACHE_DB_POOL_SIZE_ORACLE", "11")
        kv._get_neon()
        assert oc.built[0][1]["db_pool_size"] == "11"


# ── _neon_get ─────────────────────────────────────────────────────────────────

UTC = dt.timezone.utc


def _future(sec=3600):
    return dt.datetime.now(UTC) + dt.timedelta(seconds=sec)


def _past(sec=3600):
    return dt.datetime.now(UTC) - dt.timedelta(seconds=sec)


class TestNeonGet:
    def test_no_engine(self, kv):
        assert kv._neon_get("k") is None

    def test_missing_row(self, kv):
        eng = install(kv, FakeEngine())
        assert kv._neon_get("k") is None
        assert eng.calls[0][1] == {"k": "k"}

    def test_json_value_no_expiry(self, kv):
        install(kv, FakeEngine(lambda s, p: FakeResult([(json.dumps({"a": 1}), None)])))
        assert kv._neon_get("k") == {"a": 1}

    def test_non_json_value_returned_raw(self, kv):
        install(kv, FakeEngine(lambda s, p: FakeResult([("not json {", None)])))
        assert kv._neon_get("k") == "not json {"

    def test_future_expiry_tz_aware(self, kv):
        install(kv, FakeEngine(lambda s, p: FakeResult([("5", _future())])))
        assert kv._neon_get("k") == 5

    def test_future_expiry_naive_is_treated_as_utc(self, kv):
        naive = (dt.datetime.now(UTC) + dt.timedelta(hours=1)).replace(tzinfo=None)
        install(kv, FakeEngine(lambda s, p: FakeResult([("5", naive)])))
        assert kv._neon_get("k") == 5

    def test_expired_row_is_deleted_and_misses(self, kv):
        def responder(sql, params):
            if sql.startswith("SELECT"):
                return FakeResult([("5", _past())])

        eng = install(kv, FakeEngine(responder))
        assert kv._neon_get("k") is None
        assert eng.find("DELETE FROM stockky_kv")[0][1] == {"k": "k"}
        assert eng.opened == ["connect", "begin"]

    def test_expired_naive_row_is_deleted(self, kv):
        naive_past = (dt.datetime.now(UTC) - dt.timedelta(hours=1)).replace(tzinfo=None)

        def responder(sql, params):
            if sql.startswith("SELECT"):
                return FakeResult([("5", naive_past)])

        eng = install(kv, FakeEngine(responder))
        assert kv._neon_get("k") is None
        assert eng.find("DELETE")

    def test_db_error_returns_none(self, kv):
        def boom(sql, params):
            raise RuntimeError("db down")

        install(kv, FakeEngine(boom))
        assert kv._neon_get("k") is None


# ── _neon_set ─────────────────────────────────────────────────────────────────

class TestNeonSet:
    def test_no_engine_is_noop(self, kv):
        kv._neon_set("k", 1)  # must not raise

    def test_postgres_upsert_no_ttl(self, kv):
        eng = install(kv, FakeEngine())
        kv._neon_set("k", {"a": 1})
        sql, params = eng.calls[0]
        assert "INSERT INTO stockky_kv" in sql and "ON CONFLICT (k) DO UPDATE" in sql
        assert params == {"k": "k", "v": json.dumps({"a": 1}), "e": None}
        assert eng.opened == ["begin"]

    def test_ttl_sets_expiry(self, kv):
        eng = install(kv, FakeEngine())
        before = dt.datetime.now(UTC)
        kv._neon_set("k", 1, ttl=100)
        exp = eng.calls[0][1]["e"]
        assert before + dt.timedelta(seconds=99) <= exp <= dt.datetime.now(UTC) + dt.timedelta(seconds=101)

    def test_non_json_types_are_stringified(self, kv):
        eng = install(kv, FakeEngine())
        kv._neon_set("k", {"when": dt.date(2026, 9, 29)})
        assert json.loads(eng.calls[0][1]["v"]) == {"when": "2026-09-29"}

    def test_oracle_uses_merge(self, kv):
        eng = install(kv, FakeEngine(), dialect="oracle")
        kv._neon_set("k", "v", ttl=5)
        sql, params = eng.calls[0]
        assert sql == "UPSERT[oracle:stockky_kv:True]"
        assert params["k"] == "k" and params["v"] == '"v"' and params["e"] is not None

    def test_oracle_dialect_without_shim_uses_postgres_sql(self, kv, monkeypatch):
        eng = install(kv, FakeEngine(), dialect="oracle")
        monkeypatch.setattr(kv, "_oc", None)
        kv._neon_set("k", 1)
        assert "INSERT INTO stockky_kv" in eng.calls[0][0]

    def test_error_is_swallowed(self, kv):
        def boom(sql, params):
            raise RuntimeError("db down")

        install(kv, FakeEngine(boom))
        kv._neon_set("k", 1)


# ── _get_redis ────────────────────────────────────────────────────────────────

def _fake_upstash(monkeypatch, ping_raises=None, ctor_raises=None):
    made = []

    class Redis:
        def __init__(self, url, token):
            if ctor_raises:
                raise ctor_raises
            made.append((url, token))

        def ping(self):
            if ping_raises:
                raise ping_raises
            return True

    m = types.ModuleType("upstash_redis")
    m.Redis = Redis
    monkeypatch.setitem(sys.modules, "upstash_redis", m)
    return made


class TestGetRedis:
    def test_cached(self, kv):
        r = install_redis(kv, FakeRedis())
        assert kv._get_redis() is r

    def test_disabled_by_default(self, kv, monkeypatch):
        spy = LogSpy()
        monkeypatch.setattr(kv, "logger", spy)
        assert kv._get_redis() is None
        assert kv._redis_init is True
        assert "USE_REDIS=0" in spy.text()

    def test_enabled_but_no_credentials(self, load):
        m = load(USE_REDIS="1")
        assert m._get_redis() is None

    def test_enabled_missing_token(self, load):
        m = load(USE_REDIS="1", UPSTASH_REDIS_REST_URL="https://r")
        assert m._get_redis() is None

    def test_connects(self, load, monkeypatch):
        made = _fake_upstash(monkeypatch)
        m = load(USE_REDIS="1", UPSTASH_REDIS_REST_URL="https://r", UPSTASH_REDIS_REST_TOKEN="tok")
        r = m._get_redis()
        assert r is not None and made == [("https://r", "tok")]
        assert m._get_redis() is r

    def test_ping_failure(self, load, monkeypatch):
        _fake_upstash(monkeypatch, ping_raises=RuntimeError("no ping"))
        m = load(USE_REDIS="1", UPSTASH_REDIS_REST_URL="https://r", UPSTASH_REDIS_REST_TOKEN="tok")
        assert m._get_redis() is None
        assert m._get_redis() is None  # remembered

    def test_client_missing(self, load, monkeypatch):
        monkeypatch.setitem(sys.modules, "upstash_redis", None)
        m = load(USE_REDIS="1", UPSTASH_REDIS_REST_URL="https://r", UPSTASH_REDIS_REST_TOKEN="tok")
        assert m._get_redis() is None


# ── kv_get ────────────────────────────────────────────────────────────────────

class TestKvGet:
    def test_memory_hit_short_circuits(self, kv, monkeypatch):
        kv._mem.set("k", "v")
        monkeypatch.setattr(kv, "_get_redis", lambda: (_ for _ in ()).throw(AssertionError("redis used")))
        assert kv.kv_get("k") == "v"

    def test_miss_everywhere(self, kv):
        assert kv.kv_get("nothing") is None

    def test_redis_json_string(self, kv):
        install_redis(kv, FakeRedis({"k": json.dumps({"a": 1})}))
        assert kv.kv_get("k") == {"a": 1}
        assert kv._mem.get("k") == {"a": 1}

    def test_redis_bytes_decoded(self, kv):
        install_redis(kv, FakeRedis({"k": b'[1, 2]'}))
        assert kv.kv_get("k") == [1, 2]

    def test_redis_bytearray_decoded(self, kv):
        install_redis(kv, FakeRedis({"k": bytearray(b'"hi"')}))
        assert kv.kv_get("k") == "hi"

    def test_redis_non_json_string_returned_raw(self, kv):
        install_redis(kv, FakeRedis({"k": "plain text"}))
        assert kv.kv_get("k") == "plain text"

    def test_redis_already_decoded_object(self, kv):
        install_redis(kv, FakeRedis({"k": {"already": "decoded"}}))
        assert kv.kv_get("k") == {"already": "decoded"}

    def test_redis_hit_is_cached_in_memory_for_300s(self, kv):
        install_redis(kv, FakeRedis({"k": "1"}))
        kv.kv_get("k")
        assert 295 <= kv._mem.ttl("k") <= 300

    def test_redis_miss_non_durable_key_does_not_touch_neon(self, kv, monkeypatch):
        install_redis(kv, FakeRedis())
        monkeypatch.setattr(kv, "_neon_get", lambda k: (_ for _ in ()).throw(AssertionError("neon used")))
        assert kv.kv_get("plain") is None

    def test_redis_error_falls_through_to_neon(self, kv):
        r = install_redis(kv, FakeRedis())
        r.fail = True
        install(kv, FakeEngine(lambda s, p: FakeResult([('"from-neon"', None)])))
        assert kv.kv_get("stockky:watchlist") == "from-neon"

    def test_neon_hit_cached_in_memory_for_600s(self, kv):
        install(kv, FakeEngine(lambda s, p: FakeResult([('{"x": 1}', None)])))
        assert kv.kv_get("stockky:watchlist") == {"x": 1}
        assert 595 <= kv._mem.ttl("stockky:watchlist") <= 600

    def test_neon_hit_memory_copy_is_capped_at_the_rows_remaining_life(self, kv):
        """Fixed: the memory copy used to get a flat 600s even when the Neon row
        expired in 5s, so it was served from memory long after the row was gone."""
        install(kv, FakeEngine(lambda s, p: FakeResult([("1", _future(5))])))
        assert kv.kv_get("stockky:watchlist") == 1
        assert 1 <= kv._mem.ttl("stockky:watchlist") <= 5

    def test_neon_hit_with_long_lived_row_still_gets_the_600s_ceiling(self, kv):
        install(kv, FakeEngine(lambda s, p: FakeResult([("1", _future(7200))])))
        assert kv.kv_get("stockky:watchlist") == 1
        assert 595 <= kv._mem.ttl("stockky:watchlist") <= 600

    def test_neon_hit_about_to_expire_never_becomes_an_immortal_memory_entry(self, kv):
        """A row with <1s left must not produce ttl=0 (MemoryTTLCache treats a falsy
        ttl as 'never expires')."""
        install(kv, FakeEngine(lambda s, p: FakeResult([("1", _future(0.2))])))
        assert kv.kv_get("stockky:watchlist") == 1
        assert kv._mem.ttl("stockky:watchlist") != -1

    def test_durable_miss(self, kv):
        install(kv, FakeEngine())
        assert kv.kv_get("stockky:watchlist") is None

    def test_non_durable_key_never_queries_neon(self, kv):
        eng = install(kv, FakeEngine())
        assert kv.kv_get("just:memory") is None
        assert eng.calls == []


# ── kv_set / kv_delete / kv_ttl ───────────────────────────────────────────────

class TestKvSet:
    def test_memory_only_by_default(self, kv, monkeypatch):
        spy = []
        monkeypatch.setattr(kv, "_neon_set", lambda *a, **k: spy.append((a, k)))
        kv.kv_set("plain", {"a": 1})
        assert kv._mem.get("plain") == {"a": 1}
        assert spy == []

    def test_ttl_applied_to_memory(self, kv):
        kv.kv_set("plain", 1, ttl=100)
        assert 95 <= kv.kv_ttl("plain") <= 100

    def test_kv_ttl_missing(self, kv):
        assert kv.kv_ttl("missing") == -2

    def test_redis_setex_with_ttl(self, kv):
        r = install_redis(kv, FakeRedis())
        kv.kv_set("k", {"a": 1}, ttl=30.7)
        assert r.ops == [("setex", "k", 30, json.dumps({"a": 1}))]

    def test_redis_set_without_ttl(self, kv):
        r = install_redis(kv, FakeRedis())
        kv.kv_set("k", "v")
        assert r.ops == [("set", "k", '"v"')]

    def test_redis_error_is_swallowed(self, kv):
        r = install_redis(kv, FakeRedis())
        r.fail = True
        kv.kv_set("k", "v")
        assert kv._mem.get("k") == "v"

    def test_durable_key_written_to_neon(self, kv, monkeypatch):
        spy = []
        monkeypatch.setattr(kv, "_neon_set", lambda k, v, ttl=None: spy.append((k, v, ttl)))
        kv.kv_set("stockky:watchlist", ["A"], ttl=9)
        assert spy == [("stockky:watchlist", ["A"], 9)]

    def test_durable_write_reaches_engine(self, kv):
        eng = install(kv, FakeEngine())
        kv.kv_set("feed:X", {"p": 1})
        assert eng.find("INSERT INTO stockky_kv")


class TestKvDelete:
    def test_memory_delete(self, kv):
        kv._mem.set("plain", 1)
        kv.kv_delete("plain")
        assert kv._mem.get("plain") is None

    def test_redis_delete(self, kv):
        r = install_redis(kv, FakeRedis({"k": "1"}))
        kv.kv_delete("k")
        assert ("delete", "k") in r.ops

    def test_redis_error_swallowed(self, kv):
        r = install_redis(kv, FakeRedis())
        r.fail = True
        kv.kv_delete("k")

    def test_durable_delete_hits_neon(self, kv):
        eng = install(kv, FakeEngine())
        kv.kv_delete("stockky:watchlist")
        assert eng.find("DELETE FROM stockky_kv")[0][1] == {"k": "stockky:watchlist"}

    def test_durable_delete_without_engine(self, kv):
        kv.kv_delete("stockky:watchlist")  # no engine configured: fine

    def test_durable_delete_db_error_swallowed(self, kv):
        def boom(sql, params):
            raise RuntimeError("db down")

        install(kv, FakeEngine(boom))
        kv.kv_delete("stockky:watchlist")

    def test_non_durable_delete_skips_neon(self, kv):
        eng = install(kv, FakeEngine())
        kv.kv_delete("plain")
        assert eng.calls == []


# ── kv_set_many ───────────────────────────────────────────────────────────────

class TestKvSetMany:
    def test_empty_is_noop(self, kv, monkeypatch):
        monkeypatch.setattr(kv, "_get_redis", lambda: (_ for _ in ()).throw(AssertionError))
        kv.kv_set_many({})

    def test_memory_written_for_all_with_ttl(self, kv):
        kv.kv_set_many({"a": 1, "b": 2}, ttl=50)
        assert kv._mem.get("a") == 1 and kv._mem.get("b") == 2
        assert 45 <= kv._mem.ttl("a") <= 50

    def test_no_durable_keys_never_asks_for_engine(self, kv, monkeypatch):
        monkeypatch.setattr(kv, "_get_neon", lambda: (_ for _ in ()).throw(AssertionError("engine used")))
        kv.kv_set_many({"a": 1})

    def test_no_engine(self, kv):
        kv.kv_set_many({"feed:A": 1})
        assert kv._mem.get("feed:A") == 1

    def test_redis_setex_and_set(self, kv):
        r = install_redis(kv, FakeRedis())
        kv.kv_set_many({"a": 1}, ttl=10)
        kv.kv_set_many({"b": 2})
        assert ("setex", "a", 10, "1") in r.ops and ("set", "b", "2") in r.ops

    def test_redis_error_per_item_swallowed(self, kv):
        r = install_redis(kv, FakeRedis())
        r.fail = True
        kv.kv_set_many({"a": 1, "b": 2})
        assert kv._mem.get("b") == 2

    def test_postgres_single_statement(self, kv):
        eng = install(kv, FakeEngine())
        kv.kv_set_many({"feed:A": {"x": 1}, "feed:B": 2, "plain": 3}, ttl=60)
        assert len(eng.calls) == 1 and eng.opened == ["begin"]
        sql, params = eng.calls[0]
        assert "(:k0, :v0, :e0, NOW()), (:k1, :v1, :e1, NOW())" in sql
        assert "ON CONFLICT (k) DO UPDATE" in sql
        assert params["k0"] == "feed:A" and params["k1"] == "feed:B"
        assert json.loads(params["v0"]) == {"x": 1}
        assert params["e0"] is not None and params["e0"] == params["e1"]
        assert "plain" not in params.values()  # non-durable key is not persisted

    def test_no_ttl_means_null_expiry(self, kv):
        eng = install(kv, FakeEngine())
        kv.kv_set_many({"feed:A": 1})
        assert eng.calls[0][1]["e0"] is None

    def test_chunks_of_200_in_one_transaction(self, kv):
        eng = install(kv, FakeEngine())
        kv.kv_set_many({f"feed:{i}": i for i in range(450)})
        assert eng.opened == ["begin"]
        assert len(eng.calls) == 3
        sizes = [len(c[1]) // 3 for c in eng.calls]
        assert sizes == [200, 200, 50]
        assert eng.calls[2][1]["k49"] == "feed:449"

    def test_oracle_merges_each_row_in_one_transaction(self, kv):
        eng = install(kv, FakeEngine(), dialect="oracle")
        kv.kv_set_many({"feed:A": 1, "feed:B": 2}, ttl=5)
        assert eng.opened == ["begin"]
        assert [c[0] for c in eng.calls] == ["UPSERT[oracle:stockky_kv:True]"] * 2
        assert [c[1]["k"] for c in eng.calls] == ["feed:A", "feed:B"]

    def test_bulk_failure_falls_back_to_per_key_sets(self, kv, monkeypatch):
        def boom(sql, params):
            raise RuntimeError("bulk failed")

        install(kv, FakeEngine(boom))
        spy = []
        monkeypatch.setattr(kv, "_neon_set", lambda k, v, ttl=None: spy.append((k, v, ttl)))
        kv.kv_set_many({"feed:A": 1, "feed:B": 2, "plain": 3}, ttl=7)
        assert spy == [("feed:A", 1, 7), ("feed:B", 2, 7)]

    def test_fallback_per_key_error_is_swallowed(self, kv, monkeypatch):
        def boom(sql, params):
            raise RuntimeError("bulk failed")

        install(kv, FakeEngine(boom))

        def bad_set(*a, **k):
            raise RuntimeError("also failed")

        monkeypatch.setattr(kv, "_neon_set", bad_set)
        kv.kv_set_many({"feed:A": 1, "feed:B": 2})  # must not raise


# ── kv_get_many ───────────────────────────────────────────────────────────────

class TestKvGetMany:
    def test_empty(self, kv):
        assert kv.kv_get_many([]) == {}

    def test_all_in_memory(self, kv, monkeypatch):
        kv._mem.set("a", 1)
        kv._mem.set("b", 0)  # falsy but present
        monkeypatch.setattr(kv, "_get_redis", lambda: (_ for _ in ()).throw(AssertionError))
        assert kv.kv_get_many(["a", "b"]) == {"a": 1, "b": 0}

    def test_no_backends_returns_only_memory_hits(self, kv):
        kv._mem.set("a", 1)
        assert kv.kv_get_many(["a", "zzz"]) == {"a": 1}

    def test_redis_paths(self, kv):
        r = install_redis(kv, FakeRedis({
            "j": json.dumps([1]), "bts": b'{"a": 2}', "raw": "not json", "obj": {"o": 1},
        }))
        out = kv.kv_get_many(["j", "bts", "raw", "obj", "gone"])
        assert out == {"j": [1], "bts": {"a": 2}, "raw": "not json", "obj": {"o": 1}}
        assert 295 <= kv._mem.ttl("j") <= 300
        assert ("get", "gone") in r.ops

    def test_redis_resolves_everything_before_neon(self, kv, monkeypatch):
        install_redis(kv, FakeRedis({"stockky:watchlist": "1"}))
        monkeypatch.setattr(kv, "_get_neon", lambda: (_ for _ in ()).throw(AssertionError("neon used")))
        assert kv.kv_get_many(["stockky:watchlist"]) == {"stockky:watchlist": 1}

    def test_redis_error_key_goes_on_to_neon(self, kv):
        r = install_redis(kv, FakeRedis())
        r.fail = True
        eng = install(kv, FakeEngine(lambda s, p: FakeResult([("feed:A", "5", None)])))
        assert kv.kv_get_many(["feed:A"]) == {"feed:A": 5}
        assert eng.calls

    def test_neon_any_query(self, kv):
        rows = [
            ("feed:ok", '{"a": 1}', None),
            ("feed:future", "2", _future()),
            ("feed:naive", "3", (dt.datetime.now(UTC) + dt.timedelta(hours=1)).replace(tzinfo=None)),
            ("feed:expired", "4", _past()),
            ("feed:naive-expired", "9", (dt.datetime.now(UTC) - dt.timedelta(hours=1)).replace(tzinfo=None)),
            ("feed:raw", "not json", None),
        ]
        eng = install(kv, FakeEngine(lambda s, p: FakeResult(rows)))
        keys = [r[0] for r in rows] + ["plain:not-durable"]
        out = kv.kv_get_many(keys)
        assert out == {"feed:ok": {"a": 1}, "feed:future": 2, "feed:naive": 3, "feed:raw": "not json"}
        assert len(eng.calls) == 1 and "= ANY(:keys)" in eng.calls[0][0]
        assert "plain:not-durable" not in eng.calls[0][1]["keys"]
        assert 595 <= kv._mem.ttl("feed:ok") <= 600

    def test_get_many_memory_copy_is_capped_at_the_rows_remaining_life(self, kv):
        rows = [("feed:soon", "1", _future(7)), ("feed:later", "2", _future(7200))]
        install(kv, FakeEngine(lambda s, p: FakeResult(rows)))
        assert kv.kv_get_many(["feed:soon", "feed:later"]) == {"feed:soon": 1, "feed:later": 2}
        assert 1 <= kv._mem.ttl("feed:soon") <= 7
        assert 595 <= kv._mem.ttl("feed:later") <= 600

    def test_only_non_durable_missing_skips_query(self, kv):
        eng = install(kv, FakeEngine())
        assert kv.kv_get_many(["plain1", "plain2"]) == {}
        assert eng.calls == []

    def test_oracle_expanding_in_query(self, kv):
        rows = [("feed:A", "1", None)]
        eng = install(kv, FakeEngine(lambda s, p: FakeResult(rows)), dialect="oracle")
        assert kv.kv_get_many(["feed:A", "feed:B"]) == {"feed:A": 1}
        assert len(eng.calls) == 1
        assert "IN :keys" in eng.calls[0][0]
        assert eng.calls[0][1] == {"keys": ["feed:A", "feed:B"]}

    def test_oracle_chunks_in_list_at_900(self, kv):
        eng = install(kv, FakeEngine(), dialect="oracle")
        kv.kv_get_many([f"feed:{i}" for i in range(1000)])
        assert [len(c[1]["keys"]) for c in eng.calls] == [900, 100]

    def test_bulk_error_falls_back_to_individual_gets(self, kv, monkeypatch):
        def boom(sql, params):
            raise RuntimeError("bulk failed")

        install(kv, FakeEngine(boom))
        monkeypatch.setattr(kv, "_neon_get", lambda k: {"feed:A": "a"}.get(k))
        assert kv.kv_get_many(["feed:A", "feed:B", "plain"]) == {"feed:A": "a"}


# ── module-level wrappers ─────────────────────────────────────────────────────

class TestWrappers:
    def test_delegation(self, kv, monkeypatch):
        calls = []
        monkeypatch.setattr(kv, "kv_get", lambda k: calls.append(("get", k)) or "G")
        monkeypatch.setattr(kv, "kv_set", lambda k, v, ttl=None: calls.append(("set", k, v, ttl)))
        monkeypatch.setattr(kv, "kv_delete", lambda k: calls.append(("del", k)))
        monkeypatch.setattr(kv, "kv_set_many", lambda i, ttl=None: calls.append(("set_many", i, ttl)))
        monkeypatch.setattr(kv, "kv_get_many", lambda ks: calls.append(("get_many", ks)) or {"x": 1})
        assert kv.get("a") == "G" and kv.cache_get("b") == "G"
        kv.set("c", 1, ttl=2)
        kv.cache_set("d", 3)
        kv.delete("e")
        kv.set_many({"f": 1}, ttl=9)
        assert kv.get_many(["g"]) == {"x": 1}
        assert calls == [
            ("get", "a"), ("get", "b"), ("set", "c", 1, 2), ("set", "d", 3, None),
            ("del", "e"), ("set_many", {"f": 1}, 9), ("get_many", ["g"]),
        ]

    def test_roundtrip_through_real_functions(self, kv):
        kv.set("k", {"a": 1})
        assert kv.get("k") == {"a": 1}
        kv.delete("k")
        assert kv.get("k") is None


# ── status ────────────────────────────────────────────────────────────────────

class TestStatus:
    def test_memory_only(self, kv):
        kv._mem.set("a", 1)
        s = kv.status()
        assert s == {"use_redis": False, "memory_keys": 1, "neon_connected": False,
                     "neon_error": None, "cache_database_configured": False,
                     "durable_backend": "postgresql"}

    def test_postgres_connected(self, kv, monkeypatch):
        monkeypatch.setenv("CACHE_DATABASE_URL", "postgresql://h/d")
        eng = install(kv, FakeEngine())
        s = kv.status()
        assert s["neon_connected"] is True and s["neon_error"] is None
        assert s["cache_database_configured"] is True
        assert eng.sqls() == ["SELECT 1"]

    def test_oracle_probe_uses_dual(self, kv, monkeypatch):
        monkeypatch.setenv("CACHE_DATABASE_URL", "oracle+oracledb://x")
        eng = install(kv, FakeEngine(), dialect="oracle")
        s = kv.status()
        assert eng.sqls() == ["SELECT 1 FROM dual"]
        assert s["durable_backend"] == "oracle" and s["neon_connected"] is True

    def test_probe_error_is_reported_and_truncated(self, kv):
        def boom(sql, params):
            raise RuntimeError("x" * 300)

        install(kv, FakeEngine(boom))
        s = kv.status()
        assert s["neon_connected"] is False
        assert s["neon_error"] == "x" * 120

    def test_engine_lookup_error_is_reported(self, kv, monkeypatch):
        monkeypatch.setattr(kv, "_get_neon", lambda: (_ for _ in ()).throw(RuntimeError("lookup")))
        assert kv.status()["neon_error"] == "lookup"


# ── hard_reset_stockky_kv ─────────────────────────────────────────────────────

def _sym(sym, **fields):
    return (kv_prefix() + sym, json.dumps(fields))


def kv_prefix():
    return "stockky:data_feed:sym:"


def _reset_responder(legacy=None, snapshot=None, raise_on=None):
    legacy = legacy or {}
    snapshot = snapshot or []

    def responder(sql, params):
        if raise_on and raise_on in sql:
            raise RuntimeError(f"forced failure on {raise_on}")
        if sql.startswith("SELECT v FROM stockky_kv WHERE k"):
            v = legacy.get(params["k"])
            return FakeResult([(v,)] if v is not None else [])
        if "LIKE :prefix" in sql:
            return FakeResult(snapshot)
        return None

    return responder


@pytest.fixture
def reset_env(kv, monkeypatch):
    """Skip real DDL (tested separately) and record that it ran."""
    ran = []
    monkeypatch.setattr(kv, "_init_durable_schema", lambda eng: ran.append(eng))
    return ran


class TestHardResetMemoryOnly:
    def test_clears_process_memory(self, kv):
        kv._mem.set("a", 1)
        kv._mem.set("stockky:watchlist", ["X"])
        out = kv.hard_reset_stockky_kv()
        assert out["status"] == "success" and out["mode"] == "memory-only"
        assert "memory only" in out["message"]
        assert kv._mem._store == {}

    def test_lock_failure_is_swallowed(self, kv):
        class BadLock:
            def __enter__(self):
                raise RuntimeError("lock broken")

            def __exit__(self, *a):
                return False

        kv._mem._lock = BadLock()
        assert kv.hard_reset_stockky_kv()["status"] == "success"


class TestHardResetPostgres:
    def test_full_flow(self, kv, reset_env):
        snapshot = [
            _sym("TCS", pe=25.1, roce=30, price=3500, volume=10, sector="IT", model=None,
                 updated_at="t", source="yf"),
            _sym("INFY", price=1500, close=1490),          # only volatile fields -> dropped
            (kv_prefix() + "BAD", "not json"),              # invalid JSON -> skipped
            (kv_prefix() + "LIST", json.dumps([1, 2])),     # not a dict -> skipped
        ]
        legacy = {"stockky:notification_config": '{"tg": 1}'}
        eng = install(kv, FakeEngine(_reset_responder(legacy=legacy, snapshot=snapshot)))
        kv._mem.set("junk", 1)
        kv._mem.set("stockky:notification_config", "keep")
        kv._mem.set("stockky:watchlist", "keep")
        kv._mem.set("stockky:notification:extra", "keep")

        out = kv.hard_reset_stockky_kv(preserve_days=3)

        assert reset_env == [eng]
        assert out["status"] == "success" and out["mode"] == "neon"
        assert out["preserved"] == ["stockky_notification", "stockky_watchlist"]
        assert out["preserved_symbol_fields_count"] == 1 and out["preserve_days"] == 3
        assert "restored for 1 symbols" in out["message"]

        sqls = eng.sqls()
        # legacy notification key migrated to the dedicated table
        mig = eng.find("INSERT INTO stockky_notification")
        assert len(mig) == 1 and mig[0][1] == {"k": "config", "v": '{"tg": 1}'}
        assert not eng.find("INSERT INTO stockky_watchlist")  # no legacy watchlist row
        # snapshot -> truncate -> constraint/index re-assert -> restore, in that order
        i_snap = next(i for i, s in enumerate(sqls) if "LIKE :prefix" in s)
        i_trunc = sqls.index("TRUNCATE TABLE stockky_kv")
        i_restore = max(i for i, s in enumerate(sqls) if "INSERT INTO stockky_kv" in s)
        assert i_snap < i_trunc < i_restore
        assert any("ADD CONSTRAINT uq_stockky_kv_k" in s for s in sqls)
        assert any("idx_stockky_kv_k" in s for s in sqls)
        assert any("stockky_kv_expires_idx" in s for s in sqls)
        # the whole thing ran in ONE transaction
        assert eng.opened == ["begin"]

        # snapshot query: prefix + cutoff ~ now - 3 days
        snap_params = eng.find("LIKE :prefix")[0][1]
        assert snap_params["prefix"] == kv_prefix() + "%"
        age = dt.datetime.now(UTC) - snap_params["cutoff"]
        assert dt.timedelta(days=3) - dt.timedelta(seconds=5) <= age <= dt.timedelta(days=3, seconds=5)

        # restored row: volatile/None fields dropped, marker fields added
        ins = eng.find("INSERT INTO stockky_kv")[-1][1]
        assert ins["k0"] == kv_prefix() + "TCS"
        assert json.loads(ins["v0"]) == {"pe": 25.1, "roce": 30, "sector": "IT",
                                         "symbol": "TCS", "_preserved_from_reset": True}
        assert "k1" not in ins
        exp_delta = ins["e0"] - dt.datetime.now(UTC)
        assert dt.timedelta(days=3) - dt.timedelta(seconds=5) <= exp_delta <= dt.timedelta(days=3)

        # memory: only protected settings keys survive
        assert set(kv._mem._store) == {"stockky:notification_config", "stockky:watchlist",
                                       "stockky:notification:extra"}

    def test_both_legacy_keys_migrate(self, kv, reset_env):
        legacy = {"stockky:notification_config": "N", "stockky:watchlist": '["A"]'}
        eng = install(kv, FakeEngine(_reset_responder(legacy=legacy)))
        kv.hard_reset_stockky_kv()
        assert eng.find("INSERT INTO stockky_notification")[0][1] == {"k": "config", "v": "N"}
        wl = eng.find("INSERT INTO stockky_watchlist")
        assert wl[0][1] == {"k": "default", "v": '["A"]'}
        assert "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v" in wl[0][0]

    def test_empty_legacy_value_is_not_migrated(self, kv, reset_env):
        eng = install(kv, FakeEngine(_reset_responder(legacy={"stockky:watchlist": ""})))
        kv.hard_reset_stockky_kv()
        assert not eng.find("INSERT INTO stockky_watchlist")

    def test_no_snapshot_rows_skips_restore(self, kv, reset_env):
        eng = install(kv, FakeEngine(_reset_responder()))
        out = kv.hard_reset_stockky_kv()
        assert out["preserved_symbol_fields_count"] == 0
        assert not eng.find("INSERT INTO stockky_kv")

    def test_restore_chunks_of_200(self, kv, reset_env):
        snapshot = [_sym(f"S{i}", pe=i) for i in range(250)]
        eng = install(kv, FakeEngine(_reset_responder(snapshot=snapshot)))
        out = kv.hard_reset_stockky_kv()
        assert out["preserved_symbol_fields_count"] == 250
        inserts = eng.find("INSERT INTO stockky_kv")
        assert [len(c[1]) // 3 for c in inserts] == [200, 50]

    def test_legacy_migration_error_is_isolated(self, kv, reset_env):
        eng = install(kv, FakeEngine(_reset_responder(raise_on="SELECT v FROM stockky_kv")))
        out = kv.hard_reset_stockky_kv()
        assert out["status"] == "success"
        assert eng.find("TRUNCATE")

    def test_snapshot_error_continues_without_preserve(self, kv, reset_env):
        eng = install(kv, FakeEngine(_reset_responder(raise_on="LIKE :prefix")))
        out = kv.hard_reset_stockky_kv()
        assert out["status"] == "success" and out["preserved_symbol_fields_count"] == 0
        assert eng.find("TRUNCATE")

    def test_constraint_error_is_swallowed_and_indexes_still_run(self, kv, reset_env):
        eng = install(kv, FakeEngine(_reset_responder(raise_on="ALTER TABLE")))
        out = kv.hard_reset_stockky_kv()
        assert out["status"] == "success"
        assert any("stockky_kv_expires_idx" in s for s in eng.sqls())

    def test_restore_error_reports_zero_preserved(self, kv, reset_env):
        snapshot = [_sym("TCS", pe=1)]

        def responder(sql, params):
            if "INSERT INTO stockky_kv" in sql:
                raise RuntimeError("restore failed")
            return _reset_responder(snapshot=snapshot)(sql, params)

        install(kv, FakeEngine(responder))
        out = kv.hard_reset_stockky_kv()
        assert out["status"] == "success" and out["preserved_symbol_fields_count"] == 0

    def test_truncate_failure_returns_error(self, kv, reset_env):
        install(kv, FakeEngine(_reset_responder(raise_on="TRUNCATE")))
        out = kv.hard_reset_stockky_kv()
        assert out["status"] == "error" and out["mode"] == "neon"
        assert "TRUNCATE" in out["message"]

    def test_error_message_truncated_to_240(self, kv, reset_env):
        def boom(sql, params):
            raise RuntimeError("y" * 500)

        # legacy/snapshot errors are isolated, so fail the schema step instead
        install(kv, FakeEngine(boom))
        kv._init_durable_schema = lambda eng: (_ for _ in ()).throw(RuntimeError("y" * 500))
        out = kv.hard_reset_stockky_kv()
        assert out["status"] == "error" and out["message"] == "y" * 240

    def test_memory_clear_failure_is_swallowed(self, kv, reset_env):
        install(kv, FakeEngine(_reset_responder()))

        class BadLock:
            def __enter__(self):
                raise RuntimeError("lock broken")

            def __exit__(self, *a):
                return False

        kv._mem._lock = BadLock()
        assert kv.hard_reset_stockky_kv()["status"] == "success"


class TestHardResetOracle:
    def test_oracle_flow(self, kv, oc, reset_env):
        snapshot = [_sym("TCS", pe=2), _sym("INFY", roce=3)]
        legacy = {"stockky:watchlist": '["A"]'}
        eng = install(kv, FakeEngine(_reset_responder(legacy=legacy, snapshot=snapshot)), dialect="oracle")
        out = kv.hard_reset_stockky_kv(preserve_days=1)
        assert out["status"] == "success" and out["preserved_symbol_fields_count"] == 2
        sqls = eng.sqls()
        # legacy migrated through the oracle MERGE for the settings table
        assert "UPSERT[oracle:stockky_watchlist:False]" in sqls
        assert "TRUNCATE TABLE stockky_kv" in sqls
        # Postgres-only constraint / index statements are skipped
        assert not any("ALTER TABLE" in s or "CREATE INDEX" in s for s in sqls)
        # one MERGE per restored row
        merges = eng.find("UPSERT[oracle:stockky_kv:True]")
        assert [m[1]["k"] for m in merges] == [kv_prefix() + "TCS", kv_prefix() + "INFY"]
        assert json.loads(merges[0][1]["v"])["_preserved_from_reset"] is True

    def test_oracle_notification_migration_uses_merge(self, kv, reset_env):
        legacy = {"stockky:notification_config": "N"}
        eng = install(kv, FakeEngine(_reset_responder(legacy=legacy)), dialect="oracle")
        kv.hard_reset_stockky_kv()
        assert eng.find("UPSERT[oracle:stockky_notification:False]")[0][1] == {"k": "config", "v": "N"}


class TestHardResetSchemaStep:
    def test_real_schema_init_is_called_with_engine(self, kv, monkeypatch):
        """Un-patched: _init_durable_schema really runs (5 DDL statements) before the txn."""
        eng = install(kv, FakeEngine(_reset_responder()))
        kv.hard_reset_stockky_kv()
        ddl = [s for s in eng.sqls() if "CREATE TABLE IF NOT EXISTS" in s]
        assert len(ddl) == 3


# ── settings tables ───────────────────────────────────────────────────────────

class TestSettingsTableGuard:
    def test_allowed(self, kv):
        assert kv._settings_table_ok("stockky_notification") == "stockky_notification"
        assert kv._settings_table_ok("stockky_watchlist") == "stockky_watchlist"

    @pytest.mark.parametrize("bad", ["stockky_kv", "users; DROP TABLE x", "", "STOCKKY_WATCHLIST"])
    def test_rejected(self, kv, bad):
        with pytest.raises(ValueError, match="settings table not allowed"):
            kv._settings_table_ok(bad)

    def test_all_public_settings_functions_enforce_it(self, kv):
        for fn, args in ((kv.settings_get, ("evil",)), (kv.settings_set, ("evil", "k", 1)),
                         (kv.settings_delete, ("evil",))):
            with pytest.raises(ValueError):
                fn(*args)


class TestSettingsGet:
    def test_memory_hit(self, kv):
        kv._SETTINGS_MEM["stockky_watchlist:default"] = ["A"]
        assert kv.settings_get("stockky_watchlist") == ["A"]

    def test_cached_none_still_counts_as_a_hit(self, kv):
        kv._SETTINGS_MEM["stockky_watchlist:default"] = None
        eng = install(kv, FakeEngine())
        assert kv.settings_get("stockky_watchlist") is None
        assert eng.calls == []

    def test_no_engine(self, kv):
        assert kv.settings_get("stockky_watchlist") is None

    def test_row_missing(self, kv):
        eng = install(kv, FakeEngine())
        assert kv.settings_get("stockky_notification", "config") is None
        assert eng.calls[0][0].startswith("SELECT v FROM stockky_notification")
        assert eng.calls[0][1] == {"k": "config"}

    def test_json_row_is_parsed_and_cached(self, kv):
        eng = install(kv, FakeEngine(lambda s, p: FakeResult([('{"a": 1}',)])))
        assert kv.settings_get("stockky_notification", "config") == {"a": 1}
        assert kv._SETTINGS_MEM["stockky_notification:config"] == {"a": 1}
        eng.responder = lambda s, p: (_ for _ in ()).throw(AssertionError("second read hit the DB"))
        assert kv.settings_get("stockky_notification", "config") == {"a": 1}

    def test_non_json_row_returned_raw(self, kv):
        install(kv, FakeEngine(lambda s, p: FakeResult([("plain text",)])))
        assert kv.settings_get("stockky_watchlist") == "plain text"

    def test_non_string_row_returned_as_is(self, kv):
        install(kv, FakeEngine(lambda s, p: FakeResult([({"already": "dict"},)])))
        assert kv.settings_get("stockky_watchlist") == {"already": "dict"}

    def test_legacy_verbatim_numeric_row_still_decodes_as_number(self, kv):
        """Rows written BEFORE settings_set started JSON-encoding strings were stored
        verbatim; a legacy "123" still reads back as 123 until it is next rewritten.
        New writes round-trip exactly (see TestSettingsSet)."""
        install(kv, FakeEngine(lambda s, p: FakeResult([("123",)])))
        assert kv.settings_get("stockky_watchlist") == 123

    def test_legacy_verbatim_plain_string_row_is_returned_as_is(self, kv):
        install(kv, FakeEngine(lambda s, p: FakeResult([("hello",)])))
        assert kv.settings_get("stockky_watchlist") == "hello"

    def test_db_error_returns_none(self, kv, monkeypatch):
        spy = LogSpy()
        monkeypatch.setattr(kv, "logger", spy)

        def boom(sql, params):
            raise RuntimeError("db down")

        install(kv, FakeEngine(boom))
        assert kv.settings_get("stockky_watchlist") is None
        assert "warning" in spy.levels()


class TestSettingsSet:
    def test_memory_only_when_no_engine(self, kv, monkeypatch):
        spy = LogSpy()
        monkeypatch.setattr(kv, "logger", spy)
        assert kv.settings_set("stockky_watchlist", "default", ["A", "B"]) is True
        assert kv._SETTINGS_MEM["stockky_watchlist:default"] == ["A", "B"]
        assert "memory only" in spy.text()

    def test_postgres_creates_table_then_upserts(self, kv):
        eng = install(kv, FakeEngine())
        assert kv.settings_set("stockky_notification", "config", {"tg": "x"}) is True
        assert eng.opened == ["begin"]
        assert "CREATE TABLE IF NOT EXISTS stockky_notification" in eng.calls[0][0]
        sql, params = eng.calls[1]
        assert "INSERT INTO stockky_notification" in sql and "ON CONFLICT (k) DO UPDATE" in sql
        assert params == {"k": "config", "v": json.dumps({"tg": "x"})}

    def test_string_value_is_json_encoded_so_it_round_trips(self, kv):
        eng = install(kv, FakeEngine())
        kv.settings_set("stockky_watchlist", "default", "hello")
        assert eng.calls[1][1]["v"] == json.dumps("hello")
        assert kv._SETTINGS_MEM["stockky_watchlist:default"] == "hello"

    @pytest.mark.parametrize("value", ["123", "true", "null", "1.5", '{"a": 1}', "[1]", "hello", ""])
    def test_string_survives_a_db_round_trip_as_a_string(self, kv, value):
        """Fixed: settings_get json-decodes, so a verbatim "123" used to come back as 123
        (and "null" as None) after a restart, while memory still returned the str."""
        eng = install(kv, FakeEngine())
        kv.settings_set("stockky_watchlist", "default", value)
        stored = eng.calls[1][1]["v"]
        kv._SETTINGS_MEM.clear()                                   # simulate a restart
        install(kv, FakeEngine(lambda s, p: FakeResult([(stored,)])))
        assert kv.settings_get("stockky_watchlist") == value

    def test_non_dict_non_str_value_kept_as_is_in_memory(self, kv):
        install(kv, FakeEngine())
        kv.settings_set("stockky_watchlist", "default", 42)
        assert kv._SETTINGS_MEM["stockky_watchlist:default"] == 42

    def test_non_serialisable_value_uses_default_str(self, kv):
        eng = install(kv, FakeEngine())
        kv.settings_set("stockky_watchlist", "default", {"d": dt.date(2026, 1, 2)})
        assert json.loads(eng.calls[1][1]["v"]) == {"d": "2026-01-02"}
        assert kv._SETTINGS_MEM["stockky_watchlist:default"] == {"d": "2026-01-02"}

    def test_memory_copy_falls_back_to_original_when_reparse_fails(self, kv, monkeypatch):
        real = json

        def bad_loads(*a, **k):
            raise ValueError("cannot parse")

        monkeypatch.setattr(kv, "json", SimpleNamespace(dumps=real.dumps, loads=bad_loads))
        value = {"a": 1}
        assert kv.settings_set("stockky_watchlist", "default", value) is True
        assert kv._SETTINGS_MEM["stockky_watchlist:default"] is value

    def test_oracle_creates_table_via_shim_then_merges(self, kv, oc):
        eng = install(kv, FakeEngine(), dialect="oracle")
        assert kv.settings_set("stockky_notification", "config", [1]) is True
        assert oc.ddl == [("CT[oracle:stockky_notification:False]", "oracle")]
        assert eng.calls == [("UPSERT[oracle:stockky_notification:False]", {"k": "config", "v": "[1]"})]

    def test_oracle_dialect_without_shim_uses_postgres_sql(self, kv, monkeypatch):
        eng = install(kv, FakeEngine(), dialect="oracle")
        monkeypatch.setattr(kv, "_oc", None)
        kv.settings_set("stockky_watchlist", "default", [1])
        assert "CREATE TABLE IF NOT EXISTS stockky_watchlist" in eng.calls[0][0]

    def test_db_error_returns_false_but_memory_is_kept(self, kv, monkeypatch):
        spy = LogSpy()
        monkeypatch.setattr(kv, "logger", spy)

        def boom(sql, params):
            raise RuntimeError("db down")

        install(kv, FakeEngine(boom))
        assert kv.settings_set("stockky_watchlist", "default", ["A"]) is False
        assert kv._SETTINGS_MEM["stockky_watchlist:default"] == ["A"]
        assert "error" in spy.levels()


class TestSettingsDelete:
    def test_no_engine(self, kv):
        kv._SETTINGS_MEM["stockky_watchlist:default"] = ["A"]
        assert kv.settings_delete("stockky_watchlist") is True
        assert "stockky_watchlist:default" not in kv._SETTINGS_MEM

    def test_missing_memory_key_is_fine(self, kv):
        assert kv.settings_delete("stockky_watchlist", "nothing") is True

    def test_db_delete(self, kv):
        eng = install(kv, FakeEngine())
        assert kv.settings_delete("stockky_notification", "config") is True
        assert eng.calls == [("DELETE FROM stockky_notification WHERE k = :k", {"k": "config"})]

    def test_db_error_returns_false(self, kv):
        def boom(sql, params):
            raise RuntimeError("db down")

        install(kv, FakeEngine(boom))
        assert kv.settings_delete("stockky_watchlist") is False


# ── notification_config_* / watchlist_* ───────────────────────────────────────

class TestNotificationConfig:
    def test_get_prefers_settings_table(self, kv, monkeypatch):
        monkeypatch.setattr(kv, "settings_get", lambda t, k="default": {"from": "settings", "t": t, "k": k})
        monkeypatch.setattr(kv, "kv_get", lambda k: (_ for _ in ()).throw(AssertionError("legacy read")))
        assert kv.notification_config_get() == {"from": "settings", "t": "stockky_notification", "k": "config"}

    def test_get_migrates_legacy_key_once(self, kv, monkeypatch):
        sets = []
        monkeypatch.setattr(kv, "settings_get", lambda t, k="default": None)
        monkeypatch.setattr(kv, "kv_get", lambda k: {"tg": 1} if k == "stockky:notification_config" else None)
        monkeypatch.setattr(kv, "settings_set", lambda t, k, v: sets.append((t, k, v)) or True)
        assert kv.notification_config_get() == {"tg": 1}
        assert sets == [("stockky_notification", "config", {"tg": 1})]

    def test_get_none_when_nowhere(self, kv):
        assert kv.notification_config_get() is None

    def test_set_get_delete_roundtrip(self, kv):
        assert kv.notification_config_set({"chat": 5}) is True
        assert kv.notification_config_get() == {"chat": 5}
        assert kv.kv_get("stockky:notification_config") == {"chat": 5}  # legacy mirror
        assert kv.notification_config_delete() is True
        assert kv.notification_config_get() is None
        assert kv.kv_get("stockky:notification_config") is None

    def test_set_returns_settings_result_even_if_mirror_fails(self, kv, monkeypatch):
        monkeypatch.setattr(kv, "settings_set", lambda t, k, v: False)

        def bad(*a, **k):
            raise RuntimeError("mirror down")

        monkeypatch.setattr(kv, "kv_set", bad)
        assert kv.notification_config_set({"a": 1}) is False

    def test_mirror_has_no_ttl(self, kv, monkeypatch):
        seen = []
        monkeypatch.setattr(kv, "kv_set", lambda k, v, ttl="unset": seen.append((k, v, ttl)))
        kv.notification_config_set({"a": 1})
        assert seen == [("stockky:notification_config", {"a": 1}, None)]

    def test_delete_survives_legacy_delete_failure(self, kv, monkeypatch):
        def bad(*a, **k):
            raise RuntimeError("kv down")

        monkeypatch.setattr(kv, "kv_delete", bad)
        monkeypatch.setattr(kv, "settings_delete", lambda t, k="default": (t, k) == ("stockky_notification", "config"))
        assert kv.notification_config_delete() is True


class TestWatchlist:
    def test_get_prefers_settings_table(self, kv, monkeypatch):
        monkeypatch.setattr(kv, "settings_get", lambda t, k="default": ["A"] if (t, k) == ("stockky_watchlist", "default") else None)
        assert kv.watchlist_get() == ["A"]

    def test_get_migrates_legacy_key(self, kv, monkeypatch):
        sets = []
        monkeypatch.setattr(kv, "settings_get", lambda t, k="default": None)
        monkeypatch.setattr(kv, "kv_get", lambda k: ["X"] if k == "stockky:watchlist" else None)
        monkeypatch.setattr(kv, "settings_set", lambda t, k, v: sets.append((t, k, v)) or True)
        assert kv.watchlist_get() == ["X"]
        assert sets == [("stockky_watchlist", "default", ["X"])]

    def test_get_none_when_nowhere(self, kv):
        assert kv.watchlist_get() is None

    def test_set_get_delete_roundtrip(self, kv):
        assert kv.watchlist_set(["A", "B"]) is True
        assert kv.watchlist_get() == ["A", "B"]
        assert kv.kv_get("stockky:watchlist") == ["A", "B"]
        assert kv.watchlist_delete() is True
        assert kv.watchlist_get() is None

    def test_set_survives_mirror_failure(self, kv, monkeypatch):
        def bad(*a, **k):
            raise RuntimeError("mirror down")

        monkeypatch.setattr(kv, "kv_set", bad)
        assert kv.watchlist_set(["A"]) is True

    def test_delete_survives_legacy_delete_failure(self, kv, monkeypatch):
        def bad(*a, **k):
            raise RuntimeError("kv down")

        monkeypatch.setattr(kv, "kv_delete", bad)
        assert kv.watchlist_delete() is True

    def test_hard_reset_does_not_touch_settings_tables(self, kv, monkeypatch):
        """End to end with a fake engine: settings survive, feed data is wiped."""
        monkeypatch.setattr(kv, "_init_durable_schema", lambda eng: None)
        eng = install(kv, FakeEngine(_reset_responder()))
        kv.watchlist_set(["A"])
        kv.hard_reset_stockky_kv()
        assert kv.watchlist_get() == ["A"]
        truncs = [s for s in eng.sqls() if s.startswith("TRUNCATE")]
        assert truncs == ["TRUNCATE TABLE stockky_kv"]


# ═════════════════════════ gateway-only additions ═════════════════════════
# Everything below exists only in api-gateway/kv_cache.py (not in the analysis-intelligence
# copy the tests above were ported from): the extra durable prefixes and the stale-read API.

# ── extra durable prefixes ────────────────────────────────────────────────────

class TestGatewayDurablePrefixes:
    @pytest.mark.parametrize("key", [
        "stockky:hot_premarket_job", "stockky:hot_premarket_job:abc",
        "stockky:ipo:list", "stockky:ipo:manual", "stockky:ipo:job",
        "stockky:ipoalerts:quota", "stockky:ipoalerts:resp:XYZ",
        "system:surprise_feed", "system:bulk_quote_cache", "stockky:hot_stocks",
        "stockky:surprise_scan:last_result", "stockky:surprise_scan:anything_else",
    ])
    def test_durable(self, kv, key):
        assert kv._is_durable(key) is True

    @pytest.mark.parametrize("key", [
        "stockky:ipo", "stockky:ipoalert", "stockky:surprise_scan", "system:other",
        "system:surprise", "stockky:hot", "cache:stockky:ipo:list",
    ])
    def test_near_misses_are_not_durable(self, kv, key):
        assert kv._is_durable(key) is False

    def test_ipo_key_write_reaches_engine(self, kv):
        eng = install(kv, FakeEngine())
        kv.kv_set("stockky:ipo:manual", [{"name": "X"}])
        assert eng.find("INSERT INTO stockky_kv")

    def test_surprise_feed_write_without_ttl_is_durable(self, kv, monkeypatch):
        spy = []
        monkeypatch.setattr(kv, "_neon_set", lambda k, v, ttl=None: spy.append((k, v, ttl)))
        kv.kv_set("system:surprise_feed", {"rows": []}, ttl=None)
        assert spy == [("system:surprise_feed", {"rows": []}, None)]


# ── kv_get_stale / get_stale ──────────────────────────────────────────────────

class _Lob:
    """Oracle LOB stand-in: not a str/bytes, has .read()."""
    def __init__(self, text, fail=False):
        self._t, self._fail = text, fail

    def read(self):
        if self._fail:
            raise RuntimeError("lob read failed")
        return self._t

    def __str__(self):
        return "LOB-STR"


class TestKvGetStale:
    def test_memory_hit_skips_db(self, kv):
        eng = install(kv, FakeEngine())
        kv._mem.set("k", {"fresh": 1})
        assert kv.kv_get_stale("k") == {"fresh": 1}
        assert eng.calls == []

    def test_memory_falsy_value_is_still_a_hit(self, kv):
        eng = install(kv, FakeEngine())
        kv._mem.set("k", 0)
        assert kv.kv_get_stale("k") == 0
        assert eng.calls == []

    def test_no_engine_returns_none(self, kv):
        assert kv.kv_get_stale("k") is None

    def test_missing_row_returns_none(self, kv):
        eng = install(kv, FakeEngine())
        assert kv.kv_get_stale("k") is None
        assert eng.calls[0][1] == {"k": "k"}
        assert kv._mem.get("k") is None

    def test_query_ignores_expiry(self, kv):
        eng = install(kv, FakeEngine())
        kv.kv_get_stale("k")
        sql = eng.calls[0][0]
        assert sql == "SELECT v FROM stockky_kv WHERE k = :k"
        assert "expires" not in sql.lower()

    def test_json_value_returned_and_memory_warmed_120s(self, kv, clock):
        install(kv, FakeEngine(lambda s, p: FakeResult([(json.dumps({"a": [1, 2]}),)])))
        assert kv.kv_get_stale("k") == {"a": [1, 2]}
        assert kv._mem._store["k"].expires_at == clock.now + 120
        clock.now += 60
        assert kv._mem.get("k") == {"a": [1, 2]}
        clock.now += 61
        assert kv._mem.get("k") is None

    def test_second_call_served_from_memory(self, kv):
        eng = install(kv, FakeEngine(lambda s, p: FakeResult([("7",)])))
        assert kv.kv_get_stale("k") == 7
        assert kv.kv_get_stale("k") == 7
        assert len(eng.calls) == 1

    def test_non_json_value_returned_raw(self, kv):
        install(kv, FakeEngine(lambda s, p: FakeResult([("not json {",)])))
        assert kv.kv_get_stale("k") == "not json {"

    def test_bytes_value_is_decoded_as_json(self, kv):
        install(kv, FakeEngine(lambda s, p: FakeResult([(b'{"b": 2}',)])))
        assert kv.kv_get_stale("k") == {"b": 2}

    def test_lob_value_is_read(self, kv):
        install(kv, FakeEngine(lambda s, p: FakeResult([(_Lob('{"c": 3}'),)])))
        assert kv.kv_get_stale("k") == {"c": 3}

    def test_lob_read_failure_falls_back_to_str(self, kv):
        install(kv, FakeEngine(lambda s, p: FakeResult([(_Lob("x", fail=True),)])))
        assert kv.kv_get_stale("k") == "LOB-STR"

    def test_null_column_returns_none(self, kv):
        install(kv, FakeEngine(lambda s, p: FakeResult([(None,)])))
        assert kv.kv_get_stale("k") is None

    def test_db_error_returns_none_and_logs_debug(self, kv, monkeypatch):
        def boom(sql, params):
            raise RuntimeError("db down")

        install(kv, FakeEngine(boom))
        spy = LogSpy()
        monkeypatch.setattr(kv, "logger", spy)
        assert kv.kv_get_stale("k") is None
        assert spy.levels() == ["debug"]
        assert "db get_stale k: db down" in spy.text()

    def test_oracle_dialect_uses_same_query(self, kv):
        eng = install(kv, FakeEngine(lambda s, p: FakeResult([('"v"',)])), dialect="oracle")
        assert kv.kv_get_stale("k") == "v"
        assert eng.sqls() == ["SELECT v FROM stockky_kv WHERE k = :k"]


class TestGetStaleWrapper:
    def test_delegates_to_kv_get_stale(self, kv, monkeypatch):
        seen = []
        monkeypatch.setattr(kv, "kv_get_stale", lambda key: seen.append(key) or "STALE")
        assert kv.get_stale("some:key") == "STALE"
        assert seen == ["some:key"]

    def test_end_to_end_through_module_api(self, kv):
        install(kv, FakeEngine(lambda s, p: FakeResult([('[1, 2]',)])))
        assert kv.get_stale("k") == [1, 2]


# ── _capped_mem_ttl ───────────────────────────────────────────────────────────

class TestCappedMemTtl:
    @pytest.mark.parametrize("remaining, expected", [
        (None, 600), (5, 5), (4.2, 5), (0.2, 1), (0, 1), (-3, 1), (599.5, 600), (7200, 600),
    ])
    def test_values(self, kv, remaining, expected):
        assert kv._capped_mem_ttl(remaining) == expected

    @pytest.mark.parametrize("bad", ["soon", object(), float("nan"), float("inf")])
    def test_unusable_remaining_falls_back_to_default(self, kv, bad):
        assert kv._capped_mem_ttl(bad) == 600

    def test_custom_default(self, kv):
        assert kv._capped_mem_ttl(None, 120) == 120 and kv._capped_mem_ttl(500, 120) == 120
