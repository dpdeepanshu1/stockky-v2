"""
tests/test_circuit_breaker.py — coverage for circuit_breaker.py

No Redis, no upstash, no kv_cache — all tests use memory-only breakers.
The cross-service record_rate_limit_hit() is tested via a kv_cache stub.

Run from services/market-data-service:
    python3 -m pytest tests/test_circuit_breaker.py -v
"""
from __future__ import annotations
import os, sys, time, threading, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import circuit_breaker as cb


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test gets a fresh registry and no Redis."""
    cb._registry.clear()
    cb._redis = None
    cb._redis_init = False
    yield
    cb._registry.clear()
    cb._redis = None
    cb._redis_init = False


# ══════════════════════════════════════════════════════════════════════════════
# _get_redis — off by default
# ══════════════════════════════════════════════════════════════════════════════

class TestGetRedis:
    def test_returns_none_by_default(self, monkeypatch):
        monkeypatch.delenv("USE_REDIS", raising=False)
        cb._redis_init = False
        assert cb._get_redis() is None

    def test_cached_after_first_call(self, monkeypatch):
        monkeypatch.delenv("USE_REDIS", raising=False)
        cb._redis_init = False
        r1 = cb._get_redis()
        r2 = cb._get_redis()
        assert r1 is r2 is None

    def test_disable_redis_env_wins(self, monkeypatch):
        monkeypatch.setenv("USE_REDIS", "1")
        monkeypatch.setenv("DISABLE_REDIS", "1")
        cb._redis_init = False
        assert cb._get_redis() is None

    def test_disable_upstash_wins(self, monkeypatch):
        monkeypatch.setenv("USE_REDIS", "1")
        monkeypatch.setenv("DISABLE_UPSTASH", "1")
        cb._redis_init = False
        assert cb._get_redis() is None

    def test_use_redis_without_url_returns_none(self, monkeypatch):
        monkeypatch.setenv("USE_REDIS", "1")
        monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
        monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
        cb._redis_init = False
        assert cb._get_redis() is None


# ══════════════════════════════════════════════════════════════════════════════
# CircuitBreaker state machine
# ══════════════════════════════════════════════════════════════════════════════

def _breaker(**kw):
    return cb.CircuitBreaker("test", failure_threshold=3, recovery_timeout=0.05,
                             half_open_success=2, **kw)


class TestCircuitBreakerStateMachine:
    def test_starts_closed_and_allows(self):
        br = _breaker()
        assert br.state() == "closed"
        assert br.allow() is True

    def test_opens_after_threshold_failures(self):
        br = _breaker()
        for _ in range(3):
            br.record_failure("err")
        assert br.state() == "open"
        assert br.allow() is False

    def test_subsequent_failures_do_not_reset_opened_at(self):
        """Once open, further record_failure calls must not re-stamp opened_at
        (the regression that kept the circuit open indefinitely)."""
        br = _breaker()
        for _ in range(3):
            br.record_failure("err")
        t1 = br._opened_at
        time.sleep(0.01)
        br.record_failure("more")
        assert br._opened_at == t1    # unchanged

    def test_transitions_to_half_open_after_recovery_timeout(self):
        br = _breaker()
        for _ in range(3):
            br.record_failure()
        time.sleep(0.06)
        assert br.state() == "half_open"
        assert br.allow() is True

    def test_half_open_closes_after_enough_successes(self):
        br = _breaker()
        for _ in range(3):
            br.record_failure()
        time.sleep(0.06)
        br.state()    # trigger half_open transition
        br.record_success()
        assert br.state() == "half_open"
        br.record_success()
        assert br.state() == "closed"
        assert br._failures == 0

    def test_half_open_probe_failure_reopens(self):
        br = _breaker()
        for _ in range(3):
            br.record_failure()
        time.sleep(0.06)
        br.state()    # trigger half_open
        br.record_failure("probe failed")
        assert br.state() == "open"
        assert br._failures == br.failure_threshold

    def test_success_in_closed_resets_failure_count(self):
        br = _breaker()
        br.record_failure()
        br.record_failure()
        br.record_success()
        assert br._failures == 0
        assert br.state() == "closed"

    def test_retry_after_returns_zero_when_closed(self):
        br = _breaker()
        assert br.retry_after() == 0.0

    def test_retry_after_positive_when_open(self):
        br = _breaker()
        for _ in range(3):
            br.record_failure()
        assert br.retry_after() > 0.0

    def test_circuit_open_error_carries_name_and_retry_after(self):
        br = _breaker()
        for _ in range(3):
            br.record_failure()
        with pytest.raises(cb.CircuitOpenError) as exc:
            br.call(lambda: None)
        assert exc.value.name == "test"
        assert exc.value.retry_after > 0.0

    def test_call_records_success_and_returns_result(self):
        br = _breaker()
        result = br.call(lambda: 42)
        assert result == 42
        assert br._failures == 0

    def test_call_records_failure_and_reraises(self):
        br = _breaker()
        with pytest.raises(ValueError, match="boom"):
            br.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
        assert br._failures == 1


# ══════════════════════════════════════════════════════════════════════════════
# snapshot
# ══════════════════════════════════════════════════════════════════════════════

class TestSnapshot:
    def test_snapshot_fields(self):
        br = _breaker()
        s = br.snapshot()
        assert s["name"] == "test"
        assert s["state"] == "closed"
        assert s["failures"] == 0
        assert s["opened_at"] is None
        assert s["redis_backed"] is False

    def test_snapshot_opened_at_populated_when_open(self):
        br = _breaker()
        for _ in range(3):
            br.record_failure()
        s = br.snapshot()
        assert s["opened_at"] is not None
        assert s["state"] == "open"


# ══════════════════════════════════════════════════════════════════════════════
# get_breaker / all_snapshots
# ══════════════════════════════════════════════════════════════════════════════

class TestRegistry:
    def test_get_breaker_returns_same_instance(self):
        b1 = cb.get_breaker("alpha")
        b2 = cb.get_breaker("alpha")
        assert b1 is b2

    def test_different_names_give_different_instances(self):
        b1 = cb.get_breaker("x")
        b2 = cb.get_breaker("y")
        assert b1 is not b2

    def test_all_snapshots_includes_all_registered(self):
        cb.get_breaker("one")
        cb.get_breaker("two")
        snaps = cb.all_snapshots()
        assert "one" in snaps and "two" in snaps


# ══════════════════════════════════════════════════════════════════════════════
# _looks_like_rate_limit / _provider_from_breaker
# ══════════════════════════════════════════════════════════════════════════════

class TestHelpers:
    @pytest.mark.parametrize("msg", ["429", "rate limit exceeded", "Too Many Requests", "quota exhausted", "throttled"])
    def test_looks_like_rate_limit_true(self, msg):
        assert cb._looks_like_rate_limit(msg) is True

    def test_looks_like_rate_limit_false_on_generic_error(self):
        assert cb._looks_like_rate_limit("connection refused") is False

    def test_looks_like_rate_limit_empty_string(self):
        assert cb._looks_like_rate_limit("") is False

    @pytest.mark.parametrize("name,expected", [
        ("yahoo_quotes", "market_data"),
        ("yfinance_history", "market_data"),
        ("nse_symbols", "nse"),
        ("alpha_vantage", "market_data"),
        ("indianapi_quotes", "indianapi"),
        ("gemini_news", "gemini"),
        ("groq_summary", "groq"),
        ("unknown_provider", "market_data"),
    ])
    def test_provider_from_breaker(self, name, expected):
        assert cb._provider_from_breaker(name) == expected


# ══════════════════════════════════════════════════════════════════════════════
# record_rate_limit_hit — kv_cache stub
# ══════════════════════════════════════════════════════════════════════════════

class TestRecordRateLimitHit:
    def _install_kv_stub(self, monkeypatch):
        store = {}
        fake_kv = types.ModuleType("kv_cache")
        fake_kv.kv_get = lambda k: store.get(k)
        fake_kv.kv_set = lambda k, v, ttl=None: store.update({k: v})
        monkeypatch.setitem(sys.modules, "kv_cache", fake_kv)
        return store

    def test_records_event_and_stats(self, monkeypatch):
        store = self._install_kv_stub(monkeypatch)
        cb.record_rate_limit_hit("market_data", status=429, detail="test hit")
        assert cb.NEON_EVENTS_KEY in store
        events = store[cb.NEON_EVENTS_KEY]
        assert events[0]["source"] == "market_data"
        assert events[0]["status"] == 429
        assert cb.NEON_STATS_KEY in store

    def test_swallows_kv_import_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "kv_cache", None)
        # Must not raise even without kv_cache
        cb.record_rate_limit_hit("market_data")

    def test_merges_with_prior_stats(self, monkeypatch):
        store = self._install_kv_stub(monkeypatch)
        cb.record_rate_limit_hit("market_data")
        cb.record_rate_limit_hit("nse")
        stats = store[cb.NEON_STATS_KEY]
        assert "by_source_1h" in stats

    def test_truncates_events_to_500(self, monkeypatch):
        store = self._install_kv_stub(monkeypatch)
        fake_kv = sys.modules["kv_cache"]
        existing = [{"ts": 0, "source": "x", "status": 429, "path": "",
                     "detail": "", "symbol": "", "origin": "test"} for _ in range(600)]
        fake_kv.kv_get = lambda k: existing if k == cb.NEON_EVENTS_KEY else None
        cb.record_rate_limit_hit("market_data")
        events = store[cb.NEON_EVENTS_KEY]
        assert len(events) <= 500

    def test_failure_in_kv_set_does_not_raise(self, monkeypatch):
        fake_kv = types.ModuleType("kv_cache")
        fake_kv.kv_get = lambda k: None
        fake_kv.kv_set = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("neon down"))
        monkeypatch.setitem(sys.modules, "kv_cache", fake_kv)
        cb.record_rate_limit_hit("market_data")   # must not raise


# ══════════════════════════════════════════════════════════════════════════════
# Thread safety — concurrent record_failure calls
# ══════════════════════════════════════════════════════════════════════════════

class TestThreadSafety:
    def test_concurrent_failures_open_exactly_once(self):
        br = cb.CircuitBreaker("concurrent", failure_threshold=5, recovery_timeout=60)
        opens = []

        def _fail():
            orig = br._opened_at
            br.record_failure("concurrent")
            if br._state == "open" and br._opened_at != orig:
                opens.append(br._opened_at)

        threads = [threading.Thread(target=_fail) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # opened_at was stamped exactly once (the guard prevents re-stamping)
        unique = set(opens)
        assert len(unique) == 1
