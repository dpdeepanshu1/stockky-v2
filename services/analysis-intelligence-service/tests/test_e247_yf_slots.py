"""E247: at most EVENT_YF_MAX_CONCURRENT yfinance calls run at once in the event tracker (Yahoo's HTTP pool holds 10)."""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "event"))

import importlib

import pytest


@pytest.fixture()
def ev(monkeypatch):
    mod = importlib.import_module("event.main")
    return mod


def _run_many(ev, n_threads, slots, monkeypatch, wait_s="20"):
    monkeypatch.setattr(ev, "_yf_slots", threading.BoundedSemaphore(slots) if slots else None)
    monkeypatch.setenv("EVENT_YF_SLOT_WAIT_S", wait_s)
    ev._yf_cache.clear()
    live, peak, lock = [0], [0], threading.Lock()

    @ev.cached_yf("probe")
    def slow(symbol):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return symbol

    ts = [threading.Thread(target=slow, args=(f"S{i}",)) for i in range(n_threads)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return peak[0]


def test_never_more_than_the_cap_run_together(ev, monkeypatch):
    assert _run_many(ev, 14, 3, monkeypatch) <= 3


def test_zero_means_no_cap(ev, monkeypatch):
    assert _run_many(ev, 8, 0, monkeypatch) > 3


def test_a_cache_hit_takes_no_slot(ev, monkeypatch):
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(ev, "_yf_slots", sem)
    ev._yf_cache.clear()
    calls = []

    @ev.cached_yf("hit")
    def f(symbol):
        calls.append(symbol)
        return 1

    f("A")
    assert sem.acquire(blocking=False)            # the slot was released after the first call
    try:
        assert f("A") == 1 and calls == ["A"]      # served from cache while the only slot is held elsewhere
    finally:
        sem.release()


def test_a_stuck_slot_cannot_block_for_good(ev, monkeypatch):
    sem = threading.BoundedSemaphore(1)
    sem.acquire()                                  # someone holds the only slot and never lets go
    monkeypatch.setattr(ev, "_yf_slots", sem)
    monkeypatch.setenv("EVENT_YF_SLOT_WAIT_S", "0.1")
    ran = []
    t0 = time.time()
    with ev._yf_slot():
        ran.append(1)
    assert ran == [1] and time.time() - t0 < 2.0
    assert sem.acquire(blocking=False) is False    # and it did not release a slot it never got
