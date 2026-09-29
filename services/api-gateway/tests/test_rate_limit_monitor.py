"""
tests/test_rate_limit_monitor.py — api-gateway/rate_limit_monitor.py (100%).

The monitor keeps a rolling in-memory deque of 429/503/... events, mirrors them to Redis
(optional) and to the durable kv_cache ("Neon") on a background thread pool, and builds the
GET /ops/rate-limits dashboard payload.

Hermetic: no network, no database, no Redis, no real threads.
  * import      -> the module builds a singleton `monitor` at import time, which calls kv_get().
                   It is imported once with a fake `kv_cache` in sys.modules and the DB/Redis env
                   removed, so collecting this file can never reach a real database.
  * kv_cache    -> fake module per test (`kv` fixture) recording every kv_get / kv_set
  * upstash     -> fake `upstash_redis` module / fake Redis client objects
  * thread pool -> `rlm._io_pool` replaced by a synchronous fake (submit runs inline)
  * time        -> FakeClock injected as `rlm.time`

    cd services/api-gateway
    python -m pytest tests/test_rate_limit_monitor.py -v --cov=rate_limit_monitor --cov-report=term-missing
"""
from __future__ import annotations

import json
import logging
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

# ── import the module hermetically (its import-time singleton calls kv_get) ─────────────────
_ENV_KEYS = (
    "USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH", "UPSTASH_REDIS_REST_URL",
    "UPSTASH_REDIS_REST_TOKEN", "DATABASE_URL", "ORACLE_DSN", "ORACLE_USER", "ORACLE_PASSWORD",
)
_saved_env = {k: os.environ.pop(k, None) for k in _ENV_KEYS}
_prev_kv = sys.modules.get("kv_cache")
_import_kv = types.ModuleType("kv_cache")
_import_kv.kv_get = lambda key: None
_import_kv.kv_set = lambda key, value, ttl=None: None
sys.modules["kv_cache"] = _import_kv
try:
    import rate_limit_monitor as rlm
finally:
    if _prev_kv is None:
        sys.modules.pop("kv_cache", None)
    else:
        sys.modules["kv_cache"] = _prev_kv
    for _k, _v in _saved_env.items():
        if _v is not None:
            os.environ[_k] = _v


# ─────────────────────────────── fakes ───────────────────────────────

class FakeClock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def time(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(rlm, "time", types.SimpleNamespace(time=c.time))
    return c


class FakePool:
    """Synchronous stand-in for the ThreadPoolExecutor."""

    def __init__(self, raises=None):
        self.submitted = []
        self.raises = raises

    def submit(self, fn, *args, **kwargs):
        if self.raises is not None:
            raise self.raises
        self.submitted.append((fn, args, kwargs))
        fn(*args, **kwargs)


@pytest.fixture
def pool(monkeypatch):
    p = FakePool()
    monkeypatch.setattr(rlm, "_io_pool", p)
    return p


class FakeKV:
    def __init__(self):
        self.store = {}
        self.gets = []
        self.sets = []
        self.get_raises = None
        self.set_raises = None

    def kv_get(self, key):
        self.gets.append(key)
        if self.get_raises:
            raise self.get_raises
        return self.store.get(key)

    def kv_set(self, key, value, ttl=None):
        self.sets.append((key, value, ttl))
        if self.set_raises:
            raise self.set_raises
        self.store[key] = value


@pytest.fixture
def kv(monkeypatch):
    fk = FakeKV()
    mod = types.ModuleType("kv_cache")
    mod.kv_get = fk.kv_get
    mod.kv_set = fk.kv_set
    monkeypatch.setitem(sys.modules, "kv_cache", mod)
    return fk


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


class FakeRedis:
    def __init__(self, lrange_result=None, fail=None):
        self.calls = []
        self.lrange_result = lrange_result
        self.fail = fail  # name of the method that should raise

    def _do(self, name, *args):
        self.calls.append((name, args))
        if self.fail == name:
            raise ConnectionError(f"{name} failed")

    def lpush(self, *a):
        self._do("lpush", *a)

    def ltrim(self, *a):
        self._do("ltrim", *a)

    def expire(self, *a):
        self._do("expire", *a)

    def lrange(self, *a):
        self._do("lrange", *a)
        return self.lrange_result


def new_monitor(kv_fixture=None):
    return rlm.RateLimitMonitor()


def ev(ts, source="market_data", status=429, **extra):
    d = {"ts": ts, "source": source, "status": status, "path": "", "detail": "", "symbol": ""}
    d.update(extra)
    return d


# ─────────────────────────────── module constants / singleton ───────────────────────────────

class TestModule:
    def test_singleton_built_at_import_is_memory_only(self):
        assert isinstance(rlm.monitor, rlm.RateLimitMonitor)
        assert rlm.monitor._redis is None

    def test_upstream_ids_unique_and_have_codes(self):
        ids = [u["id"] for u in rlm.UPSTREAMS]
        assert len(ids) == len(set(ids))
        assert all(u["codes"] and u["label"] for u in rlm.UPSTREAMS)

    def test_io_pool_is_a_bounded_executor(self):
        assert rlm._io_pool._max_workers == 2


# ─────────────────────────────── _init_redis ───────────────────────────────

@pytest.fixture
def fake_upstash(monkeypatch):
    created = {}

    class _Redis:
        def __init__(self, url, token):
            created["url"] = url
            created["token"] = token
            created["ping_called"] = False
            created["instance"] = self
            if created.get("ctor_raises"):
                raise created["ctor_raises"]

        def ping(self):
            created["ping_called"] = True
            if created.get("ping_raises"):
                raise created["ping_raises"]
            return True

    mod = types.ModuleType("upstash_redis")
    mod.Redis = _Redis
    monkeypatch.setitem(sys.modules, "upstash_redis", mod)
    return created


class TestInitRedis:
    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "Yes"])
    def test_disable_redis_wins(self, monkeypatch, kv, fake_upstash, val):
        monkeypatch.setenv("USE_REDIS", "1")
        monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://x")
        monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "t")
        monkeypatch.setenv("DISABLE_REDIS", val)
        m = new_monitor()
        assert m._redis is None
        assert "url" not in fake_upstash

    def test_disable_redis_zero_does_not_disable(self, monkeypatch, kv, fake_upstash):
        monkeypatch.setenv("USE_REDIS", "1")
        monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://x")
        monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "t")
        monkeypatch.setenv("DISABLE_REDIS", "0")
        assert new_monitor()._redis is not None

    def test_disable_upstash(self, monkeypatch, kv, fake_upstash, caplog):
        monkeypatch.setenv("USE_REDIS", "1")
        monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://x")
        monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "t")
        monkeypatch.setenv("DISABLE_UPSTASH", "true")
        with caplog.at_level(logging.INFO, logger="rate-limit-monitor"):
            m = new_monitor()
        assert m._redis is None
        assert "url" not in fake_upstash
        assert any("DISABLE_UPSTASH" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("val", [None, "0", "false", "no", ""])
    def test_use_redis_off_by_default(self, monkeypatch, kv, fake_upstash, val):
        if val is not None:
            monkeypatch.setenv("USE_REDIS", val)
        monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://x")
        monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "t")
        assert new_monitor()._redis is None
        assert "url" not in fake_upstash

    @pytest.mark.parametrize("url,token", [(None, None), ("https://x", None), (None, "t"), ("", "t")])
    def test_missing_credentials_stay_memory_only(self, monkeypatch, kv, fake_upstash, url, token):
        monkeypatch.setenv("USE_REDIS", "1")
        if url is not None:
            monkeypatch.setenv("UPSTASH_REDIS_REST_URL", url)
        if token is not None:
            monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", token)
        assert new_monitor()._redis is None
        assert "url" not in fake_upstash

    def test_connects_and_pings(self, monkeypatch, kv, fake_upstash, caplog):
        monkeypatch.setenv("USE_REDIS", "yes")
        monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://x.upstash.io")
        monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "tok")
        with caplog.at_level(logging.INFO, logger="rate-limit-monitor"):
            m = new_monitor()
        assert m._redis is fake_upstash["instance"]
        assert fake_upstash["url"] == "https://x.upstash.io"
        assert fake_upstash["token"] == "tok"
        assert fake_upstash["ping_called"] is True
        assert any("using Redis" in r.getMessage() for r in caplog.records)

    def test_ping_failure_falls_back_to_memory(self, monkeypatch, kv, fake_upstash, caplog):
        monkeypatch.setenv("USE_REDIS", "1")
        monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://x")
        monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "t")
        fake_upstash["ping_raises"] = ConnectionError("no route")
        with caplog.at_level(logging.WARNING, logger="rate-limit-monitor"):
            m = new_monitor()
        assert m._redis is None
        assert any("Redis unavailable" in r.getMessage() for r in caplog.records)

    def test_constructor_failure_falls_back_to_memory(self, monkeypatch, kv, fake_upstash):
        monkeypatch.setenv("USE_REDIS", "1")
        monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://x")
        monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "t")
        fake_upstash["ctor_raises"] = ValueError("bad url")
        assert new_monitor()._redis is None

    def test_missing_package_falls_back_to_memory(self, monkeypatch, kv):
        monkeypatch.setenv("USE_REDIS", "1")
        monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://x")
        monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "t")
        monkeypatch.setitem(sys.modules, "upstash_redis", None)  # import raises ImportError
        assert new_monitor()._redis is None


# ─────────────────────────────── _kv_get / _kv_set ───────────────────────────────

class TestKvHelpers:
    def test_kv_get_returns_value(self, kv):
        m = new_monitor()
        kv.store["k"] = {"a": 1}
        assert m._kv_get("k") == {"a": 1}

    def test_kv_get_swallows_errors(self, kv, caplog):
        m = new_monitor()
        kv.get_raises = RuntimeError("db down")
        with caplog.at_level(logging.DEBUG, logger="rate-limit-monitor"):
            assert m._kv_get("k") is None
        assert any("kv_get k" in r.getMessage() for r in caplog.records)

    def test_kv_get_swallows_import_errors(self, kv, monkeypatch):
        m = new_monitor()
        monkeypatch.setitem(sys.modules, "kv_cache", None)  # `from kv_cache import ...` -> ImportError
        assert m._kv_get("k") is None

    def test_kv_set_swallows_import_errors(self, kv, monkeypatch):
        m = new_monitor()
        monkeypatch.setitem(sys.modules, "kv_cache", None)
        m._kv_set("k", 1)  # must not raise

    def test_kv_set_passes_default_ttl(self, kv):
        m = new_monitor()
        kv.sets.clear()
        m._kv_set("k", {"v": 1})
        assert kv.sets == [("k", {"v": 1}, rlm.NEON_TTL)]

    def test_kv_set_passes_custom_ttl(self, kv):
        m = new_monitor()
        kv.sets.clear()
        m._kv_set("k", 1, ttl=5)
        assert kv.sets == [("k", 1, 5)]

    def test_kv_set_swallows_errors(self, kv, caplog):
        m = new_monitor()
        kv.set_raises = RuntimeError("db down")
        with caplog.at_level(logging.DEBUG, logger="rate-limit-monitor"):
            m._kv_set("k", 1)
        assert any("kv_set k" in r.getMessage() for r in caplog.records)


# ─────────────────────────────── _load_neon_events ───────────────────────────────

class TestLoadNeonEvents:
    def test_init_reads_events_key(self, kv):
        new_monitor()
        assert kv.gets == [rlm.NEON_EVENTS_KEY]

    def test_nothing_stored(self, kv):
        m = new_monitor()
        assert list(m._events) == []
        assert m._neon_backed is False

    def test_list_hydrates_and_preserves_newest_first_order(self, kv, caplog):
        newest, mid, oldest = ev(300), ev(200), ev(100)
        kv.store[rlm.NEON_EVENTS_KEY] = [newest, mid, oldest]
        with caplog.at_level(logging.INFO, logger="rate-limit-monitor"):
            m = new_monitor()
        assert list(m._events) == [newest, mid, oldest]
        assert m._neon_backed is True
        assert any("hydrated 3 events" in r.getMessage() for r in caplog.records)

    def test_list_skips_invalid_items(self, kv):
        good = ev(100)
        kv.store[rlm.NEON_EVENTS_KEY] = [good, "junk", 7, None, {"source": "x"}, {"ts": 0}]
        m = new_monitor()
        assert list(m._events) == [good]

    def test_list_respects_max_events(self, kv, monkeypatch):
        monkeypatch.setattr(rlm, "MAX_EVENTS", 3)
        kv.store[rlm.NEON_EVENTS_KEY] = [ev(i) for i in (6, 5, 4, 3, 2, 1)]
        m = new_monitor()
        assert len(m._events) <= 3

    def test_dict_form_with_events_list(self, kv):
        e1, e2 = ev(200), ev(100)
        kv.store[rlm.NEON_EVENTS_KEY] = {"events": [e1, e2, "bad"]}
        m = new_monitor()
        assert list(m._events) == [e1, e2]
        assert m._neon_backed is True

    def test_dict_form_with_empty_events_still_marks_backed(self, kv):
        kv.store[rlm.NEON_EVENTS_KEY] = {"events": []}
        m = new_monitor()
        assert list(m._events) == []
        assert m._neon_backed is True

    def test_dict_without_events_list_is_ignored(self, kv):
        kv.store[rlm.NEON_EVENTS_KEY] = {"events": "nope"}
        m = new_monitor()
        assert list(m._events) == []
        assert m._neon_backed is False

    def test_empty_list_is_ignored(self, kv):
        kv.store[rlm.NEON_EVENTS_KEY] = []
        m = new_monitor()
        assert m._neon_backed is False

    def test_unexpected_type_is_ignored(self, kv):
        kv.store[rlm.NEON_EVENTS_KEY] = "garbage"
        m = new_monitor()
        assert list(m._events) == [] and m._neon_backed is False

    def test_hydration_error_is_swallowed(self, kv, monkeypatch, caplog):
        def boom(self, key):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(rlm.RateLimitMonitor, "_kv_get", boom)
        with caplog.at_level(logging.DEBUG, logger="rate-limit-monitor"):
            m = new_monitor()
        assert list(m._events) == []
        assert any("neon hydrate failed" in r.getMessage() for r in caplog.records)

    def test_kv_failure_at_startup_is_harmless(self, kv):
        kv.get_raises = RuntimeError("db down")
        m = new_monitor()
        assert list(m._events) == []


# ─────────────────────────────── _persist_neon ───────────────────────────────

class TestPersistNeon:
    def test_writes_events_and_window_aggregate(self, kv, clock):
        m = new_monitor()
        now = clock.now
        m._events.extend([
            ev(now - 10, source="gemini"),
            ev(now - 20, source="gemini"),
            ev(now - 30, source="nse"),
            ev(now - rlm.WINDOW_SEC - 1, source="nse"),   # outside window
            ev(0, source="nse"),                          # no usable ts
            {"ts": now - 5, "status": 429},               # no source -> "unknown"
            {"ts": now - 5, "source": "", "status": 503}, # empty source -> "unknown"
        ])
        kv.sets.clear()
        m._persist_neon()
        (k1, v1, t1), (k2, v2, t2) = kv.sets
        assert k1 == rlm.NEON_EVENTS_KEY and t1 == rlm.NEON_TTL
        assert v1 == list(m._events)
        assert k2 == rlm.NEON_STATS_KEY and t2 == rlm.NEON_TTL
        assert v2["by_source_1h"] == {"gemini": 2, "nse": 1, "unknown": 2}
        assert v2["events_1h"] == 5
        assert v2["window_sec"] == rlm.WINDOW_SEC
        assert v2["updated_at"] == now
        assert v2["limits"]["gemini"] == {"limit": 60}
        assert set(v2["limits"]) == {"market_data", "indianapi", "gemini", "groq", "nse"}
        assert m._neon_backed is True

    def test_window_boundary_is_inclusive_of_cutoff(self, kv, clock):
        m = new_monitor()
        m._events.append(ev(clock.now - rlm.WINDOW_SEC, source="nse"))  # ts == cutoff: kept
        kv.sets.clear()
        m._persist_neon()
        assert kv.sets[1][1]["by_source_1h"] == {"nse": 1}

    def test_empty_events(self, kv, clock):
        m = new_monitor()
        kv.sets.clear()
        m._persist_neon()
        assert kv.sets[0][1] == []
        assert kv.sets[1][1]["by_source_1h"] == {}
        assert kv.sets[1][1]["events_1h"] == 0

    def test_caps_events_at_max(self, kv, clock, monkeypatch):
        monkeypatch.setattr(rlm, "MAX_EVENTS", 2)
        m = new_monitor()
        m._events.extend([ev(clock.now - i) for i in range(5)])
        kv.sets.clear()
        m._persist_neon()
        assert len(kv.sets[0][1]) == 2

    def test_failure_is_swallowed_and_flag_untouched(self, kv, monkeypatch, caplog):
        m = new_monitor()

        def boom(self, key, value, ttl=rlm.NEON_TTL):
            raise RuntimeError("write failed")

        monkeypatch.setattr(rlm.RateLimitMonitor, "_kv_set", boom)
        with caplog.at_level(logging.DEBUG, logger="rate-limit-monitor"):
            m._persist_neon()
        assert m._neon_backed is False
        assert any("neon persist failed" in r.getMessage() for r in caplog.records)

    def test_bad_ts_value_is_swallowed(self, kv, clock):
        m = new_monitor()
        m._events.append({"ts": "not-a-number", "source": "nse"})
        kv.sets.clear()
        m._persist_neon()  # float("not-a-number") raises -> caught, nothing written after events
        assert m._neon_backed is False
        assert [k for k, _, _ in kv.sets] == [rlm.NEON_EVENTS_KEY]


# ─────────────────────────────── record ───────────────────────────────

class TestRecord:
    def test_builds_normalised_event(self, kv, pool, clock):
        m = new_monitor()
        m.record("Market_Data", "429", path="/quote/X", detail="slow down", symbol="RELIANCE.NS")
        e = m._events[0]
        assert e == {
            "ts": clock.now,
            "source": "market_data",
            "status": 429,
            "path": "/quote/X",
            "detail": "slow down",
            "symbol": "RELIANCE.NS",
        }

    def test_status_is_coerced_to_int(self, kv, pool):
        m = new_monitor()
        m.record("nse", 503.0)
        assert m._events[0]["status"] == 503 and isinstance(m._events[0]["status"], int)

    def test_blank_and_none_fields(self, kv, pool):
        m = new_monitor()
        m.record("", 429, path=None, detail=None, symbol=None)
        e = m._events[0]
        assert e["source"] == "unknown"
        assert e["path"] == e["detail"] == e["symbol"] == ""

    def test_none_source(self, kv, pool):
        m = new_monitor()
        m.record(None, 429)
        assert m._events[0]["source"] == "unknown"

    def test_fields_are_truncated(self, kv, pool):
        m = new_monitor()
        m.record("S" * 100, 429, path="p" * 500, detail="d" * 500, symbol="y" * 100)
        e = m._events[0]
        assert e["source"] == "s" * 40
        assert len(e["path"]) == 120
        assert len(e["detail"]) == 200
        assert len(e["symbol"]) == 32

    def test_invalid_status_raises_and_records_nothing(self, kv, pool):
        m = new_monitor()
        with pytest.raises(ValueError):
            m.record("nse", "abc")
        assert len(m._events) == 0 and pool.submitted == []

    def test_newest_first(self, kv, pool, clock):
        m = new_monitor()
        m.record("a", 429)
        clock.now += 1
        m.record("b", 429)
        assert [e["source"] for e in m._events] == ["b", "a"]

    def test_deque_is_bounded(self, kv, pool):
        m = new_monitor()
        for i in range(rlm.MAX_EVENTS + 25):
            m.record("nse", 429, detail=str(i))
        assert len(m._events) == rlm.MAX_EVENTS
        assert m._events[0]["detail"] == str(rlm.MAX_EVENTS + 24)

    def test_persistence_is_handed_to_the_pool_not_run_inline(self, kv, monkeypatch, clock):
        m = new_monitor()
        deferred = FakePool()
        deferred.submit = lambda fn, *a, **k: deferred.submitted.append((fn, a, k))
        monkeypatch.setattr(rlm, "_io_pool", deferred)
        kv.sets.clear()
        m.record("nse", 429)
        assert kv.sets == []                       # nothing written on the caller's thread
        (fn, args, _), = deferred.submitted
        assert fn == m._persist_background and args == (m._events[0],)

    def test_pool_shutdown_is_swallowed(self, kv, monkeypatch):
        m = new_monitor()
        monkeypatch.setattr(rlm, "_io_pool", FakePool(raises=RuntimeError("cannot schedule new futures")))
        m.record("nse", 429)  # must not raise
        assert len(m._events) == 1

    def test_end_to_end_with_pool_persists_to_kv(self, kv, pool, clock):
        m = new_monitor()
        kv.sets.clear()
        m.record("gemini", 429)
        keys = [k for k, _, _ in kv.sets]
        assert keys == [rlm.NEON_EVENTS_KEY, rlm.NEON_STATS_KEY]
        assert kv.store[rlm.NEON_STATS_KEY]["by_source_1h"] == {"gemini": 1}


# ─────────────────────────────── _persist_background ───────────────────────────────

class TestPersistBackground:
    def test_redis_mirror_then_neon(self, kv, clock):
        m = new_monitor()
        m._redis = FakeRedis()
        event = ev(clock.now)
        m._events.appendleft(event)
        kv.sets.clear()
        m._persist_background(event)
        assert m._redis.calls == [
            ("lpush", (rlm.REDIS_KEY, json.dumps(event))),
            ("ltrim", (rlm.REDIS_KEY, 0, rlm.MAX_EVENTS - 1)),
            ("expire", (rlm.REDIS_KEY, rlm.REDIS_TTL)),
        ]
        assert [k for k, _, _ in kv.sets] == [rlm.NEON_EVENTS_KEY, rlm.NEON_STATS_KEY]

    def test_no_redis_still_writes_neon(self, kv, clock):
        m = new_monitor()
        m._events.appendleft(ev(clock.now))
        kv.sets.clear()
        m._persist_background(ev(clock.now))
        assert len(kv.sets) == 2

    @pytest.mark.parametrize("failing", ["lpush", "ltrim", "expire"])
    def test_redis_failure_does_not_block_neon(self, kv, clock, caplog, failing):
        m = new_monitor()
        m._redis = FakeRedis(fail=failing)
        m._events.appendleft(ev(clock.now))
        kv.sets.clear()
        with caplog.at_level(logging.DEBUG, logger="rate-limit-monitor"):
            m._persist_background(ev(clock.now))
        assert any("Redis record failed" in r.getMessage() for r in caplog.records)
        assert len(kv.sets) == 2

    def test_neon_failure_is_swallowed(self, kv, monkeypatch):
        m = new_monitor()

        def boom(self):
            raise RuntimeError("persist exploded")

        monkeypatch.setattr(rlm.RateLimitMonitor, "_persist_neon", boom)
        m._persist_background(ev(1))  # must not raise


# ─────────────────────────────── _all_events ───────────────────────────────

class TestAllEvents:
    def test_memory_only(self, kv):
        m = new_monitor()
        m._events.extend([ev(3), ev(2)])
        got = m._all_events()
        assert got == [ev(3), ev(2)]
        assert got is not m._events  # a copy

    def test_redis_wins_when_it_has_events(self, kv):
        m = new_monitor()
        m._events.append(ev(1, source="memory"))
        m._redis = FakeRedis(lrange_result=[json.dumps(ev(2, source="redis"))])
        assert [e["source"] for e in m._all_events()] == ["redis"]
        assert m._redis.calls == [("lrange", (rlm.REDIS_KEY, 0, rlm.MAX_EVENTS - 1))]

    def test_redis_item_types(self, kv):
        m = new_monitor()
        m._redis = FakeRedis(lrange_result=[
            json.dumps(ev(1, source="str")),
            json.dumps(ev(2, source="bytes")).encode(),
            ev(3, source="dict"),
            "{not json",         # unparseable -> skipped, the rest are kept
        ])
        assert [e["source"] for e in m._all_events()] == ["str", "bytes", "dict"]

    def test_unrecognised_item_types_are_ignored(self, kv):
        m = new_monitor()
        m._events.append(ev(9, source="memory"))
        m._redis = FakeRedis(lrange_result=[123, None, 4.5])
        # nothing usable from Redis -> falls back to memory
        assert [e["source"] for e in m._all_events()] == ["memory"]

    def test_undecodable_bytes_fall_back_to_memory(self, kv, caplog):
        m = new_monitor()
        m._events.append(ev(9, source="memory"))
        m._redis = FakeRedis(lrange_result=[b"\xff\xfe"])
        with caplog.at_level(logging.DEBUG, logger="rate-limit-monitor"):
            assert [e["source"] for e in m._all_events()] == ["memory"]
        assert any("Redis read failed" in r.getMessage() for r in caplog.records)

    def test_redis_error_falls_back_to_memory(self, kv, caplog):
        m = new_monitor()
        m._events.append(ev(9, source="memory"))
        m._redis = FakeRedis(fail="lrange")
        with caplog.at_level(logging.DEBUG, logger="rate-limit-monitor"):
            assert [e["source"] for e in m._all_events()] == ["memory"]
        assert any("Redis read failed" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("empty", [[], None])
    def test_empty_redis_falls_back_to_memory(self, kv, empty):
        m = new_monitor()
        m._events.append(ev(9, source="memory"))
        m._redis = FakeRedis(lrange_result=empty)
        assert [e["source"] for e in m._all_events()] == ["memory"]

    def test_partial_redis_results_before_error_are_kept(self, kv):
        m = new_monitor()

        class Exploding(list):
            def __iter__(self):
                yield json.dumps(ev(1, source="ok"))
                raise ConnectionError("cut off")

        r = FakeRedis()
        r.lrange = lambda *a: Exploding([1])
        m._redis = r
        assert [e["source"] for e in m._all_events()] == ["ok"]


# ─────────────────────────────── snapshot ───────────────────────────────

class TestSnapshotBasics:
    def test_healthy_when_empty(self, kv, clock):
        m = new_monitor()
        s = m.snapshot()
        assert s["overall"] == "healthy"
        assert s["window_sec"] == rlm.WINDOW_SEC
        assert s["events_1h"] == 0
        assert s["by_status_1h"] == {}
        assert s["recent_events"] == []
        assert s["circuits"] == []
        assert s["redis_backed"] is False
        assert s["neon_backed"] is False
        assert s["generated_at"] == clock.now
        assert [u["id"] for u in s["upstreams"]] == [u["id"] for u in rlm.UPSTREAMS]
        assert all(u["level"] == "ok" and u["events_1h"] == 0 and u["by_status"] == {} for u in s["upstreams"])
        assert s["by_source_1h"] == {u["id"]: 0 for u in rlm.UPSTREAMS}
        assert s["advice"] == [
            "No rate-limit pressure in the last hour.",
            "Index names are mapped to Yahoo tickers — avoid raw 'NIFTY NEXT 50' paths.",
        ]

    def test_counts_by_status_and_source(self, kv, clock):
        m = new_monitor()
        now = clock.now
        m._events.extend([
            ev(now - 1, "market_data", 429),
            ev(now - 2, "market_data", 429),
            ev(now - 3, "market_data", 503),
            ev(now - 4, "gemini", 429),
        ])
        s = m.snapshot()
        assert s["events_1h"] == 4
        assert s["by_status_1h"] == {"429": 3, "503": 1}
        assert s["by_source_1h"]["market_data"] == 3
        assert s["by_source_1h"]["gemini"] == 1
        md = next(u for u in s["upstreams"] if u["id"] == "market_data")
        assert md["by_status"] == {"429": 2, "503": 1}
        assert md["events_1h"] == 3
        assert md["label"] == "Market Data / Yahoo" and md["codes"] == [429, 503, 404]

    def test_old_events_excluded_and_boundary_included(self, kv, clock):
        m = new_monitor()
        now = clock.now
        m._events.extend([
            ev(now - rlm.WINDOW_SEC, "nse", 429),         # exactly at window edge: included
            ev(now - rlm.WINDOW_SEC - 0.5, "nse", 429),   # just outside
            {"source": "nse", "status": 429},              # no ts: treated as ts=0 -> outside
        ])
        s = m.snapshot()
        assert s["events_1h"] == 1

    def test_missing_source_and_status_fields(self, kv, clock):
        m = new_monitor()
        m._events.append({"ts": clock.now})
        s = m.snapshot()
        assert s["events_1h"] == 1
        assert s["by_status_1h"] == {"0": 1}
        assert s["overall"] == "healthy"

    def test_none_source_becomes_unknown_and_is_not_an_upstream(self, kv, clock):
        m = new_monitor()
        m._events.append({"ts": clock.now, "source": None, "status": 429})
        s = m.snapshot()
        assert s["by_status_1h"] == {"429": 1}
        assert sum(s["by_source_1h"].values()) == 0  # "unknown" is not in UPSTREAMS

    def test_recent_events_capped_at_forty(self, kv, clock):
        m = new_monitor()
        for i in range(60):
            m._events.appendleft(ev(clock.now - i, "nse", 429, detail=str(i)))
        s = m.snapshot()
        assert len(s["recent_events"]) == 40
        assert s["events_1h"] == 60
        assert s["recent_events"][0]["detail"] == "59"  # deque is newest-first (appendleft)

    def test_redis_backed_flag_and_source(self, kv, clock):
        m = new_monitor()
        m._redis = FakeRedis(lrange_result=[json.dumps(ev(clock.now, "gemini", 429))])
        s = m.snapshot()
        assert s["redis_backed"] is True
        assert s["by_source_1h"]["gemini"] == 1


class TestSnapshotLevels:
    @staticmethod
    def _level(m, clock, n, source="gemini", status=429):
        m._events.clear()
        m._events.extend(ev(clock.now - 1, source, status) for _ in range(n))
        return next(u for u in m.snapshot()["upstreams"] if u["id"] == source)["level"]

    @pytest.mark.parametrize("n,level", [
        (0, "ok"), (1, "watch"), (4, "watch"), (5, "warn"), (19, "warn"), (20, "critical"), (40, "critical"),
    ])
    def test_upstream_thresholds(self, kv, clock, n, level):
        m = new_monitor()
        assert self._level(m, clock, n) == level

    def test_only_listed_codes_count_for_an_upstream(self, kv, clock):
        m = new_monitor()
        # gemini only watches 429; a pile of 503s must not raise its level
        assert self._level(m, clock, 30, "gemini", 503) == "ok"
        # ...but analysis watches 502/503
        assert self._level(m, clock, 30, "analysis", 503) == "critical"
        assert self._level(m, clock, 5, "analysis", 502) == "warn"
        assert self._level(m, clock, 3, "indianapi", 403) == "watch"

    def test_codes_are_summed_across_listed_statuses(self, kv, clock):
        m = new_monitor()
        now = clock.now
        m._events.extend([ev(now - 1, "market_data", 429) for _ in range(3)]
                         + [ev(now - 1, "market_data", 404) for _ in range(2)])
        md = next(u for u in m.snapshot()["upstreams"] if u["id"] == "market_data")
        assert md["events_1h"] == 5 and md["level"] == "warn"


class TestSnapshotOverall:
    @staticmethod
    def _overall(m, clock, n429=0, n503=0):
        m._events.clear()
        m._events.extend([ev(clock.now - 1, "x", 429) for _ in range(n429)]
                         + [ev(clock.now - 1, "x", 503) for _ in range(n503)])
        return m.snapshot()["overall"]

    @pytest.mark.parametrize("n429,n503,expected", [
        (0, 0, "healthy"),
        (1, 0, "watch"), (4, 0, "watch"),
        (0, 1, "watch"), (0, 9, "watch"),
        (5, 0, "degraded"), (14, 0, "degraded"),
        (0, 10, "degraded"), (0, 29, "degraded"),
        (15, 0, "critical"),
        (0, 30, "critical"),
        (2, 12, "degraded"),
    ])
    def test_thresholds(self, kv, clock, n429, n503, expected):
        assert self._overall(new_monitor(), clock, n429, n503) == expected

    def test_other_statuses_do_not_affect_overall(self, kv, clock):
        m = new_monitor()
        m._events.extend(ev(clock.now - 1, "x", 404) for _ in range(50))
        assert m.snapshot()["overall"] == "healthy"

    def test_advice_follows_overall(self, kv, clock):
        m = new_monitor()
        m._events.append(ev(clock.now - 1, "x", 429))
        adv = m.snapshot()["advice"]
        assert any("HTTP 429" in a for a in adv)
        assert not any("No rate-limit pressure" in a for a in adv)


class TestSnapshotCircuits:
    def test_dict_snapshots_passed_through(self, kv, clock):
        m = new_monitor()
        snap = {"name": "market_data", "state": "open"}
        assert m.snapshot(circuits={"market_data": snap})["circuits"] == [snap]

    def test_non_dict_snapshots_wrapped(self, kv, clock):
        m = new_monitor()
        s = m.snapshot(circuits={"gemini": "half-open", "x": 3})
        assert s["circuits"] == [{"name": "gemini", "raw": "half-open"}, {"name": "x", "raw": 3}]

    def test_none_and_empty(self, kv, clock):
        m = new_monitor()
        assert m.snapshot(circuits=None)["circuits"] == []
        assert m.snapshot(circuits={})["circuits"] == []


class TestSnapshotNeonMerge:
    def test_neon_extra_sources_are_merged(self, kv, clock):
        kv.store[rlm.NEON_STATS_KEY] = {"by_source_1h": {"yahoo": 7, "market_data": 99}}
        m = new_monitor()
        s = m.snapshot()
        assert s["by_source_1h"]["yahoo"] == 7
        assert s["neon_backed"] is True

    def test_local_counts_override_neon_for_known_upstreams(self, kv, clock):
        kv.store[rlm.NEON_STATS_KEY] = {"by_source_1h": {"market_data": 99}}
        m = new_monitor()
        m._events.append(ev(clock.now - 1, "market_data", 429))
        assert m.snapshot()["by_source_1h"]["market_data"] == 1

    def test_cold_start_with_neon_aggregate_only(self, kv, clock):
        kv.store[rlm.NEON_STATS_KEY] = {"by_source_1h": {"yahoo": 4}}
        m = new_monitor()
        s = m.snapshot()
        assert s["events_1h"] == 0
        assert s["by_source_1h"]["yahoo"] == 4

    def test_stats_without_by_source(self, kv, clock):
        kv.store[rlm.NEON_STATS_KEY] = {"updated_at": 1}
        m = new_monitor()
        s = m.snapshot()
        assert s["by_source_1h"] == {u["id"]: 0 for u in rlm.UPSTREAMS}
        assert s["neon_backed"] is True

    def test_non_dict_stats_are_ignored(self, kv, clock):
        kv.store[rlm.NEON_STATS_KEY] = ["not", "a", "dict"]
        m = new_monitor()
        s = m.snapshot()
        assert s["by_source_1h"] == {u["id"]: 0 for u in rlm.UPSTREAMS}
        assert s["neon_backed"] is True  # any truthy stats value counts as "backed"

    def test_neon_backed_from_hydration(self, kv, clock):
        kv.store[rlm.NEON_EVENTS_KEY] = [ev(clock.now - 5, "nse", 429)]
        m = new_monitor()
        assert m.snapshot()["neon_backed"] is True

    def test_kv_error_during_snapshot_is_tolerated(self, kv, clock):
        m = new_monitor()
        kv.get_raises = RuntimeError("db down")
        s = m.snapshot()
        assert s["neon_backed"] is False and s["overall"] == "healthy"

    def test_snapshot_reads_stats_key(self, kv, clock):
        m = new_monitor()
        kv.gets.clear()
        m.snapshot()
        assert kv.gets == [rlm.NEON_STATS_KEY]


class TestRoundTrip:
    def test_record_then_restart_hydrates_dashboard(self, kv, pool, clock):
        first = new_monitor()
        for i in range(3):
            first.record("gemini", 429, detail=str(i))
            clock.now += 1
        restarted = new_monitor()  # hydrates from what `first` persisted
        s = restarted.snapshot()
        assert s["events_1h"] == 3
        assert [e["detail"] for e in s["recent_events"]] == ["2", "1", "0"]  # newest first survives
        assert s["by_source_1h"]["gemini"] == 3
        assert s["neon_backed"] is True


# ─────────────────────────────── _advice ───────────────────────────────

class TestAdvice:
    def test_healthy(self):
        assert rlm._advice("healthy", 0, 0) == [
            "No rate-limit pressure in the last hour.",
            "Index names are mapped to Yahoo tickers — avoid raw 'NIFTY NEXT 50' paths.",
        ]

    def test_429_only(self):
        tips = rlm._advice("watch", 2, 0)
        assert tips[0].startswith("HTTP 429")
        assert len(tips) == 2

    def test_503_only(self):
        tips = rlm._advice("watch", 0, 2)
        assert tips[0].startswith("HTTP 503")
        assert len(tips) == 2

    def test_both(self):
        tips = rlm._advice("critical", 20, 40)
        assert tips[0].startswith("HTTP 429") and tips[1].startswith("HTTP 503")
        assert len(tips) == 3

    def test_healthy_label_wins_even_with_counts(self):
        # _advice trusts its `overall` argument; the "healthy" tip is keyed off the label alone
        assert "No rate-limit pressure in the last hour." in rlm._advice("healthy", 1, 0)
