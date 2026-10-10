"""group299: one fresh fundamentals computation per symbol at a time (real threads)."""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main as m


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("FUNDAMENTALS_SINGLE_FLIGHT", raising=False)
    monkeypatch.delenv("FUNDAMENTALS_JOIN_WAIT_S", raising=False)
    m._FUND_FLIGHTS.clear()


class Fake:
    """Stand-in for _get_fundamentals_compute with a cache that honours `force`, like the real one."""

    def __init__(self, work_s=0.3, fail_first=False):
        self.cache = {}
        self.fresh = 0
        self.hits = 0
        self.forces = []
        self.work_s = work_s
        self.fail_first = fail_first
        self._lock = threading.Lock()

    def __call__(self, symbol, force=False):
        sym = m.normalize_symbol(symbol)
        with self._lock:
            self.forces.append(force)
            if not force and sym in self.cache:
                self.hits += 1
                return self.cache[sym]
            self.fresh += 1
            n = self.fresh
        time.sleep(self.work_s)
        if self.fail_first and n == 1:
            raise RuntimeError("yahoo down")
        self.cache[sym] = {"symbol": sym, "n": n}
        return self.cache[sym]


def run_threads(calls):
    out, errs = [None] * len(calls), [None] * len(calls)

    def work(i, c):
        try:
            out[i] = m._get_fundamentals_inner(*c[0], **c[1])
        except Exception as e:  # noqa: BLE001
            errs[i] = e
    ts = [threading.Thread(target=work, args=(i, c)) for i, c in enumerate(calls)]
    for t in ts:
        t.start()
        time.sleep(0.03)            # the first caller is clearly first
    for t in ts:
        t.join()
    return out, errs


def patch(monkeypatch, fake):
    monkeypatch.setattr(m, "_get_fundamentals_compute", fake)
    return fake


def test_two_callers_for_one_symbol_cost_one_computation(monkeypatch):
    f = patch(monkeypatch, Fake())
    out, errs = run_threads([(("ASTERDM.NS",), {}), (("ASTERDM.NS",), {"force": False})])
    assert errs == [None, None] and f.fresh == 1 and f.hits == 1
    assert out[0] == out[1] and out[0]["n"] == 1


def test_spellings_of_one_symbol_share_the_flight(monkeypatch):
    f = patch(monkeypatch, Fake())
    run_threads([(("INFY",), {}), (("INFY.NS",), {}), (("infy",), {})])
    assert f.fresh == 1 and f.hits == 2


def test_different_symbols_do_not_wait_for_each_other(monkeypatch):
    f = patch(monkeypatch, Fake(work_s=0.4))
    t0 = time.monotonic()
    run_threads([(("AAA",), {}), (("BBB",), {}), (("CCC",), {})])
    assert f.fresh == 3 and time.monotonic() - t0 < 1.0          # three in parallel, not 1.2 s in a row


def test_the_first_caller_keeps_its_force_flag(monkeypatch):
    f = patch(monkeypatch, Fake())
    f.cache["AAA.NS"] = {"old": True}
    out, _ = run_threads([(("AAA",), {"force": True})])
    assert f.fresh == 1 and f.forces == [True] and out[0]["n"] == 1


def test_a_forced_follower_reuses_the_fresh_result_of_the_first_caller(monkeypatch):
    f = patch(monkeypatch, Fake())
    out, _ = run_threads([(("AAA",), {"force": True}), (("AAA",), {"force": True})])
    assert f.fresh == 1 and f.forces == [True, False] and out[0] == out[1]


def test_when_the_first_caller_fails_the_follower_computes_for_itself(monkeypatch):
    f = patch(monkeypatch, Fake(fail_first=True))
    out, errs = run_threads([(("AAA",), {}), (("AAA",), {})])
    assert isinstance(errs[0], RuntimeError) and errs[1] is None
    assert f.fresh == 2 and out[1]["n"] == 2


def test_a_follower_stops_waiting_after_the_join_limit(monkeypatch):
    monkeypatch.setenv("FUNDAMENTALS_JOIN_WAIT_S", "0.1")
    f = patch(monkeypatch, Fake(work_s=0.6))
    t0 = time.monotonic()
    out, _ = run_threads([(("AAA",), {"force": True}), (("AAA",), {"force": True})])
    assert f.fresh == 2 and f.forces == [True, True]               # timed-out follower computes with its own force
    assert time.monotonic() - t0 < 1.2


def test_the_switch_restores_one_computation_per_call(monkeypatch):
    monkeypatch.setenv("FUNDAMENTALS_SINGLE_FLIGHT", "0")
    f = patch(monkeypatch, Fake(work_s=0.3))
    run_threads([(("AAA",), {}), (("AAA",), {})])
    assert f.fresh == 2 and f.hits == 0 and m._FUND_FLIGHTS == {}


@pytest.mark.parametrize("raw", ["", "1", "yes", " ON "])
def test_blank_or_on_keeps_it_on(monkeypatch, raw):
    monkeypatch.setenv("FUNDAMENTALS_SINGLE_FLIGHT", raw)
    assert m._fund_single_flight_on() is True


@pytest.mark.parametrize("raw", ["0", "false", "No", " off "])
def test_off_values(monkeypatch, raw):
    monkeypatch.setenv("FUNDAMENTALS_SINGLE_FLIGHT", raw)
    assert m._fund_single_flight_on() is False


@pytest.mark.parametrize("raw, want", [("", 40.0), ("  ", 40.0), ("x", 40.0), ("-3", 40.0), ("0", 40.0), ("nan", 40.0),
                                       ("12", 12.0), ("0.5", 0.5)])
def test_join_wait_parsing_is_blank_safe(monkeypatch, raw, want):
    monkeypatch.setenv("FUNDAMENTALS_JOIN_WAIT_S", raw)
    assert m._fund_join_wait_s() == want


def test_the_lock_is_released_after_an_error(monkeypatch):
    f = patch(monkeypatch, Fake(work_s=0.01, fail_first=True))
    with pytest.raises(RuntimeError):
        m._get_fundamentals_inner("AAA")
    assert m._get_fundamentals_inner("AAA")["n"] == 2 and f.fresh == 2
    assert not m._FUND_FLIGHTS["AAA.NS"].locked()


def test_the_lock_map_drops_idle_locks_when_full(monkeypatch):
    monkeypatch.setattr(m, "_FUND_FLIGHTS_MAX", 3)
    held = m._fund_flight_lock("HELD.NS")
    held.acquire()
    try:
        m._fund_flight_lock("A.NS")
        m._fund_flight_lock("B.NS")
        m._fund_flight_lock("C.NS")                                # map is full: idle locks are dropped first
        assert "HELD.NS" in m._FUND_FLIGHTS and len(m._FUND_FLIGHTS) <= 2
    finally:
        held.release()


def test_the_endpoint_still_answers_through_the_wrapper(monkeypatch):
    f = patch(monkeypatch, Fake(work_s=0.0))
    monkeypatch.setattr(m, "is_known_delisted", lambda s: False)
    out = m.get_fundamentals_raw("AAA.NS", force=False)
    assert out["n"] == 1 and f.fresh == 1
