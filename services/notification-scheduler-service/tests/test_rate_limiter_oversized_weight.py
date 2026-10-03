"""scheduler/rate_limiter.py: a request heavier than the bucket must not stall for the whole max_wait.

Tokens are capped at `capacity`, so `tokens >= weight` could never become true for a heavier weight:
acquire() slept out all of max_wait (20s default) on every call and then "proceeded anyway". An oversized
weight now means "the whole bucket": wait for it to fill, drain it, return.

The module is loaded fresh from its file (like test_rate_limiter_env.py) and gets a fake clock.

Run from services/notification-scheduler-service:
    python -m pytest tests/test_rate_limiter_oversized_weight.py -v
"""
from __future__ import annotations

import importlib.util
import itertools
import logging
import os
import sys
import types

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scheduler", "rate_limiter.py")
_counter = itertools.count()


def _load():
    name = f"nss_rate_limiter_ow_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(name, _PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod            # @dataclass needs the module registered while the class body runs
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(name, None)
    return mod


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


@pytest.fixture
def rl(monkeypatch):
    mod = _load()
    clk = FakeClock()
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(time=clk.time, sleep=clk.sleep))
    mod._clk = clk
    return mod


def _bucket(rl, rps, capacity, tokens=None):
    b = rl._Bucket(rps=rps, capacity=float(capacity))
    b.updated = rl._clk.now            # the dataclass default factory captured the real time.time
    if tokens is not None:
        b.tokens = tokens
    return b


class TestOversizedWeight:
    def test_full_bucket_is_granted_immediately(self, rl):
        b = _bucket(rl, 2.0, 6)
        assert b.acquire(weight=50, max_wait=20.0) == 0.0
        assert b.tokens == 0.0 and rl._clk.sleeps == []
        assert b.throttle_events == 0

    def test_waits_only_for_the_bucket_to_fill(self, rl):
        b = _bucket(rl, 2.0, 6, tokens=0.0)
        assert b.acquire(weight=50, max_wait=20.0) == 3.0     # 6 tokens at 2/s, not the 20s budget
        assert rl._clk.sleeps == [2.0, 1.0]
        assert b.tokens == 0.0 and b.waiters == 0
        assert b.throttle_events == 1 and b.last_wait_sec == 3.0

    def test_just_above_capacity_is_capped_too(self, rl):
        b = _bucket(rl, 2.0, 6)
        assert b.acquire(weight=6.5) == 0.0 and b.tokens == 0.0

    def test_weight_equal_to_capacity_is_unchanged(self, rl, caplog):
        b = _bucket(rl, 2.0, 6)
        with caplog.at_level(logging.DEBUG):
            assert b.acquire(weight=6) == 0.0
        assert not any("exceeds bucket capacity" in r.getMessage() for r in caplog.records)

    def test_oversized_weight_is_noted_at_debug(self, rl, caplog):
        b = _bucket(rl, 2.0, 6)
        with caplog.at_level(logging.DEBUG):
            b.acquire(weight=50)
        recs = [r for r in caplog.records if "exceeds bucket capacity" in r.getMessage()]
        assert len(recs) == 1 and recs[0].levelno == logging.DEBUG

    def test_still_bounded_when_nothing_refills(self, rl):
        b = _bucket(rl, 0.0, 6, tokens=1.0)
        waited = b.acquire(weight=50, max_wait=1.0)
        assert waited == 1.0 and rl._clk.sleeps == [0.5, 0.5]
        assert b.tokens == 0.0                                 # give-up path drains, as before

    def test_zero_capacity_is_left_alone(self, rl):
        b = _bucket(rl, 1.0, 0)
        assert b.acquire(weight=1, max_wait=1.0) == 1.0 and b.tokens == 0.0

    def test_module_acquire_accepts_oversized_weight(self, rl):
        rl._buckets["yfinance"] = _bucket(rl, 2.0, 6)
        assert rl.acquire("yfinance", weight=50) == 0.0

    def test_ordinary_weights_still_wait_for_refill(self, rl):
        b = _bucket(rl, 1.0, 2, tokens=0.0)
        assert b.acquire(weight=1, max_wait=5) == 1.0
        assert rl._clk.sleeps == [1.0]
