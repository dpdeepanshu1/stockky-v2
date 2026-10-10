"""group300: peer fundamentals timeout is configurable (default 30 s, was a fixed 15 s) and a TIMEOUT is retried once."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fundamental"))

import httpx
import pytest
import peer_multi_quarter as pmq


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("PEER_FUNDAMENTALS_TIMEOUT_S", raising=False)
    monkeypatch.delenv("PEER_FUNDAMENTALS_TIMEOUT_RETRIES", raising=False)
    with pmq._FUND_CACHE_LOCK:
        pmq._FUND_CACHE.clear()
        pmq._FUND_INFLIGHT.clear()
    yield
    with pmq._FUND_CACHE_LOCK:
        pmq._FUND_CACHE.clear()
        pmq._FUND_INFLIGHT.clear()


class _R:
    def __init__(self, code=200, body=None):
        self.status_code, self._b = code, body if body is not None else {"pe_ratio": 20}

    def json(self):
        return self._b


def script(monkeypatch, steps):
    """steps: list of exceptions / responses returned one per httpx.get call."""
    calls = []

    def _get(url, timeout=None, **k):
        calls.append((url, timeout))
        step = steps[min(len(calls) - 1, len(steps) - 1)]
        if isinstance(step, Exception):
            raise step
        return step
    monkeypatch.setattr(httpx, "get", _get)
    return calls


def test_default_timeout_is_30_not_15(monkeypatch):
    calls = script(monkeypatch, [_R()])
    pmq.fetch_fundamentals("http://md", "CIPLA")
    assert calls[0][1] == 30.0


def test_an_explicit_timeout_still_wins(monkeypatch):
    calls = script(monkeypatch, [_R()])
    pmq.fetch_fundamentals("http://md", "CIPLA", timeout=5.0)
    assert calls[0][1] == 5.0


def test_batch_uses_the_configured_timeout(monkeypatch):
    monkeypatch.setenv("PEER_FUNDAMENTALS_TIMEOUT_S", "42")
    calls = script(monkeypatch, [_R()])
    pmq.fetch_fundamentals_batch("http://md", ["CIPLA", "DRREDDY"])
    assert {c[1] for c in calls} == {42.0}


def test_a_timeout_is_retried_once_and_the_retry_result_is_cached(monkeypatch):
    calls = script(monkeypatch, [httpx.ReadTimeout("t"), _R(body={"pe_ratio": 31})])
    assert pmq.fetch_fundamentals("http://md", "DIVISLAB") == {"pe_ratio": 31}
    assert len(calls) == 2
    assert pmq.fetch_fundamentals("http://md", "DIVISLAB") == {"pe_ratio": 31} and len(calls) == 2   # cached now


def test_two_timeouts_give_up_with_an_empty_dict_after_two_calls(monkeypatch):
    calls = script(monkeypatch, [httpx.ReadTimeout("t")])
    assert pmq.fetch_fundamentals("http://md", "DIVISLAB") == {}
    assert len(calls) == 2


def test_retries_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("PEER_FUNDAMENTALS_TIMEOUT_RETRIES", "0")
    calls = script(monkeypatch, [httpx.ReadTimeout("t"), _R()])
    assert pmq.fetch_fundamentals("http://md", "DIVISLAB") == {} and len(calls) == 1


def test_a_connection_error_is_not_retried(monkeypatch):
    calls = script(monkeypatch, [httpx.ConnectError("down"), _R()])
    assert pmq.fetch_fundamentals("http://md", "DIVISLAB") == {} and len(calls) == 1


def test_a_non_200_is_not_retried_and_not_cached(monkeypatch):
    calls = script(monkeypatch, [_R(code=502), _R()])
    assert pmq.fetch_fundamentals("http://md", "DIVISLAB") == {} and len(calls) == 1
    assert "DIVISLAB.NS" not in pmq._FUND_CACHE


@pytest.mark.parametrize("raw, want", [("", 30.0), ("  ", 30.0), ("x", 30.0), ("nan", 30.0), ("0", 30.0), ("-5", 30.0),
                                       ("500", 30.0), ("12", 12.0), (" 45.5 ", 45.5), ("120", 120.0)])
def test_timeout_parsing_is_blank_safe(monkeypatch, raw, want):
    monkeypatch.setenv("PEER_FUNDAMENTALS_TIMEOUT_S", raw)
    assert pmq._fund_timeout_s() == want


@pytest.mark.parametrize("raw, want", [("", 1), ("x", 1), ("-1", 1), ("9", 1), ("0", 0), ("2", 2), (" 3 ", 3), ("1.0", 1)])
def test_retry_parsing_is_blank_safe(monkeypatch, raw, want):
    monkeypatch.setenv("PEER_FUNDAMENTALS_TIMEOUT_RETRIES", raw)
    assert pmq._fund_timeout_retries() == want
