"""Round 189 (issue #4): the gateway's NSE api pause escalates on repeated blocks, and the securities bhavcopy fallback is asked and logged once per
NSE_SECURITIES_FALLBACK_TTL_S instead of once per caller.

Run from services/api-gateway:  python3 -m pytest tests/test_round189_nse_backoff_and_fallback_memory.py -q
"""
from __future__ import annotations

import time

import main as gw


def test_pause_length_doubles_and_caps(monkeypatch):
    monkeypatch.delenv("NSE_API_BLOCK_MAX_S", raising=False)
    assert [gw._nse_api_block_pause_s(600, k) for k in (1, 2, 3, 4, 5)] == [600, 1200, 2400, 3600, 3600]
    assert gw._nse_api_block_pause_s(0, 3) == 0.0
    monkeypatch.setenv("NSE_API_BLOCK_MAX_S", "100")
    assert gw._nse_api_block_pause_s(600, 4) == 600


class _Resp:
    def __init__(self, code, body=None):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


class _Client:
    def __init__(self, code):
        self.code = code

    def get(self, url):
        return _Resp(self.code)


def _blocked_round(monkeypatch):
    monkeypatch.setattr(gw, "_redis_get", lambda k: None)
    monkeypatch.setattr(gw, "_get_nse_client", lambda force_new=False: _Client(403))
    gw._fetch_from_nse_api("a", "ck")


def test_second_block_in_a_row_pauses_twice_as_long(monkeypatch):
    monkeypatch.delenv("NSE_API_BLOCK_SECONDS", raising=False)
    monkeypatch.delenv("NSE_API_BLOCK_MAX_S", raising=False)
    _blocked_round(monkeypatch)
    assert abs((gw._nse_api_block_until - time.time()) - 600) < 5 and gw._nse_api_block_streak == 1
    gw._nse_api_block_until = time.time() - 1
    _blocked_round(monkeypatch)
    assert abs((gw._nse_api_block_until - time.time()) - 1200) < 5 and gw._nse_api_block_streak == 2


def test_a_200_resets_the_streak(monkeypatch):
    monkeypatch.delenv("NSE_API_BLOCK_SECONDS", raising=False)
    _blocked_round(monkeypatch)
    gw._nse_api_block_until = time.time() - 1
    monkeypatch.setattr(gw, "_get_nse_client", lambda force_new=False: type("C", (), {"get": lambda self, u: _Resp(200, {"data": []})})())
    monkeypatch.setattr(gw, "_redis_set", lambda *a, **k: None)
    gw._fetch_from_nse_api("a", "ck")
    assert gw._nse_api_block_streak == 0


def test_bhavcopy_fallback_is_asked_once_for_several_callers(monkeypatch):
    monkeypatch.delenv("NSE_SECURITIES_FALLBACK_TTL_S", raising=False)
    gw._nse_securities_fallback.update({"symbols": [], "ts": 0.0})
    nse_calls, bhav_calls = [], []
    monkeypatch.setattr(gw, "_fetch_from_nse_api", lambda *a, **k: nse_calls.append(1) or {"data": []})
    monkeypatch.setattr(gw.httpx, "get", lambda *a, **k: bhav_calls.append(1) or type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: {"symbols": ["ABC", "DEF"]}})())
    assert gw._get_all_nse_securities() == ["ABC", "DEF"]
    assert gw._get_all_nse_securities() == ["ABC", "DEF"]
    assert len(nse_calls) == 1 and len(bhav_calls) == 1


def test_ttl_zero_keeps_the_old_every_call_behaviour(monkeypatch):
    monkeypatch.setenv("NSE_SECURITIES_FALLBACK_TTL_S", "0")
    gw._nse_securities_fallback.update({"symbols": [], "ts": 0.0})
    nse_calls = []
    monkeypatch.setattr(gw, "_fetch_from_nse_api", lambda *a, **k: nse_calls.append(1) or {"data": []})
    monkeypatch.setattr(gw.httpx, "get", lambda *a, **k: type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: {"symbols": ["ABC"]}})())
    gw._get_all_nse_securities(); gw._get_all_nse_securities()
    assert len(nse_calls) == 2


def test_empty_fallback_is_not_remembered(monkeypatch):
    monkeypatch.delenv("NSE_SECURITIES_FALLBACK_TTL_S", raising=False)
    gw._nse_securities_fallback.update({"symbols": [], "ts": 0.0})
    monkeypatch.setattr(gw, "_fetch_from_nse_api", lambda *a, **k: None)
    monkeypatch.setattr(gw.httpx, "get", lambda *a, **k: type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: {"symbols": []}})())
    gw._get_all_nse_securities()
    assert gw._nse_securities_fallback["symbols"] == []
