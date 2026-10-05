"""group170: md_guard - concurrency cap, single-flight and short cool-down around calls to market-data-service.
Run: python3 -m pytest tests/test_md_guard.py -q"""
from __future__ import annotations

import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import httpx
import pytest

import md_guard


class _Resp:
    def __init__(self, status=200):
        self.status_code = status


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("MD_GUARD", "MD_BREAKER", "MD_BREAKER_THRESHOLD", "MD_BREAKER_COOLDOWN_S", "MD_MAX_CONCURRENT",
              "MD_SLOT_WAIT_S"):
        monkeypatch.delenv(k, raising=False)
    md_guard.reset_state()
    yield
    md_guard.reset_state()


def fake_get(monkeypatch, fn):
    monkeypatch.setattr(httpx, "get", fn)


# ── exc_detail ───────────────────────────────────────────────────────────────
def test_exc_detail_names_type_when_message_is_empty():
    assert md_guard.exc_detail(httpx.ReadTimeout("")) == "ReadTimeout"
    assert md_guard.exc_detail(httpx.ReadTimeout("slow")) == "ReadTimeout: slow"


def test_exc_detail_survives_a_broken_str():
    class Bad(Exception):
        def __str__(self):
            raise RuntimeError("no")
    assert md_guard.exc_detail(Bad()) == "Bad"


# ── env parsing ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [("", 12.0), ("abc", 12.0), ("-3", 12.0), ("nan", 12.0), ("5", 5.0), ("0", 0.0)])
def test_env_float(monkeypatch, raw, expected):
    monkeypatch.setenv("MD_MAX_CONCURRENT", raw)
    assert md_guard._env_float("MD_MAX_CONCURRENT", 12, 0) == expected


# ── pass-through ─────────────────────────────────────────────────────────────
def test_plain_call_passes_url_params_and_timeout(monkeypatch):
    seen = []
    fake_get(monkeypatch, lambda url, params=None, timeout=None: seen.append((url, params, timeout)) or _Resp())
    assert md_guard.md_get("http://x/history/A", params={"period": "6mo"}, timeout=35).status_code == 200
    assert seen == [("http://x/history/A", {"period": "6mo"}, 35)]


def test_call_without_params_does_not_pass_params(monkeypatch):
    seen = []
    fake_get(monkeypatch, lambda url, timeout=None: seen.append((url, timeout)) or _Resp())   # no params kw at all
    md_guard.md_get("http://x/q", timeout=10)
    assert seen == [("http://x/q", 10)]


def test_guard_off_is_a_plain_call(monkeypatch):
    monkeypatch.setenv("MD_GUARD", "0")
    calls = []
    fake_get(monkeypatch, lambda url, params=None, timeout=None: calls.append(params) or _Resp())
    md_guard.md_get("http://x/a", params={"k": "v"}, timeout=1)
    md_guard.md_get("http://x/a", timeout=1) if False else None
    assert calls == [{"k": "v"}]


def test_guard_off_without_params(monkeypatch):
    monkeypatch.setenv("MD_GUARD", "0")
    fake_get(monkeypatch, lambda url, timeout=None: _Resp(204))
    assert md_guard.md_get("http://x/a", timeout=1).status_code == 204


def test_http_error_status_is_returned_not_raised(monkeypatch):
    fake_get(monkeypatch, lambda url, timeout=None: _Resp(503))
    assert md_guard.md_get("http://x/a", timeout=1).status_code == 503


# ── single flight ────────────────────────────────────────────────────────────
def _threads(n, target):
    ts = [threading.Thread(target=target) for _ in range(n)]
    for t in ts:
        t.start()
    return ts


def test_identical_concurrent_requests_share_one_upstream_call(monkeypatch):
    gate = threading.Event()
    calls = []

    def slow(url, timeout=None):
        calls.append(url)
        gate.wait(5)
        return _Resp()
    fake_get(monkeypatch, slow)
    out = []
    ts = _threads(6, lambda: out.append(md_guard.md_get("http://x/fund/A", timeout=5)))
    time.sleep(0.2)
    gate.set()
    for t in ts:
        t.join(5)
    assert len(calls) == 1 and len(out) == 6 and len({id(r) for r in out}) == 1
    assert md_guard._flights == {}


def test_different_urls_or_params_are_not_merged(monkeypatch):
    calls = []
    fake_get(monkeypatch, lambda url, params=None, timeout=None: calls.append((url, params)) or _Resp())
    md_guard.md_get("http://x/h/A", params={"force": "false"}, timeout=1)
    md_guard.md_get("http://x/h/A", params={"force": "true"}, timeout=1)
    md_guard.md_get("http://x/h/B", params={"force": "false"}, timeout=1)
    assert len(calls) == 3


def test_leader_error_is_shared_and_flight_is_cleared(monkeypatch):
    gate = threading.Event()
    calls = []

    def boom(url, timeout=None):
        calls.append(1)
        gate.wait(5)
        raise httpx.ReadTimeout("")
    fake_get(monkeypatch, boom)
    errs = []

    def one():
        try:
            md_guard.md_get("http://x/a", timeout=5)
        except httpx.ReadTimeout as e:
            errs.append(e)
    ts = _threads(4, one)
    time.sleep(0.2)
    gate.set()
    for t in ts:
        t.join(5)
    assert len(calls) == 1 and len(errs) == 4 and md_guard._flights == {}


def test_follower_makes_its_own_call_when_leader_never_finishes(monkeypatch):
    class NeverDone:
        done = False
        result = error = None

        class event:
            @staticmethod
            def wait(t):
                return False
    md_guard._flights[("http://x/a", ())] = NeverDone()
    fake_get(monkeypatch, lambda url, timeout=None: _Resp())
    assert md_guard.md_get("http://x/a", timeout="not-a-number").status_code == 200


def test_follower_gets_leader_error_object(monkeypatch):
    err = httpx.ConnectError("no")

    class Done:
        done = True
        result = None
        error = err

        class event:
            @staticmethod
            def wait(t):
                return True
    md_guard._flights[("http://x/a", ())] = Done()
    with pytest.raises(httpx.ConnectError):
        md_guard.md_get("http://x/a", timeout=1)


def test_unhashable_params_skip_single_flight(monkeypatch):
    fake_get(monkeypatch, lambda url, params=None, timeout=None: _Resp())
    assert md_guard.md_get("http://x/a", params={"k": ["a", "b"]}, timeout=1).status_code == 200


# ── concurrency cap ──────────────────────────────────────────────────────────
def test_no_more_than_the_cap_run_at_once(monkeypatch):
    monkeypatch.setenv("MD_MAX_CONCURRENT", "3")
    lock = threading.Lock()
    state = {"now": 0, "max": 0}

    def work(url, timeout=None):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return _Resp()
    fake_get(monkeypatch, work)
    ts = [threading.Thread(target=lambda i=i: md_guard.md_get(f"http://x/{i}", timeout=5)) for i in range(12)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert 1 <= state["max"] <= 3


def test_call_without_a_free_slot_fails_fast(monkeypatch):
    monkeypatch.setenv("MD_MAX_CONCURRENT", "1")
    monkeypatch.setenv("MD_SLOT_WAIT_S", "0.05")
    gate = threading.Event()
    fake_get(monkeypatch, lambda url, timeout=None: gate.wait(5) or _Resp())
    t = threading.Thread(target=lambda: md_guard.md_get("http://x/slow", timeout=5))
    t.start()
    time.sleep(0.2)
    with pytest.raises(md_guard.MarketDataUnavailable, match="queue full"):
        md_guard.md_get("http://x/other", timeout=5)
    gate.set()
    t.join(5)


def test_slot_is_released_after_an_error(monkeypatch):
    monkeypatch.setenv("MD_MAX_CONCURRENT", "1")
    monkeypatch.setenv("MD_BREAKER", "0")

    def boom(url, timeout=None):
        raise httpx.ReadTimeout("")
    fake_get(monkeypatch, boom)
    for i in range(3):
        with pytest.raises(httpx.ReadTimeout):
            md_guard.md_get(f"http://x/{i}", timeout=1)


def test_zero_means_unlimited(monkeypatch):
    monkeypatch.setenv("MD_MAX_CONCURRENT", "0")
    fake_get(monkeypatch, lambda url, timeout=None: _Resp())
    assert md_guard._semaphore() is None
    assert md_guard.md_get("http://x/a", timeout=1).status_code == 200


def test_capacity_follows_env_changes(monkeypatch):
    monkeypatch.setenv("MD_MAX_CONCURRENT", "2")
    a = md_guard._semaphore()
    assert md_guard._semaphore() is a
    monkeypatch.setenv("MD_MAX_CONCURRENT", "4")
    assert md_guard._semaphore() is not a


# ── cool-down ────────────────────────────────────────────────────────────────
def _timeouts(monkeypatch):
    def boom(url, timeout=None):
        raise httpx.ReadTimeout("")
    fake_get(monkeypatch, boom)


def test_repeated_timeouts_open_the_cooldown_and_calls_fail_fast(monkeypatch, caplog):
    monkeypatch.setenv("MD_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("MD_BREAKER_COOLDOWN_S", "30")
    _timeouts(monkeypatch)
    with caplog.at_level("WARNING", logger="md-guard"):
        for i in range(3):
            with pytest.raises(httpx.ReadTimeout):
                md_guard.md_get(f"http://x/{i}", timeout=1)
    assert sum("failing fast" in r.getMessage() for r in caplog.records) == 1
    called = []
    fake_get(monkeypatch, lambda url, timeout=None: called.append(url) or _Resp())
    with pytest.raises(md_guard.MarketDataUnavailable, match="cooling down"):
        md_guard.md_get("http://x/next", timeout=1)
    assert called == []                                     # market-data was not called at all
    assert isinstance(md_guard.MarketDataUnavailable("x"), httpx.HTTPError)


def test_cooldown_ends(monkeypatch):
    monkeypatch.setenv("MD_BREAKER_THRESHOLD", "2")
    monkeypatch.setenv("MD_BREAKER_COOLDOWN_S", "0.1")
    _timeouts(monkeypatch)
    for i in range(2):
        with pytest.raises(httpx.ReadTimeout):
            md_guard.md_get(f"http://x/{i}", timeout=1)
    time.sleep(0.2)
    fake_get(monkeypatch, lambda url, timeout=None: _Resp())
    assert md_guard.md_get("http://x/after", timeout=1).status_code == 200


def test_a_response_resets_the_streak(monkeypatch):
    monkeypatch.setenv("MD_BREAKER_THRESHOLD", "3")
    _timeouts(monkeypatch)
    for i in range(2):
        with pytest.raises(httpx.ReadTimeout):
            md_guard.md_get(f"http://x/{i}", timeout=1)
    fake_get(monkeypatch, lambda url, timeout=None: _Resp(500))
    md_guard.md_get("http://x/ok", timeout=1)
    _timeouts(monkeypatch)
    for i in range(2):
        with pytest.raises(httpx.ReadTimeout):
            md_guard.md_get(f"http://x/b{i}", timeout=1)
    fake_get(monkeypatch, lambda url, timeout=None: _Resp())
    assert md_guard.md_get("http://x/still-open", timeout=1).status_code == 200    # 2 < 3: not open


def test_non_timeout_errors_do_not_count(monkeypatch):
    monkeypatch.setenv("MD_BREAKER_THRESHOLD", "2")

    def refuse(url, timeout=None):
        raise httpx.ConnectError("no")
    fake_get(monkeypatch, refuse)
    for i in range(4):
        with pytest.raises(httpx.ConnectError):
            md_guard.md_get(f"http://x/{i}", timeout=1)


def test_breaker_off_switch(monkeypatch):
    monkeypatch.setenv("MD_BREAKER", "0")
    monkeypatch.setenv("MD_BREAKER_THRESHOLD", "1")
    _timeouts(monkeypatch)
    for i in range(3):
        with pytest.raises(httpx.ReadTimeout):
            md_guard.md_get(f"http://x/{i}", timeout=1)
    assert md_guard._breaker_remaining() == 0.0


def test_zero_cooldown_never_opens(monkeypatch):
    monkeypatch.setenv("MD_BREAKER_THRESHOLD", "1")
    monkeypatch.setenv("MD_BREAKER_COOLDOWN_S", "0")
    _timeouts(monkeypatch)
    for i in range(3):
        with pytest.raises(httpx.ReadTimeout):
            md_guard.md_get(f"http://x/{i}", timeout=1)
    assert md_guard._breaker_remaining() == 0.0
