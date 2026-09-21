"""
tests/test_rt_circuit_breaker.py — offline tests for resilience/circuit_breaker.py
(real-trade-service): the CircuitBreaker class, its alerts, and call() fallback
behaviour. Pure asyncio.run() so no pytest-asyncio dependency.

Run from services/real-trade-service:
    python3 -m pytest tests/test_rt_circuit_breaker.py -q --cov=resilience --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from resilience import circuit_breaker as cb


@pytest.fixture()
def rig(monkeypatch):
    now = [1000.0]
    alerts: list[str] = []
    monkeypatch.setattr(cb, "time", types.SimpleNamespace(monotonic=lambda: now[0]))

    async def fake_notify(text):
        alerts.append(text)
    monkeypatch.setattr(cb, "notify_async", fake_notify)
    return now, alerts


def run(coro):
    """Run a coroutine, then let any fire-and-forget alert tasks finish."""
    async def _wrap():
        r = await coro
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return r
    return asyncio.run(_wrap())


def fail_n(b, n, alerts_loop=True):
    async def go():
        for _ in range(n):
            b.record_failure()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    asyncio.run(go())


# ── state machine ────────────────────────────────────────────────────────────
class TestStateMachine:
    def test_new_breaker_is_closed(self, rig):
        b = cb.CircuitBreaker("svc", failure_threshold=3, cooldown_s=60.0)
        assert b.is_open is False
        d = b.to_dict()
        assert d == {"name": "svc", "state": "closed", "consecutive_failures": 0, "failure_threshold": 3,
                     "cooldown_s": 60.0, "seconds_until_retry": None}

    def test_opens_at_the_threshold_not_before(self, rig):
        b = cb.CircuitBreaker("svc", 3, 60.0)
        fail_n(b, 2)
        assert b.is_open is False
        fail_n(b, 1)
        assert b.is_open is True and b.to_dict()["state"] == "open"

    def test_open_countdown_then_half_open_at_exactly_the_cooldown(self, rig):
        now, _ = rig
        b = cb.CircuitBreaker("svc", 1, 60.0)
        fail_n(b, 1)
        now[0] += 59.9
        assert b.is_open is True and b.to_dict()["seconds_until_retry"] == pytest.approx(0.1)
        now[0] += 0.1
        assert b.is_open is False
        d = b.to_dict()
        assert d["state"] == "half_open" and d["seconds_until_retry"] == 0.0

    def test_failed_probe_rearms_the_cooldown(self, rig):
        now, _ = rig
        b = cb.CircuitBreaker("svc", 1, 60.0)
        fail_n(b, 1)
        now[0] += 61.0
        fail_n(b, 1)                                              # probe fails
        assert b.is_open is True and b.to_dict()["consecutive_failures"] == 2
        assert b.to_dict()["seconds_until_retry"] == pytest.approx(60.0)

    def test_successful_probe_closes_and_resets(self, rig):
        now, _ = rig
        b = cb.CircuitBreaker("svc", 1, 60.0)
        fail_n(b, 1)
        now[0] += 61.0
        b.record_success()
        assert b.is_open is False and b.to_dict()["state"] == "closed" and b._failures == 0

    def test_success_resets_a_sub_threshold_streak(self, rig):
        b = cb.CircuitBreaker("svc", 3, 60.0)
        fail_n(b, 2)
        b.record_success()
        fail_n(b, 2)
        assert b.is_open is False


# ── alerts ───────────────────────────────────────────────────────────────────
class TestAlerts:
    def test_down_alert_fires_once_when_the_breaker_opens(self, rig):
        _, alerts = rig
        b = cb.CircuitBreaker("api-gateway", 3, 120.0)
        fail_n(b, 3)
        assert len(alerts) == 1 and "api-gateway is DOWN" in alerts[0] and "3 consecutive" in alerts[0]

    def test_no_alert_below_the_threshold(self, rig):
        _, alerts = rig
        fail_n(cb.CircuitBreaker("svc", 5, 60.0), 4)
        assert alerts == []

    def test_recovery_alert_only_if_it_was_actually_open(self, rig):
        now, alerts = rig
        b = cb.CircuitBreaker("svc", 3, 60.0)
        fail_n(b, 2)                                              # never opened
        async def ok():
            b.record_success()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        asyncio.run(ok())
        assert alerts == []                                       # the historic "recovered" false alarm
        fail_n(b, 3)
        assert len(alerts) == 1
        now[0] += 61.0
        asyncio.run(ok())
        assert len(alerts) == 2 and "recovered" in alerts[1]

    def test_failed_probe_alert(self, rig):
        now, alerts = rig
        b = cb.CircuitBreaker("svc", 1, 60.0)
        fail_n(b, 1)
        now[0] += 61.0
        fail_n(b, 1)
        assert len(alerts) == 2 and "probe failed again" in alerts[1]

    def test_alerting_without_an_event_loop_is_silent_and_safe(self, rig):
        _, alerts = rig
        b = cb.CircuitBreaker("svc", 1, 60.0)
        b.record_failure()                                        # sync: no running loop
        b.record_success()
        assert b.is_open is False and alerts == []

    def test_alert_dispatch_error_never_reaches_the_caller(self, rig, monkeypatch):
        def boom():
            raise ValueError("loop exploded")
        monkeypatch.setattr(cb.asyncio, "get_running_loop", boom)
        b = cb.CircuitBreaker("svc", 1, 60.0)
        b.record_failure()
        assert b.is_open is True


# ── call() ───────────────────────────────────────────────────────────────────
class TestCall:
    def test_success_passes_the_result_through_and_counts_as_success(self, rig):
        b = cb.CircuitBreaker("svc", 3, 60.0)
        b._failures = 2

        async def fn(x, y=0):
            return x + y
        assert run(b.call(fn, 1, y=2)) == 3 and b._failures == 0

    def test_failure_uses_the_fallback_and_records_it(self, rig):
        b = cb.CircuitBreaker("svc", 3, 60.0)

        async def fn():
            raise RuntimeError("upstream 503")

        async def fallback():
            return "cached"
        assert run(b.call(fn, fallback=fallback)) == "cached" and b._failures == 1

    def test_failure_without_a_fallback_returns_none(self, rig):
        b = cb.CircuitBreaker("svc", 3, 60.0)

        async def fn():
            raise RuntimeError("x")
        assert run(b.call(fn)) is None

    def test_open_breaker_skips_the_call_entirely(self, rig):
        b = cb.CircuitBreaker("svc", 1, 60.0)
        fail_n(b, 1)
        called = []

        async def fn():
            called.append(1)
            return "live"

        async def fallback():
            return "cached"
        assert run(b.call(fn, fallback=fallback)) == "cached" and called == []

    def test_open_breaker_without_fallback_returns_none(self, rig):
        b = cb.CircuitBreaker("svc", 1, 60.0)
        fail_n(b, 1)
        assert run(b.call(lambda: None)) is None

    def test_half_open_probe_success_closes_the_breaker(self, rig):
        now, _ = rig
        b = cb.CircuitBreaker("svc", 1, 60.0)
        fail_n(b, 1)
        now[0] += 61.0

        async def fn():
            return "live"
        assert run(b.call(fn)) == "live" and b.to_dict()["state"] == "closed"

    def test_half_open_probe_failure_reopens_it(self, rig):
        now, _ = rig
        b = cb.CircuitBreaker("svc", 1, 60.0)
        fail_n(b, 1)
        now[0] += 61.0

        async def fn():
            raise RuntimeError("still down")

        async def fallback():
            return "cached"
        assert run(b.call(fn, fallback=fallback)) == "cached" and b.is_open is True

    def test_trips_after_consecutive_call_failures(self, rig):
        b = cb.CircuitBreaker("svc", 3, 60.0)

        async def fn():
            raise RuntimeError("x")
        for _ in range(3):
            run(b.call(fn))
        assert b.is_open is True


def test_module_singletons_have_the_documented_settings():
    assert (cb.api_gateway_breaker.name, cb.api_gateway_breaker.failure_threshold,
            cb.api_gateway_breaker.cooldown_s) == ("api-gateway", 10, 120.0)
    assert (cb.market_data_breaker.name, cb.market_data_breaker.failure_threshold,
            cb.market_data_breaker.cooldown_s) == ("market-data-service", 10, 60.0)
    assert (cb.event_service_breaker.name, cb.event_service_breaker.failure_threshold,
            cb.event_service_breaker.cooldown_s) == ("event-service", 10, 90.0)
