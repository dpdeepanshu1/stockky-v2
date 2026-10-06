"""
group189 (item 12): the momentum-movers computation (4 NSE boards + AngelOne sweep + bulk fallback) ran twice at
once at boot because the result cache is only filled when a pass finishes. Concurrent callers now share one pass.

Run from services/api-gateway:
    python3 -m pytest tests/test_group189_momentum_movers_single_flight.py -q
"""
from __future__ import annotations
import os, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import main as gw


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("MOMENTUM_MOVERS_SINGLE_FLIGHT", raising=False)
    monkeypatch.delenv("MOMENTUM_MOVERS_JOIN_WAIT_S", raising=False)
    monkeypatch.setattr(gw, "_redis_get", lambda *a, **k: None)
    gw._mm_flight = None
    yield
    gw._mm_flight = None


class _Slow:
    """Stand-in for _compute_momentum_movers: counts calls, blocks until released."""
    def __init__(self, result=("AAA", "BBB"), raises=None):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.result = list(result)
        self.raises = raises
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
            n = self.calls
        self.started.set()
        if n == 1:
            self.release.wait(5)
        if self.raises and n == 1:
            raise self.raises
        return list(self.result)


def _run_callers(n):
    out, ths = [None] * n, []
    for i in range(n):
        def _w(i=i):
            try:
                out[i] = gw._get_momentum_movers()
            except Exception as e:  # noqa: BLE001
                out[i] = e
        t = threading.Thread(target=_w)
        ths.append(t)
        t.start()
    return out, ths


def test_concurrent_callers_share_one_pass(monkeypatch):
    slow = _Slow()
    monkeypatch.setattr(gw, "_compute_momentum_movers", slow)
    out, ths = _run_callers(1)
    assert slow.started.wait(2)
    more, ths2 = _run_callers(3)
    time.sleep(0.2)
    slow.release.set()
    for t in ths + ths2:
        t.join(5)
    assert slow.calls == 1
    assert out[0] == ["AAA", "BBB"] and all(r == ["AAA", "BBB"] for r in more)


def test_followers_get_their_own_copy(monkeypatch):
    slow = _Slow()
    monkeypatch.setattr(gw, "_compute_momentum_movers", slow)
    _, ths = _run_callers(1)
    slow.started.wait(2)
    more, ths2 = _run_callers(2)
    time.sleep(0.2)
    slow.release.set()
    for t in ths + ths2:
        t.join(5)
    more[0].append("ZZZ")
    assert "ZZZ" not in more[1]


def test_next_call_after_the_pass_computes_again(monkeypatch):
    slow = _Slow()
    slow.release.set()
    monkeypatch.setattr(gw, "_compute_momentum_movers", slow)
    gw._get_momentum_movers()
    gw._get_momentum_movers()
    assert slow.calls == 2
    assert gw._mm_flight is None


def test_cache_hit_skips_everything(monkeypatch):
    slow = _Slow()
    monkeypatch.setattr(gw, "_compute_momentum_movers", slow)
    monkeypatch.setattr(gw, "_redis_get", lambda *a, **k: ["X"])
    assert gw._get_momentum_movers() == ["X"]
    assert slow.calls == 0


def test_leader_failure_follower_computes_itself(monkeypatch):
    slow = _Slow(raises=RuntimeError("boom"))
    monkeypatch.setattr(gw, "_compute_momentum_movers", slow)
    out, ths = _run_callers(1)
    slow.started.wait(2)
    more, ths2 = _run_callers(1)
    time.sleep(0.2)
    slow.release.set()
    for t in ths + ths2:
        t.join(5)
    assert isinstance(out[0], RuntimeError)
    assert more[0] == ["AAA", "BBB"]
    assert slow.calls == 2
    assert gw._mm_flight is None


def test_follower_does_not_wait_forever(monkeypatch):
    monkeypatch.setenv("MOMENTUM_MOVERS_JOIN_WAIT_S", "0.2")
    slow = _Slow()
    monkeypatch.setattr(gw, "_compute_momentum_movers", slow)
    _, ths = _run_callers(1)
    slow.started.wait(2)
    t0 = time.time()
    assert gw._get_momentum_movers() == ["AAA", "BBB"]      # computed by the follower itself (call #2, not blocked)
    assert time.time() - t0 < 2
    assert slow.calls == 2
    slow.release.set()
    for t in ths:
        t.join(5)


def test_switch_off_means_one_pass_per_caller(monkeypatch):
    monkeypatch.setenv("MOMENTUM_MOVERS_SINGLE_FLIGHT", "0")
    slow = _Slow()
    slow.release.set()
    monkeypatch.setattr(gw, "_compute_momentum_movers", slow)
    gw._get_momentum_movers()
    assert gw._mm_flight is None and slow.calls == 1


def test_reentrant_call_from_the_computing_thread_does_not_deadlock(monkeypatch):
    seen = []

    def _compute():
        seen.append(gw._get_momentum_movers())     # same thread calls back in while it is the leader
        return ["AAA"]

    calls = []

    def _wrapped():
        calls.append(1)
        if len(calls) > 1:
            return ["INNER"]
        return _compute()

    monkeypatch.setattr(gw, "_compute_momentum_movers", _wrapped)
    assert gw._get_momentum_movers() == ["AAA"]
    assert seen == [["INNER"]]


def test_non_list_result_is_not_shared(monkeypatch):
    monkeypatch.setattr(gw, "_compute_momentum_movers", lambda: None)
    assert gw._get_momentum_movers() is None
    assert gw._mm_flight is None


def test_bad_wait_value_falls_back(monkeypatch):
    monkeypatch.setenv("MOMENTUM_MOVERS_JOIN_WAIT_S", "abc")
    assert gw._momentum_single_flight_cfg() == (True, 45.0)
