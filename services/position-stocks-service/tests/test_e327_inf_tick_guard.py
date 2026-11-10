"""
E327 - an infinite tick must never look like "above target" / "below stop" to the position-stocks watchdogs.

`_last_tick` only refused ltp <= 0 (NaN already fell out, inf did not), so an inf tick made scaleout's
`ltp >= target` true and sold the kept half at market. `is_breached`, scaleout `_num` and `run_half_sell` now also
require a finite price. Defensive: the websocket feed is binary and should never produce inf.

Run from services/position-stocks-service:
    python -m pytest tests/test_e327_inf_tick_guard.py -q
"""
from __future__ import annotations

import os
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from orders import stop_watchdog as sw
from orders import scaleout as so

NAN, INF = float("nan"), float("inf")


def _feed(monkeypatch, buf):
    fake = types.ModuleType("feed")
    fake.ws_client = types.SimpleNamespace(get_tick_buffer=lambda sym: buf)
    monkeypatch.setitem(sys.modules, "feed", fake)


@pytest.mark.parametrize("bad", [INF, -INF, NAN, 0, -1.0])
def test_last_tick_refuses_unusable_prices(monkeypatch, bad):
    _feed(monkeypatch, [(time.time(), bad)])
    ltp, age = sw._last_tick("ABC")
    assert ltp is None


def test_last_tick_returns_a_real_price(monkeypatch):
    _feed(monkeypatch, [(time.time(), 101.5)])
    ltp, age = sw._last_tick("ABC")
    assert ltp == 101.5 and age is not None and age < 5


@pytest.mark.parametrize("ltp,stop", [(INF, 100.0), (NAN, 100.0), (99.0, INF), (99.0, NAN), (-INF, 100.0)])
def test_is_breached_false_on_non_finite(ltp, stop):
    assert sw.is_breached(ltp, stop) is False
    assert sw.is_breached(ltp, stop, short=True) is False


def test_is_breached_still_true_for_a_real_breach():
    assert sw.is_breached(90.0, 100.0) is True
    assert sw.is_breached(110.0, 100.0, short=True) is True


@pytest.mark.parametrize("bad", [INF, -INF, NAN, 0, -1, "x", None])
def test_scaleout_num_rejects_non_finite(bad):
    assert so._num(bad) is None


def test_scaleout_num_accepts_a_real_price():
    assert so._num("101.5") == 101.5
