"""tests/test_rate_limiter_reserve_zone.py — group 66.

A background caller (reserve > 0) can never hold more than `capacity - reserve` tokens. A weight
between `capacity - reserve` and `capacity` was not treated as oversized (the test was
`weight > capacity`), so it waited out the whole budget on every call and then proceeded anyway, or,
with fail_fast, was skipped forever. It is now capped at `capacity - reserve` like any oversized
weight. Interactive callers (reserve 0) and weights within the usable part are unchanged; when the
reserve leaves nothing (reserve >= capacity) behaviour is unchanged (still denied).

Run from services/api-gateway:
    python3 -m pytest tests/test_rate_limiter_reserve_zone.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD = os.path.join(os.path.dirname(_HERE), "rate_limiter.py")


class _Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


@pytest.fixture
def rl(monkeypatch):
    name = "rate_limiter_reserve_zone_under_test"
    spec = importlib.util.spec_from_file_location(name, _MOD)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)   # @dataclass looks its module up in sys.modules
    spec.loader.exec_module(mod)
    clock = _Clock()
    monkeypatch.setattr(mod, "time", clock)
    mod._test_clock = clock
    return mod


def _bucket(rl, rps, capacity, tokens):
    b = rl._Bucket(rps=rps, capacity=float(capacity))
    b.tokens = float(tokens)
    b.updated = rl._test_clock.now
    return b


@pytest.mark.parametrize("weight", [6.5, 7, 7.9, 8])
def test_weight_between_usable_and_capacity_is_granted_when_full(rl, weight):
    b = _bucket(rl, 1, 8, 8.0)
    assert b.acquire(weight=weight, reserve=2.0, max_wait=1.0, fail_fast=True) == 0.0
    assert b.tokens == 2.0                      # reserve untouched
    assert b.denied_events == 0 and rl._test_clock.sleeps == []


def test_weight_in_the_zone_waits_only_for_the_usable_part(rl):
    b = _bucket(rl, 1, 8, 2.0)                  # at the reserve line: 4 more tokens needed...
    assert b.acquire(weight=7, reserve=2.0, max_wait=20) == 6.0   # ...to reach capacity-reserve = 6
    assert b.tokens == 2.0


def test_weight_in_the_zone_still_gives_up_when_the_budget_runs_out(rl):
    b = _bucket(rl, 0.0, 8, 3.0)                # no refill: bounded by the budget, not stuck
    assert b.acquire(weight=7, reserve=2.0, max_wait=1.0, fail_fast=True) == -1.0
    assert b.denied_events == 1 and b.tokens == 3.0


def test_weight_exactly_usable_is_not_capped(rl):
    b = _bucket(rl, 1, 8, 8.0)
    assert b.acquire(weight=6, reserve=2.0) == 0.0
    assert b.tokens == 2.0


def test_interactive_callers_are_unchanged(rl):
    b = _bucket(rl, 1, 8, 8.0)
    assert b.acquire(weight=7) == 0.0 and b.tokens == 1.0   # no reserve: full weight taken


def test_reserve_that_leaves_nothing_is_unchanged(rl):
    b = _bucket(rl, 1, 4, 4.0)
    assert b.acquire(weight=3, reserve=4.0, max_wait=1.0, fail_fast=True) == -1.0
    assert b.acquire(weight=3, reserve=5.0, max_wait=1.0, fail_fast=True) == -1.0
    assert b.tokens == 4.0


def test_zone_debug_note_names_the_usable_capacity(rl, monkeypatch):
    seen = []

    class _Log:
        def debug(self, msg, *a):
            seen.append(msg % a)

        def warning(self, msg, *a):
            seen.append(msg % a)

    monkeypatch.setattr(rl, "logger", _Log())
    _bucket(rl, 1, 8, 8.0).acquire(weight=7, reserve=2.0)
    assert seen == ["rate_limiter: weight 7 exceeds usable capacity 6.0 (capacity 8.0 minus reserve 2.0), treating as 6.0"]
