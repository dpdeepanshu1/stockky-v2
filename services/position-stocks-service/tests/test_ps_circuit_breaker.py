"""
tests/test_ps_circuit_breaker.py — offline tests for resilience/circuit_breaker.py
(position-stocks-service). Module-level singleton state, so every test resets it
and drives time through a fake monotonic clock.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_ps_circuit_breaker.py -q --cov=resilience --cov-report=term-missing
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from resilience import circuit_breaker as cb


@pytest.fixture()
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cb, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(cb, "_failure_count", 0)
    monkeypatch.setattr(cb, "_open_since", 0.0)
    return now


def trip():
    for _ in range(cb._FAILURE_THRESHOLD):
        cb.record_failure()


def test_starts_closed(clock):
    assert cb.is_open() is False
    s = cb.status()
    assert s["state"] == "closed" and s["consecutive_failures"] == 0 and s["seconds_until_retry"] is None


def test_status_has_the_shape_the_dashboard_reads(clock):
    assert set(cb.status()) == {"state", "consecutive_failures", "failure_threshold",
                                "cooldown_s", "seconds_until_retry"}


def test_stays_closed_below_the_threshold(clock):
    for _ in range(cb._FAILURE_THRESHOLD - 1):
        cb.record_failure()
    assert cb.is_open() is False and cb.status()["consecutive_failures"] == 4


def test_opens_exactly_at_the_threshold(clock):
    trip()
    assert cb.is_open() is True
    s = cb.status()
    assert s["state"] == "open" and s["consecutive_failures"] == 5
    assert s["seconds_until_retry"] == pytest.approx(60.0)


def test_countdown_runs_down_while_open(clock):
    trip()
    clock[0] += 45.0
    assert cb.is_open() is True and cb.status()["seconds_until_retry"] == pytest.approx(15.0)


def test_half_open_exactly_when_the_cooldown_elapses(clock):
    trip()
    clock[0] += 59.999
    assert cb.is_open() is True
    clock[0] += 0.001
    assert cb.is_open() is False
    s = cb.status()
    assert s["state"] == "half_open" and s["seconds_until_retry"] == 0.0
    assert s["consecutive_failures"] == 5                    # NOT reset to zero by merely waiting


def test_failed_probe_rearms_a_full_cooldown(clock):
    trip()
    clock[0] += 61.0                                         # half-open
    cb.record_failure()                                      # the probe fails
    assert cb.is_open() is True and cb.status()["consecutive_failures"] == 6
    assert cb.status()["seconds_until_retry"] == pytest.approx(60.0)
    clock[0] += 61.0
    assert cb.is_open() is False                             # and it half-opens again after another window


def test_repeated_failed_probes_never_leave_it_stuck_half_open(clock):
    trip()
    for _ in range(3):
        clock[0] += 61.0
        assert cb.is_open() is False
        cb.record_failure()
        assert cb.is_open() is True


def test_successful_probe_closes_and_resets(clock):
    trip()
    clock[0] += 61.0
    cb.record_success()
    assert cb.is_open() is False
    s = cb.status()
    assert s["state"] == "closed" and s["consecutive_failures"] == 0 and s["seconds_until_retry"] is None


def test_a_success_resets_a_partial_failure_streak(clock):
    for _ in range(4):
        cb.record_failure()
    cb.record_success()
    cb.record_failure()
    assert cb.is_open() is False and cb.status()["consecutive_failures"] == 1


def test_needs_a_fresh_full_streak_to_trip_again(clock):
    trip()
    clock[0] += 61.0
    cb.record_success()
    for _ in range(cb._FAILURE_THRESHOLD - 1):
        cb.record_failure()
    assert cb.is_open() is False
    cb.record_failure()
    assert cb.is_open() is True


def test_elapsed_helper_is_zero_when_never_tripped(clock):
    assert cb._elapsed_since_open() == 0.0


def test_countdown_never_goes_negative_once_half_open(clock):
    trip()
    clock[0] += 500.0
    assert cb.status()["seconds_until_retry"] == 0.0
