"""
tests/test_redis_rate_limit.py — api-gateway/redis_rate_limit.py (100%).

Despite its name this module is a process-local token-bucket limiter (Redis is accepted by
`set_redis` but never used); main.py's _cb_get() consults `limiter.allow()` / `wait_budget_sec()`
to pace internal fan-out.

Hermetic: no network, no real sleeping.
  * time          -> FakeClock injected as `rrl.time` (sleep() just advances the clock)
  * global state  -> the module-level bucket registry is emptied around every test
  * env           -> RL_* variables are removed around every test

Buckets built directly get `.updated` set from the fake clock because the dataclass default
factory captured the real `time.time` when the class was defined.

    cd services/api-gateway
    python -m pytest tests/test_redis_rate_limit.py -v --cov=redis_rate_limit --cov-report=term-missing
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import redis_rate_limit as rrl


# ─────────────────────────────── fakes ───────────────────────────────

class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now
        self.sleeps = []
        self.on_sleep = None

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(rrl, "time", types.SimpleNamespace(time=c.time, sleep=c.sleep))
    return c


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    for k in list(os.environ):
        if k.startswith("RL_"):
            monkeypatch.delenv(k, raising=False)
    with rrl._buckets_lock:
        rrl._buckets.clear()
    yield
    with rrl._buckets_lock:
        rrl._buckets.clear()


def make_bucket(clock, rps=10.0, capacity=5.0):
    b = rrl._Bucket(rps=rps, capacity=capacity)
    b.updated = clock.now
    return b


# ─────────────────────────────── _cfg ───────────────────────────────

class TestCfg:
    def test_known_provider_defaults(self):
        assert rrl._cfg("yfinance") == (2.0, 6)
        assert rrl._cfg("global") == (50.0, 150)
        assert rrl._cfg("market_data") == (40.0, 120)

    def test_every_default_is_positive(self):
        for name, (rps, burst) in rrl._DEFAULTS.items():
            assert rps > 0 and burst > 0, name

    def test_unknown_provider_gets_generic_default(self):
        assert rrl._cfg("something_new") == (10.0, 30)

    def test_env_overrides_both(self, monkeypatch):
        monkeypatch.setenv("RL_GEMINI_RPS", "0.5")
        monkeypatch.setenv("RL_GEMINI_BURST", "9")
        assert rrl._cfg("gemini") == (0.5, 9)

    def test_env_key_uses_upper_case_provider(self, monkeypatch):
        monkeypatch.setenv("RL_MARKET_DATA_RPS", "7")
        assert rrl._cfg("market_data") == (7.0, 120)

    def test_env_overrides_only_rps(self, monkeypatch):
        monkeypatch.setenv("RL_NSE_RPS", "4")
        assert rrl._cfg("nse") == (4.0, 3)

    def test_env_overrides_only_burst(self, monkeypatch):
        monkeypatch.setenv("RL_NSE_BURST", "11")
        assert rrl._cfg("nse") == (1.0, 11)

    def test_invalid_rps_keeps_defaults(self, monkeypatch):
        monkeypatch.setenv("RL_NSE_RPS", "fast")
        monkeypatch.setenv("RL_NSE_BURST", "11")
        # rps parse fails first, so the burst override is never reached: both stay default
        assert rrl._cfg("nse") == (1.0, 3)

    def test_invalid_burst_keeps_valid_rps_override(self, monkeypatch):
        monkeypatch.setenv("RL_NSE_RPS", "4")
        monkeypatch.setenv("RL_NSE_BURST", "lots")
        assert rrl._cfg("nse") == (4.0, 3)

    def test_empty_env_values_are_ignored(self, monkeypatch):
        monkeypatch.setenv("RL_NSE_RPS", "")
        monkeypatch.setenv("RL_NSE_BURST", "")
        assert rrl._cfg("nse") == (1.0, 3)


# ─────────────────────────────── _Bucket ───────────────────────────────

class TestBucketInit:
    def test_starts_full(self, clock):
        b = make_bucket(clock, rps=2.0, capacity=7.0)
        assert b.tokens == 7.0
        assert b.waiters == 0
        assert b.throttle_events == 0
        assert b.last_wait_sec == 0.0

    def test_default_updated_is_a_timestamp(self):
        b = rrl._Bucket(rps=1.0, capacity=1.0)
        assert isinstance(b.updated, float)


class TestBucketAcquire:
    def test_immediate_when_tokens_available(self, clock):
        b = make_bucket(clock)
        assert b.acquire(weight=2) == 0.0
        assert b.tokens == 3.0
        assert b.throttle_events == 0
        assert b.last_wait_sec == 0.0
        assert clock.sleeps == []
        assert b.waiters == 0

    def test_default_weight_is_one(self, clock):
        b = make_bucket(clock)
        b.acquire()
        assert b.tokens == 4.0

    def test_blocks_until_refill(self, clock):
        b = make_bucket(clock, rps=10.0, capacity=5.0)
        b.acquire(weight=5)           # drain
        waited = b.acquire(weight=1)  # needs 0.1s of refill
        # deficit/rps = 0.1s but sleep is floored at 0.05 -> 0.1 rounds up via one 0.1 sleep
        assert clock.sleeps == [pytest.approx(0.1)]
        assert waited == pytest.approx(0.1)
        assert b.last_wait_sec == pytest.approx(0.1)
        assert b.throttle_events == 1  # waited > 0.05
        assert b.waiters == 0

    def test_short_wait_is_not_counted_as_throttle(self, clock):
        b = make_bucket(clock, rps=1000.0, capacity=1.0)
        b.acquire(weight=1)
        waited = b.acquire(weight=1)  # deficit tiny -> floored sleep of 0.05, waited == 0.05
        assert waited == pytest.approx(0.05)
        assert b.throttle_events == 0

    def test_sleep_is_capped_at_two_seconds(self, clock):
        b = make_bucket(clock, rps=0.1, capacity=1.0)
        b.acquire(weight=1)
        b.acquire(weight=1)  # needs 10s of refill, sleeps capped at 2s each
        assert clock.sleeps[0] == 2.0
        assert all(s <= 2.0 for s in clock.sleeps)
        assert sum(clock.sleeps) >= 10.0

    def test_waiters_counted_while_sleeping(self, clock):
        b = make_bucket(clock, rps=10.0, capacity=1.0)
        b.acquire(weight=1)
        seen = []
        clock.on_sleep = lambda s: seen.append(b.waiters)
        b.acquire(weight=1)
        assert seen and all(w == 1 for w in seen)
        assert b.waiters == 0

    def test_max_wait_exceeded_proceeds_and_debits(self, clock, caplog):
        b = make_bucket(clock, rps=0.01, capacity=1.0)
        b.acquire(weight=1)  # drain
        with caplog.at_level(logging.DEBUG, logger="rate-limiter"):
            waited = b.acquire(weight=5, max_wait=1.0)
        assert waited >= 1.0
        assert b.tokens == 0.0                # never goes negative
        assert b.waiters == 0
        assert any("max_wait exceeded" in r.getMessage() for r in caplog.records)

    def test_zero_rps_uses_half_second_poll(self, clock):
        b = make_bucket(clock, rps=0.0, capacity=1.0)
        b.acquire(weight=1)
        b.acquire(weight=1, max_wait=1.2)
        assert clock.sleeps[0] == 0.5
        assert b.tokens == 0.0

    def test_weight_larger_than_capacity_takes_the_whole_bucket_without_stalling(self, clock):
        # A request heavier than the burst can never be satisfied in full; it used to wait the
        # whole max_wait and then proceed anyway. It now means "the entire bucket".
        b = make_bucket(clock, rps=10.0, capacity=3.0)
        assert b.acquire(weight=10, max_wait=5.0) == 0.0
        assert b.tokens == 0.0
        assert clock.sleeps == []

    def test_oversized_weight_waits_only_for_the_bucket_to_fill(self, clock):
        b = make_bucket(clock, rps=1.0, capacity=3.0)
        b.acquire(weight=3)                              # drain
        assert b.acquire(weight=10, max_wait=60.0) == pytest.approx(3.0)
        assert clock.sleeps == [2.0, 1.0]
        assert b.tokens == 0.0 and b.waiters == 0

    def test_oversized_weight_is_noted_at_debug(self, clock, caplog):
        b = make_bucket(clock, rps=10.0, capacity=3.0)
        with caplog.at_level(logging.DEBUG, logger="rate-limiter"):
            b.acquire(weight=10)
        assert any("exceeds bucket capacity" in r.getMessage() for r in caplog.records)

    def test_weight_equal_to_capacity_is_not_logged_as_oversized(self, clock, caplog):
        b = make_bucket(clock, rps=10.0, capacity=3.0)
        with caplog.at_level(logging.DEBUG, logger="rate-limiter"):
            assert b.acquire(weight=3) == 0.0
        assert not any("exceeds bucket capacity" in r.getMessage() for r in caplog.records)

    def test_oversized_weight_is_still_bounded_when_nothing_refills(self, clock):
        b = make_bucket(clock, rps=0.0, capacity=3.0)
        b.acquire(weight=3)
        waited = b.acquire(weight=10, max_wait=1.0)
        assert waited >= 1.0 and b.tokens == 0.0

    def test_zero_capacity_is_left_alone(self, clock):
        b = make_bucket(clock, rps=1.0, capacity=0.0)
        assert b.acquire(weight=1, max_wait=1.0) >= 1.0
        assert b.tokens == 0.0

    def test_refill_never_exceeds_capacity(self, clock):
        b = make_bucket(clock, rps=10.0, capacity=5.0)
        b.acquire(weight=5)
        clock.now += 1000
        b.acquire(weight=1)
        assert b.tokens == 4.0

    def test_waiters_restored_after_exception(self, clock, monkeypatch):
        b = make_bucket(clock, rps=10.0, capacity=1.0)
        b.acquire(weight=1)

        def boom(_):
            raise RuntimeError("sleep failed")

        clock.on_sleep = boom
        with pytest.raises(RuntimeError):
            b.acquire(weight=1)
        assert b.waiters == 0


class TestBucketAllow:
    def test_true_consumes(self, clock):
        b = make_bucket(clock, capacity=3.0)
        assert b.allow() is True
        assert b.tokens == 2.0

    def test_false_when_empty_and_does_not_sleep(self, clock):
        b = make_bucket(clock, rps=1.0, capacity=2.0)
        assert b.allow(weight=2) is True
        assert b.allow() is False
        assert clock.sleeps == []
        assert b.tokens == 0.0

    def test_refills_with_time(self, clock):
        b = make_bucket(clock, rps=2.0, capacity=2.0)
        b.allow(weight=2)
        assert b.allow() is False
        clock.now += 0.5  # +1 token
        assert b.allow() is True
        assert b.allow() is False

    def test_denied_call_still_advances_refill_clock(self, clock):
        b = make_bucket(clock, rps=1.0, capacity=2.0)
        b.allow(weight=2)
        clock.now += 0.4
        assert b.allow() is False
        assert b.updated == clock.now
        assert b.tokens == pytest.approx(0.4)

    def test_weighted_allow(self, clock):
        b = make_bucket(clock, capacity=5.0)
        assert b.allow(weight=3) is True
        assert b.allow(weight=3) is False
        assert b.tokens == pytest.approx(2.0)  # a denied call consumes nothing
        assert b.allow(weight=2) is True

    def test_oversized_allow_takes_the_whole_bucket_when_full(self, clock):
        # allow(weight > capacity) used to be False forever (the gateway's _cb_get would then
        # raise CircuitOpenError for good); it now means "the whole bucket".
        b = make_bucket(clock, rps=1.0, capacity=5.0)
        assert b.allow(weight=6) is True
        assert b.tokens == 0.0

    def test_oversized_allow_is_denied_while_the_bucket_is_not_full(self, clock):
        b = make_bucket(clock, rps=1.0, capacity=5.0)
        b.allow(weight=1)
        assert b.allow(weight=6) is False
        assert b.tokens == pytest.approx(4.0)  # denied -> nothing consumed

    def test_zero_capacity_allow_is_left_alone(self, clock):
        b = make_bucket(clock, rps=1.0, capacity=0.0)
        assert b.allow(weight=1) is False


class TestBucketWaitBudget:
    def test_zero_when_available(self, clock):
        b = make_bucket(clock, capacity=3.0)
        assert b.wait_budget_sec() == 0.0

    def test_deficit_over_rps(self, clock):
        b = make_bucket(clock, rps=4.0, capacity=2.0)
        b.allow(weight=2)
        assert b.wait_budget_sec(weight=1) == pytest.approx(0.25)
        assert b.wait_budget_sec(weight=2) == pytest.approx(0.5)

    def test_accounts_for_elapsed_time(self, clock):
        b = make_bucket(clock, rps=4.0, capacity=2.0)
        b.allow(weight=2)
        clock.now += 0.25  # +1 token
        assert b.wait_budget_sec(weight=1) == 0.0

    def test_does_not_consume_or_update(self, clock):
        b = make_bucket(clock, rps=1.0, capacity=2.0)
        b.allow(weight=2)
        before_updated = b.updated
        clock.now += 0.3
        b.wait_budget_sec(weight=1)
        assert b.tokens == 0.0
        assert b.updated == before_updated

    def test_zero_rps_returns_half_second(self, clock):
        b = make_bucket(clock, rps=0.0, capacity=1.0)
        b.allow()
        assert b.wait_budget_sec() == 0.5

    def test_oversized_weight_budget_is_time_to_fill_not_to_an_impossible_target(self, clock):
        # wait_budget_sec(weight > capacity) used to report deficit against a target the bucket
        # can never reach (10 tokens vs capacity 2 -> 2.5s even when the bucket is full).
        b = make_bucket(clock, rps=4.0, capacity=2.0)
        assert b.wait_budget_sec(weight=10) == 0.0       # full bucket: granted immediately
        b.allow(weight=2)
        assert b.wait_budget_sec(weight=10) == pytest.approx(0.5)   # 2 tokens at 4/s

    def test_zero_capacity_budget_is_left_alone(self, clock):
        b = make_bucket(clock, rps=1.0, capacity=0.0)
        assert b.wait_budget_sec(weight=1) == pytest.approx(1.0)


class TestBucketSnapshot:
    def test_shape_and_rounding(self, clock):
        b = make_bucket(clock, rps=2.0, capacity=4.0)
        b.tokens = 1.23456
        b.waiters = 2
        b.throttle_events = 3
        b.last_wait_sec = 0.98765
        assert b.snapshot() == {
            "rps": 2.0,
            "capacity": 4.0,
            "tokens_available": 1.23,
            "waiters": 2,
            "throttle_events": 3,
            "last_wait_sec": 0.99,
        }


class TestBucketThreadSafety:
    def test_concurrent_allow_never_over_grants(self):
        # Real clock; rps so small that no refill happens during the test.
        b = rrl._Bucket(rps=0.0001, capacity=10.0)
        results = []
        res_lock = threading.Lock()

        def worker():
            r = b.allow()
            with res_lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count(True) == 10
        assert results.count(False) == 30

    def test_real_clock_acquire_blocks_briefly(self):
        b = rrl._Bucket(rps=1000.0, capacity=1.0)
        b.acquire(weight=1)
        waited = b.acquire(weight=1)  # floored 0.05s sleep
        assert 0.04 <= waited < 1.0


# ─────────────────────────────── registry ───────────────────────────────

class TestGetBucket:
    def test_creates_once_and_reuses(self):
        a = rrl._get_bucket("gemini")
        assert rrl._get_bucket("gemini") is a
        assert rrl._buckets["gemini"] is a

    def test_uses_provider_config(self):
        b = rrl._get_bucket("yfinance")
        assert (b.rps, b.capacity) == (2.0, 6.0)
        assert b.tokens == 6.0

    def test_capacity_is_float(self):
        assert isinstance(rrl._get_bucket("nse").capacity, float)

    def test_env_override_applies_at_creation(self, monkeypatch):
        monkeypatch.setenv("RL_NSE_RPS", "9")
        monkeypatch.setenv("RL_NSE_BURST", "12")
        b = rrl._get_bucket("nse")
        assert (b.rps, b.capacity) == (9.0, 12.0)

    def test_env_change_after_creation_is_not_picked_up(self, monkeypatch):
        b = rrl._get_bucket("nse")
        monkeypatch.setenv("RL_NSE_RPS", "99")
        assert rrl._get_bucket("nse") is b
        assert b.rps == 1.0

    def test_providers_are_independent(self):
        assert rrl._get_bucket("nse") is not rrl._get_bucket("gemini")


# ─────────────────────────────── module functions ───────────────────────────────

class TestAcquireFn:
    def test_returns_wait_and_debits_shared_bucket(self, clock):
        b = rrl._get_bucket("gemini")
        b.updated = clock.now
        assert rrl.acquire("gemini", weight=2) == 0.0
        assert b.tokens == 4.0

    def test_blocks_when_drained(self, clock):
        b = rrl._get_bucket("nse")  # 1 rps, burst 3
        b.updated = clock.now
        rrl.acquire("nse", weight=3)
        waited = rrl.acquire("nse", weight=1)
        assert waited == pytest.approx(1.0)

    def test_max_wait_is_forwarded(self, clock):
        b = rrl._get_bucket("nse")
        b.updated = clock.now
        rrl.acquire("nse", weight=3)
        waited = rrl.acquire("nse", weight=100, max_wait=2.0)
        assert 2.0 <= waited < 5.0

    def test_fails_open_on_internal_error(self, monkeypatch, caplog):
        def boom(_):
            raise RuntimeError("bucket broke")

        monkeypatch.setattr(rrl, "_get_bucket", boom)
        with caplog.at_level(logging.DEBUG, logger="rate-limiter"):
            assert rrl.acquire("nse") == 0.0
        assert any("failed open" in r.getMessage() for r in caplog.records)


class TestSuggestedTimeout:
    def test_no_waiters_returns_base(self):
        assert rrl.suggested_timeout(25.0, "yfinance") == 25.0

    def test_scales_with_waiters(self):
        b = rrl._get_bucket("yfinance")
        b.waiters = 3
        assert rrl.suggested_timeout(10.0, "yfinance") == pytest.approx(15.0)

    def test_caps_at_double(self):
        b = rrl._get_bucket("yfinance")
        b.waiters = 6
        assert rrl.suggested_timeout(10.0, "yfinance") == pytest.approx(20.0)
        b.waiters = 600
        assert rrl.suggested_timeout(10.0, "yfinance") == pytest.approx(20.0)

    def test_floor_applies(self):
        assert rrl.suggested_timeout(0.2, "yfinance") == 1.0
        assert rrl.suggested_timeout(0.2, "yfinance", floor=0.5) == 0.5

    def test_error_returns_base_timeout(self, monkeypatch):
        def boom(_):
            raise RuntimeError("bucket broke")

        monkeypatch.setattr(rrl, "_get_bucket", boom)
        assert rrl.suggested_timeout(7.5, "yfinance") == 7.5


class TestAllowFn:
    def test_true_then_false(self, clock):
        b = rrl._get_bucket("nse")  # burst 3
        b.updated = clock.now
        assert [rrl.allow("nse") for _ in range(4)] == [True, True, True, False]

    def test_weight_forwarded(self, clock):
        b = rrl._get_bucket("nse")
        b.updated = clock.now
        assert rrl.allow("nse", weight=2) is True
        assert rrl.allow("nse", weight=2) is False      # only 1 token left
        assert rrl.allow("nse", weight=1) is True

    def test_oversized_weight_forwarded_means_the_whole_bucket(self, clock):
        b = rrl._get_bucket("nse")                       # nse burst is 3
        b.updated = clock.now
        assert rrl.allow("nse", weight=4) is True
        assert b.tokens == 0.0

    def test_fails_open(self, monkeypatch, caplog):
        def boom(_):
            raise RuntimeError("bucket broke")

        monkeypatch.setattr(rrl, "_get_bucket", boom)
        with caplog.at_level(logging.DEBUG, logger="rate-limiter"):
            assert rrl.allow("nse") is True
        assert any("failed open" in r.getMessage() for r in caplog.records)


class TestWaitBudgetFn:
    def test_zero_when_available(self):
        assert rrl.wait_budget_sec("nse") == 0.0

    def test_positive_when_drained(self, clock):
        b = rrl._get_bucket("nse")
        b.updated = clock.now
        rrl.allow("nse", weight=3)
        assert rrl.wait_budget_sec("nse") == pytest.approx(1.0)
        assert rrl.wait_budget_sec("nse", weight=2) == pytest.approx(2.0)

    def test_fails_open_to_zero(self, monkeypatch):
        def boom(_):
            raise RuntimeError("bucket broke")

        monkeypatch.setattr(rrl, "_get_bucket", boom)
        assert rrl.wait_budget_sec("nse") == 0.0


class TestStats:
    def test_empty(self):
        assert rrl.stats() == {}

    def test_reports_every_touched_provider(self):
        rrl.allow("nse")
        rrl.allow("gemini", weight=2)
        s = rrl.stats()
        assert set(s) == {"nse", "gemini"}
        assert s["nse"]["capacity"] == 3.0
        assert s["nse"]["tokens_available"] == pytest.approx(2.0, abs=0.05)
        assert s["gemini"]["rps"] == 2.0

    def test_untouched_provider_absent(self):
        rrl.allow("nse")
        assert "gemini" not in rrl.stats()


# ─────────────────────────────── LocalMemoryRateLimiter ───────────────────────────────

class TestLocalMemoryRateLimiter:
    def test_singleton_and_alias(self):
        assert isinstance(rrl.limiter, rrl.LocalMemoryRateLimiter)
        assert rrl.rate_limiter is rrl.limiter

    def test_set_redis_stores_client_and_returns_none(self):
        lim = rrl.LocalMemoryRateLimiter()
        assert lim._redis is None
        client = object()
        assert lim.set_redis(client) is None
        assert lim._redis is client

    def test_set_redis_accepts_none(self):
        lim = rrl.LocalMemoryRateLimiter()
        lim.set_redis(object())
        assert lim.set_redis() is None
        assert lim._redis is None

    def test_methods_exist_that_main_py_calls(self):
        # Regression for "'LocalMemoryRateLimiter' object has no attribute 'allow'".
        for name in ("set_redis", "allow", "wait_budget_sec", "acquire", "suggested_timeout", "stats"):
            assert callable(getattr(rrl.limiter, name)), name

    def test_allow_delegates(self, clock):
        b = rrl._get_bucket("nse")
        b.updated = clock.now
        lim = rrl.LocalMemoryRateLimiter()
        assert [lim.allow("nse") for _ in range(4)] == [True, True, True, False]
        assert lim.allow("nse", 0) is True  # zero weight always passes

    def test_wait_budget_delegates(self, clock):
        b = rrl._get_bucket("nse")
        b.updated = clock.now
        lim = rrl.LocalMemoryRateLimiter()
        lim.allow("nse", 3)
        assert lim.wait_budget_sec("nse") == pytest.approx(1.0)

    def test_acquire_delegates(self, clock):
        b = rrl._get_bucket("nse")
        b.updated = clock.now
        lim = rrl.LocalMemoryRateLimiter()
        assert lim.acquire("nse", 1, 5.0) == 0.0
        assert b.tokens == 2.0

    def test_suggested_timeout_delegates(self):
        rrl._get_bucket("nse").waiters = 6
        assert rrl.LocalMemoryRateLimiter().suggested_timeout(10.0, "nse") == pytest.approx(20.0)
        assert rrl.LocalMemoryRateLimiter().suggested_timeout(0.1, "nse", floor=3.0) == 3.0

    def test_stats_delegates(self):
        lim = rrl.LocalMemoryRateLimiter()
        lim.allow("nse")
        assert "nse" in lim.stats()

    def test_instances_share_module_level_buckets(self, clock):
        b = rrl._get_bucket("nse")
        b.updated = clock.now
        a, c = rrl.LocalMemoryRateLimiter(), rrl.LocalMemoryRateLimiter()
        a.allow("nse", 3)
        assert c.allow("nse") is False

    def test_main_py_style_internal_gate(self, clock):
        # Mirrors main.py _cb_get(): allow -> sleep(wait_budget) -> allow again.
        b = rrl._get_bucket("nse")
        b.updated = clock.now
        lim = rrl.limiter
        for _ in range(3):
            assert lim.allow("nse")
        assert not lim.allow("nse")
        clock.now += min(2.0, lim.wait_budget_sec("nse"))
        assert lim.allow("nse")
