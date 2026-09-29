"""
tests/test_circuit_breaker.py — api-gateway/circuit_breaker.py (100%).

The gateway copy is NOT the same as the circuit_breaker.py in market-data-service / decision /
position-stocks / real-trade (the gateway one moved all Redis I/O onto a background pool), so
these tests are written for this file rather than ported.

Hermetic: no Redis, no network, no real sleeping.
  * time          -> a fake clock injected as `cb.time`
  * Redis         -> a FakeRedis injected as `cb._redis`; upstash_redis is faked in sys.modules
  * background IO -> `cb._redis_io_pool` replaced by an inline pool that runs the job at once
                     (or raises RuntimeError to model interpreter shutdown)
  * global state  -> registry and Redis globals restored after every test

    cd services/api-gateway
    python -m pytest tests/test_circuit_breaker.py -v --cov=circuit_breaker --cov-report=term-missing
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import circuit_breaker as cb


# ─────────────────────────────── fakes ───────────────────────────────

class Clock:
    def __init__(self, now=1_700_000_000.0, mono=500.0):
        self.now = now
        self.mono = mono

    def time(self):
        return self.now

    def monotonic(self):
        return self.mono


class InlinePool:
    """Runs submitted jobs synchronously; can be told to refuse (shutdown)."""

    def __init__(self):
        self.jobs = []
        self.refuse = False

    def submit(self, fn, *args, **kwargs):
        if self.refuse:
            raise RuntimeError("cannot schedule new futures after shutdown")
        self.jobs.append((fn.__name__, args))
        return fn(*args, **kwargs)


class FakeRedis:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.sets = []
        self.fail_get = False
        self.fail_set = False
        self.pinged = False

    def ping(self):
        self.pinged = True

    def get(self, k):
        if self.fail_get:
            raise RuntimeError("redis get failed")
        return self.data.get(k)

    def set(self, k, v, ex=None):
        if self.fail_set:
            raise RuntimeError("redis set failed")
        self.sets.append((k, v, ex))
        self.data[k] = v


class LogSpy:
    def __init__(self):
        self.rec = []

    def _mk(self, level):
        return lambda msg, *a, **k: self.rec.append((level, msg % a if a else msg))

    def __getattr__(self, name):
        if name in ("debug", "info", "warning", "error", "exception"):
            return self._mk(name)
        raise AttributeError(name)

    def text(self, level=None):
        return "\n".join(m for lv, m in self.rec if level in (None, lv))


_ENV = ("USE_REDIS", "CB_REDIS_SYNC", "CB_REDIS_MIN_INTERVAL",
        "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cb, "_redis", None)
    monkeypatch.setattr(cb, "_redis_init", False)
    monkeypatch.setattr(cb, "_registry", {})
    yield


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(cb, "time", c)
    return c


@pytest.fixture
def pool(monkeypatch):
    p = InlinePool()
    monkeypatch.setattr(cb, "_redis_io_pool", p)
    return p


@pytest.fixture
def log(monkeypatch):
    spy = LogSpy()
    monkeypatch.setattr(cb, "logger", spy)
    return spy


def sync_on(monkeypatch, interval="15"):
    monkeypatch.setenv("CB_REDIS_SYNC", "1")
    monkeypatch.setenv("CB_REDIS_MIN_INTERVAL", interval)


def trip(b, n=None):
    for _ in range(n or b.failure_threshold):
        b.record_failure("boom")


# ─────────────────────────── module defaults ───────────────────────────

class TestDefaults:
    def test_defaults(self):
        assert cb.DEFAULT_FAILURE_THRESHOLD == 12
        assert cb.DEFAULT_RECOVERY_TIMEOUT == 30.0
        assert cb.DEFAULT_HALF_OPEN_SUCCESS == 2

    def test_module_pool_exists(self):
        assert cb._redis_io_pool._max_workers == 2


# ─────────────────────────────── _get_redis ───────────────────────────────

def _fake_upstash(monkeypatch, ping_raises=None, ctor_raises=None):
    made = []

    class _Redis(FakeRedis):
        def __init__(self, url=None, token=None):
            if ctor_raises:
                raise ctor_raises
            super().__init__()
            self.url, self.token = url, token
            made.append(self)

        def ping(self):
            if ping_raises:
                raise ping_raises
            super().ping()

    mod = types.ModuleType("upstash_redis")
    mod.Redis = _Redis
    monkeypatch.setitem(sys.modules, "upstash_redis", mod)
    return made


def _enable(monkeypatch, url="https://u", token="t"):
    monkeypatch.setenv("USE_REDIS", "1")
    monkeypatch.setenv("CB_REDIS_SYNC", "1")
    if url is not None:
        monkeypatch.setenv("UPSTASH_REDIS_REST_URL", url)
    if token is not None:
        monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", token)


class TestGetRedis:
    def test_off_by_default(self):
        assert cb._get_redis() is None
        assert cb._redis_init is True

    @pytest.mark.parametrize("val", ["0", "false", "no", ""])
    def test_use_redis_falsey(self, monkeypatch, val):
        monkeypatch.setenv("USE_REDIS", val)
        monkeypatch.setenv("CB_REDIS_SYNC", "1")
        assert cb._get_redis() is None

    def test_sync_flag_required(self, monkeypatch):
        monkeypatch.setenv("USE_REDIS", "1")
        assert cb._get_redis() is None

    @pytest.mark.parametrize("url,token", [(None, "t"), ("https://u", None), (None, None)])
    def test_missing_credentials(self, monkeypatch, url, token):
        _enable(monkeypatch, url=url, token=token)
        assert cb._get_redis() is None

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes"])
    def test_enabled_builds_and_pings(self, monkeypatch, log, val):
        made = _fake_upstash(monkeypatch)
        _enable(monkeypatch)
        monkeypatch.setenv("USE_REDIS", val)
        monkeypatch.setenv("CB_REDIS_SYNC", val)
        r = cb._get_redis()
        assert r is made[0] and r.pinged is True
        assert (r.url, r.token) == ("https://u", "t")
        assert "Redis backend enabled" in log.text("info")

    def test_result_is_cached_after_first_call(self, monkeypatch):
        made = _fake_upstash(monkeypatch)
        _enable(monkeypatch)
        first = cb._get_redis()
        assert cb._get_redis() is first
        assert len(made) == 1

    def test_ping_failure_disables_and_warns(self, monkeypatch, log):
        _fake_upstash(monkeypatch, ping_raises=RuntimeError("no route"))
        _enable(monkeypatch)
        assert cb._get_redis() is None
        assert cb._redis is None
        assert "Redis unavailable: no route" in log.text("warning")

    def test_constructor_failure_disables(self, monkeypatch, log):
        _fake_upstash(monkeypatch, ctor_raises=ValueError("bad url"))
        _enable(monkeypatch)
        assert cb._get_redis() is None
        assert "bad url" in log.text("warning")

    def test_failed_init_is_not_retried(self, monkeypatch):
        _fake_upstash(monkeypatch, ping_raises=RuntimeError("x"))
        _enable(monkeypatch)
        cb._get_redis()
        made = _fake_upstash(monkeypatch)
        assert cb._get_redis() is None
        assert made == []


# ─────────────────────────── CircuitOpenError ───────────────────────────

class TestCircuitOpenError:
    def test_fields_and_message(self):
        e = cb.CircuitOpenError("yf", 12.6)
        assert (e.name, e.retry_after) == ("yf", 12.6)
        assert str(e) == "circuit open for yf; retry after 13s"

    def test_is_exception(self):
        with pytest.raises(cb.CircuitOpenError):
            raise cb.CircuitOpenError("x", 0)


# ─────────────────────────── construction ───────────────────────────

class TestConstruction:
    def test_defaults(self):
        b = cb.CircuitBreaker("svc")
        assert b.name == "svc"
        assert (b.failure_threshold, b.recovery_timeout, b.half_open_success) == (12, 30.0, 2)
        assert b._state == "closed" and b._failures == 0 and b._opened_at == 0.0
        assert b._last_error is None

    def test_values_are_coerced_and_floored(self):
        b = cb.CircuitBreaker("svc", failure_threshold="0", recovery_timeout="7", half_open_success=-3)
        assert (b.failure_threshold, b.recovery_timeout, b.half_open_success) == (1, 7.0, 1)

    def test_redis_key_names(self):
        assert cb.CircuitBreaker("svc")._rk("state") == "cb:svc:state"

    def test_constructor_schedules_remote_load(self, monkeypatch, pool, clock):
        sync_on(monkeypatch)
        _fake_upstash(monkeypatch)
        b = cb.CircuitBreaker("svc")
        assert [j[0] for j in pool.jobs] == ["_load_remote_blocking"]
        assert b._last_load_at == clock.now


# ───────────────────── _schedule_remote_load / _load_remote_blocking ─────────────────────

class TestRemoteLoad:
    def test_noop_without_sync_flag(self, pool):
        b = cb.CircuitBreaker("svc")
        b._schedule_remote_load()
        assert pool.jobs == []

    def test_throttled_within_interval(self, monkeypatch, pool, clock):
        sync_on(monkeypatch, "15")
        b = cb.CircuitBreaker("svc")
        assert len(pool.jobs) == 1
        clock.now += 14
        b._schedule_remote_load()
        assert len(pool.jobs) == 1
        clock.now += 1
        b._schedule_remote_load()
        assert len(pool.jobs) == 2

    def test_pool_shutdown_is_swallowed(self, monkeypatch, pool):
        sync_on(monkeypatch)
        pool.refuse = True
        b = cb.CircuitBreaker("svc")  # must not raise
        assert b._state == "closed"

    def test_no_redis_is_a_noop(self, monkeypatch, pool):
        sync_on(monkeypatch)
        b = cb.CircuitBreaker("svc")
        assert b._state == "closed"

    def _b(self, monkeypatch, redis):
        monkeypatch.setattr(cb, "_redis", redis)
        monkeypatch.setattr(cb, "_redis_init", True)
        return cb.CircuitBreaker("svc")

    def test_loads_all_three_keys(self, monkeypatch):
        r = FakeRedis({"cb:svc:state": "open", "cb:svc:failures": "9", "cb:svc:opened_at": "1700000000.5"})
        b = self._b(monkeypatch, r)
        b._load_remote_blocking()
        assert (b._state, b._failures, b._opened_at) == ("open", 9, 1700000000.5)

    def test_bytes_state_is_decoded(self, monkeypatch):
        b = self._b(monkeypatch, FakeRedis({"cb:svc:state": b"half_open"}))
        b._load_remote_blocking()
        assert b._state == "half_open"

    def test_unknown_state_is_ignored(self, monkeypatch):
        b = self._b(monkeypatch, FakeRedis({"cb:svc:state": "weird"}))
        b._load_remote_blocking()
        assert b._state == "closed"

    def test_missing_keys_leave_local_state(self, monkeypatch):
        b = self._b(monkeypatch, FakeRedis())
        b._failures = 4
        b._load_remote_blocking()
        assert (b._state, b._failures, b._opened_at) == ("closed", 4, 0.0)

    def test_get_failure_is_logged_and_swallowed(self, monkeypatch, log):
        r = FakeRedis()
        r.fail_get = True
        b = self._b(monkeypatch, r)
        b._load_remote_blocking()
        assert "cb load remote svc: redis get failed" in log.text("debug")

    def test_garbage_value_is_swallowed(self, monkeypatch, log):
        b = self._b(monkeypatch, FakeRedis({"cb:svc:failures": "not-an-int"}))
        b._load_remote_blocking()
        assert b._failures == 0
        assert "cb load remote svc" in log.text("debug")


# ───────────────────── _schedule_persist / _persist_blocking ─────────────────────

class TestPersist:
    def test_noop_without_sync_flag(self, pool):
        b = cb.CircuitBreaker("svc")
        with b._lock:
            b._schedule_persist()
        assert pool.jobs == []

    def test_persists_state_failures_and_ttl(self, monkeypatch, pool, clock):
        sync_on(monkeypatch)
        r = FakeRedis()
        monkeypatch.setattr(cb, "_redis", r)
        monkeypatch.setattr(cb, "_redis_init", True)
        b = cb.CircuitBreaker("svc", failure_threshold=2, recovery_timeout=100)
        pool.jobs.clear()
        clock.now += 100  # get past the load throttle; persist has its own
        trip(b)
        keys = {k: (v, ex) for k, v, ex in r.sets}
        assert keys["cb:svc:state"] == ("open", 300)
        assert keys["cb:svc:failures"] == ("2", 300)
        assert keys["cb:svc:opened_at"][0] == str(clock.now)

    def test_ttl_has_60s_floor(self, monkeypatch, pool):
        sync_on(monkeypatch)
        r = FakeRedis()
        monkeypatch.setattr(cb, "_redis", r)
        monkeypatch.setattr(cb, "_redis_init", True)
        b = cb.CircuitBreaker("svc", recovery_timeout=5)
        b.record_failure("x")
        assert {ex for _, _, ex in r.sets} == {60}

    def test_unchanged_signature_is_throttled(self, monkeypatch, pool, clock):
        sync_on(monkeypatch, "15")
        b = cb.CircuitBreaker("svc")
        pool.jobs.clear()
        b.record_success()
        b.record_success()
        assert len(pool.jobs) == 1
        clock.now += 15
        b.record_success()
        assert len(pool.jobs) == 2

    def test_changed_signature_bypasses_throttle(self, monkeypatch, pool):
        sync_on(monkeypatch, "15")
        b = cb.CircuitBreaker("svc")
        pool.jobs.clear()
        b.record_failure("a")
        b.record_failure("b")
        assert len(pool.jobs) == 2

    def test_pool_shutdown_is_swallowed(self, monkeypatch, pool):
        sync_on(monkeypatch)
        pool.refuse = True
        b = cb.CircuitBreaker("svc")
        b.record_failure("x")  # must not raise
        assert b._failures == 1

    def test_persist_without_redis_is_a_noop(self):
        b = cb.CircuitBreaker("svc")
        b._persist_blocking("open", 3, 1.0, 60)  # _get_redis() -> None

    def test_persist_skips_opened_at_when_zero(self, monkeypatch):
        r = FakeRedis()
        monkeypatch.setattr(cb, "_redis", r)
        monkeypatch.setattr(cb, "_redis_init", True)
        cb.CircuitBreaker("svc")._persist_blocking("closed", 0, 0.0, 60)
        assert [k for k, _, _ in r.sets] == ["cb:svc:state", "cb:svc:failures"]

    def test_persist_failure_is_logged_and_swallowed(self, monkeypatch, log):
        r = FakeRedis()
        r.fail_set = True
        monkeypatch.setattr(cb, "_redis", r)
        monkeypatch.setattr(cb, "_redis_init", True)
        cb.CircuitBreaker("svc")._persist_blocking("open", 1, 1.0, 60)
        assert "cb persist svc: redis set failed" in log.text("debug")


# ─────────────────────── state / snapshot / half-open ───────────────────────

class TestStateMachine:
    def test_starts_closed_and_allows(self):
        b = cb.CircuitBreaker("svc")
        assert b.state() == "closed" and b.allow() is True

    def test_opens_at_threshold_and_blocks(self, clock):
        b = cb.CircuitBreaker("svc", failure_threshold=3)
        trip(b, 2)
        assert b.state() == "closed" and b.allow() is True
        b.record_failure("third")
        assert b.state() == "open" and b.allow() is False
        assert b._opened_at == clock.now

    def test_success_resets_failure_count_while_closed(self):
        b = cb.CircuitBreaker("svc", failure_threshold=3)
        trip(b, 2)
        b.record_success()
        assert b._failures == 0
        trip(b, 2)
        assert b.state() == "closed"

    def test_last_error_recorded_and_truncated(self):
        b = cb.CircuitBreaker("svc")
        b.record_failure("x" * 500)
        assert b._last_error == "x" * 200
        b.record_failure("")
        assert b._last_error == ""
        b.record_failure(None)
        assert b._last_error == ""

    def test_open_logs_warning_once(self, log):
        b = cb.CircuitBreaker("svc", failure_threshold=2)
        trip(b, 2)
        assert "circuit svc → open after 2 failures: boom" in log.text("warning")

    def test_extra_failures_while_open_do_not_restamp_opened_at(self, clock):
        # regression: in-flight calls landing after the trip used to push opened_at forward
        b = cb.CircuitBreaker("svc", failure_threshold=2, recovery_timeout=30)
        trip(b, 2)
        stamped = b._opened_at
        clock.now += 20
        for _ in range(5):
            b.record_failure("late")
        assert b._opened_at == stamped
        assert b._failures == 7
        clock.now = stamped + 30
        assert b.state() == "half_open"

    def test_half_open_after_recovery_timeout_wall_clock(self, clock):
        b = cb.CircuitBreaker("svc", failure_threshold=1, recovery_timeout=30)
        b.record_failure("x")
        clock.now += 29.9
        assert b.state() == "open" and b.allow() is False
        clock.now += 0.1
        assert b.state() == "half_open" and b.allow() is True
        assert b._successes_half == 0

    def test_half_open_uses_monotonic_when_opened_at_is_small(self, clock):
        b = cb.CircuitBreaker("svc", failure_threshold=1, recovery_timeout=30)
        b._state, b._opened_at = "open", 100.0  # < 1e9 -> monotonic base
        clock.mono = 129.0
        assert b.state() == "open"
        clock.mono = 130.0
        assert b.state() == "half_open"

    def test_open_without_opened_at_never_half_opens(self, clock):
        b = cb.CircuitBreaker("svc")
        b._state, b._opened_at = "open", 0.0
        clock.now += 10_000
        assert b.state() == "open"

    def test_half_open_success_closes(self, log):
        b = cb.CircuitBreaker("svc", failure_threshold=1, half_open_success=2)
        b._state, b._failures, b._last_error = "half_open", 5, "old"
        b._opened_at = 1.0
        b.record_success()
        assert b.state() == "half_open" and b._successes_half == 1
        b.record_success()
        assert b.state() == "closed"
        assert (b._failures, b._opened_at, b._last_error) == (0, 0.0, None)
        assert "circuit svc → closed" in log.text("info")

    def test_half_open_success_does_not_reset_failures_before_closing(self):
        b = cb.CircuitBreaker("svc", half_open_success=3)
        b._state, b._failures = "half_open", 7
        b.record_success()
        assert b._failures == 7

    def test_half_open_probe_failure_reopens(self, clock, log):
        b = cb.CircuitBreaker("svc", failure_threshold=4)
        b._state = "half_open"
        b.record_failure("probe died")
        assert b._state == "open"
        assert b._opened_at == clock.now
        assert b._failures == 4
        assert "half_open probe failed): probe died" in log.text("warning")

    def test_full_cycle(self, clock):
        b = cb.CircuitBreaker("svc", failure_threshold=2, recovery_timeout=10, half_open_success=1)
        trip(b, 2)
        assert b.allow() is False
        clock.now += 10
        assert b.allow() is True
        b.record_success()
        assert b.state() == "closed"


class TestRetryAfter:
    def test_zero_when_closed(self):
        assert cb.CircuitBreaker("svc").retry_after() == 0.0

    def test_zero_when_half_open(self):
        b = cb.CircuitBreaker("svc")
        b._state, b._opened_at = "half_open", 1.0
        assert b.retry_after() == 0.0

    def test_zero_when_open_but_unstamped(self):
        b = cb.CircuitBreaker("svc")
        b._state = "open"
        assert b.retry_after() == 0.0

    def test_counts_down_on_wall_clock(self, clock):
        b = cb.CircuitBreaker("svc", failure_threshold=1, recovery_timeout=30)
        b.record_failure("x")
        clock.now += 12
        assert b.retry_after() == 18.0

    def test_never_negative(self, clock):
        b = cb.CircuitBreaker("svc", failure_threshold=1, recovery_timeout=30)
        b.record_failure("x")
        clock.now += 500
        assert b.retry_after() == 0.0

    def test_counts_down_on_monotonic_for_small_stamp(self, clock):
        b = cb.CircuitBreaker("svc", recovery_timeout=30)
        b._state, b._opened_at = "open", 100.0
        clock.mono = 110.0
        assert b.retry_after() == 20.0


class TestSnapshot:
    def test_closed_snapshot(self):
        assert cb.CircuitBreaker("svc").snapshot() == {
            "name": "svc", "state": "closed", "failures": 0, "opened_at": None,
            "last_error": None, "failure_threshold": 12, "recovery_timeout": 30.0,
            "redis_backed": False,
        }

    def test_open_snapshot_carries_error_and_stamp(self, clock):
        b = cb.CircuitBreaker("svc", failure_threshold=1)
        b.record_failure("bad gateway")
        s = b.snapshot()
        assert s["state"] == "open" and s["failures"] == 1
        assert s["opened_at"] == clock.now and s["last_error"] == "bad gateway"

    def test_snapshot_applies_half_open_transition(self, clock):
        b = cb.CircuitBreaker("svc", failure_threshold=1, recovery_timeout=5)
        b.record_failure("x")
        clock.now += 5
        assert b.snapshot()["state"] == "half_open"

    def test_redis_backed_flag(self, monkeypatch):
        monkeypatch.setattr(cb, "_redis", FakeRedis())
        monkeypatch.setattr(cb, "_redis_init", True)
        assert cb.CircuitBreaker("svc").snapshot()["redis_backed"] is True
        monkeypatch.setattr(cb, "_redis", None)
        assert cb.CircuitBreaker("svc").snapshot()["redis_backed"] is False

    def test_redis_backed_false_before_init_even_if_client_set(self, monkeypatch):
        monkeypatch.setattr(cb, "_redis", FakeRedis())
        monkeypatch.setattr(cb, "_redis_init", False)
        assert cb.CircuitBreaker("svc").snapshot()["redis_backed"] is False


# ─────────────────────────── reset / call ───────────────────────────

class TestReset:
    def test_reset_closes_and_clears(self, log):
        b = cb.CircuitBreaker("svc", failure_threshold=1)
        b.record_failure("x")
        b._successes_half = 1
        b.reset()
        assert b.state() == "closed" and b.allow() is True
        assert (b._failures, b._successes_half, b._opened_at, b._last_error) == (0, 0, 0.0, None)
        assert "circuit svc → closed (manual reset)" in log.text("info")

    def test_reset_persists_when_sync_on(self, monkeypatch, pool):
        sync_on(monkeypatch)
        r = FakeRedis()
        monkeypatch.setattr(cb, "_redis", r)
        monkeypatch.setattr(cb, "_redis_init", True)
        b = cb.CircuitBreaker("svc", failure_threshold=1)
        b.record_failure("x")
        r.sets.clear()
        b.reset()
        assert dict((k, v) for k, v, _ in r.sets)["cb:svc:state"] == "closed"


class TestCall:
    def test_success_passes_args_and_returns(self):
        b = cb.CircuitBreaker("svc")
        assert b.call(lambda a, b_=0: a + b_, 2, b_=3) == 5

    def test_success_counts_toward_half_open_close(self):
        b = cb.CircuitBreaker("svc", half_open_success=1)
        b._state = "half_open"
        b.call(lambda: 1)
        assert b.state() == "closed"

    def test_failure_is_recorded_and_reraised(self):
        b = cb.CircuitBreaker("svc")

        def boom():
            raise ValueError("nope")

        with pytest.raises(ValueError):
            b.call(boom)
        assert b._failures == 1 and b._last_error == "nope"

    def test_open_circuit_raises_without_calling(self, clock):
        b = cb.CircuitBreaker("svc", failure_threshold=1, recovery_timeout=30)
        b.record_failure("x")
        clock.now += 10
        called = []
        with pytest.raises(cb.CircuitOpenError) as ei:
            b.call(lambda: called.append(1))
        assert called == []
        assert ei.value.name == "svc" and ei.value.retry_after == 20.0

    def test_trips_after_repeated_failures_through_call(self):
        b = cb.CircuitBreaker("svc", failure_threshold=3)

        def boom():
            raise RuntimeError("x")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                b.call(boom)
        with pytest.raises(cb.CircuitOpenError):
            b.call(boom)

    def test_base_exceptions_are_not_counted(self):
        b = cb.CircuitBreaker("svc")

        def stop():
            raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            b.call(stop)
        assert b._failures == 0


# ─────────────────────────────── registry ───────────────────────────────

class TestRegistry:
    def test_get_breaker_creates_once(self):
        a = cb.get_breaker("yf", failure_threshold=3, recovery_timeout=9)
        assert a is cb.get_breaker("yf", failure_threshold=99, recovery_timeout=1)
        assert (a.failure_threshold, a.recovery_timeout) == (3, 9.0)

    def test_get_breaker_defaults(self):
        b = cb.get_breaker("dflt")
        assert (b.failure_threshold, b.recovery_timeout) == (12, 30.0)

    def test_distinct_names_are_distinct(self):
        assert cb.get_breaker("a") is not cb.get_breaker("b")

    def test_all_snapshots(self):
        cb.get_breaker("a")
        cb.get_breaker("b", failure_threshold=1).record_failure("x")
        snaps = cb.all_snapshots()
        assert set(snaps) == {"a", "b"}
        assert snaps["a"]["state"] == "closed" and snaps["b"]["state"] == "open"

    def test_all_snapshots_empty(self):
        assert cb.all_snapshots() == {}

    def test_reset_all_breakers(self):
        for n in ("a", "b"):
            cb.get_breaker(n, failure_threshold=1).record_failure("x")
        assert sorted(cb.reset_all_breakers()) == ["a", "b"]
        assert {s["state"] for s in cb.all_snapshots().values()} == {"closed"}

    def test_reset_all_skips_and_logs_a_breaker_that_raises(self, log):
        good = cb.get_breaker("good", failure_threshold=1)
        good.record_failure("x")
        bad = cb.get_breaker("bad")

        def explode():
            raise RuntimeError("cannot reset")

        bad.reset = explode
        assert cb.reset_all_breakers() == ["good"]
        assert "reset bad: cannot reset" in log.text("debug")
        assert good.state() == "closed"

    def test_reset_all_empty(self):
        assert cb.reset_all_breakers() == []
