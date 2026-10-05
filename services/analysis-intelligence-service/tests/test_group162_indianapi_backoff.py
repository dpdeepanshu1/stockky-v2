"""
group162 (item 3): IndianAPI 429 cooldown and per-symbol failure skip (fundamental/indianapi_fallback.py).

At the open VINCOFE, SATIN, KOHINOOR, COMSYN, KKCL and DCI each hit IndianAPI again and again and got 429
every time. A 429 now starts a process-wide cooldown; other failures keep only that symbol out for a while.
No real HTTP: requests.get is monkeypatched.
"""
from __future__ import annotations
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fundamental"))

import pytest
import requests

import indianapi_fallback as iaf


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("INDIANAPI_COOLDOWN", "INDIANAPI_COOLDOWN_S", "INDIANAPI_COOLDOWN_MAX_S",
              "INDIANAPI_SYMBOL_FAIL_TTL_S"):
        monkeypatch.delenv(k, raising=False)
    iaf.INDIANAPI_KEY = "k"
    iaf.reset_backoff()
    monkeypatch.setattr(iaf, "_enforce_rate_limit", lambda r: None)
    yield
    iaf.INDIANAPI_KEY = None
    iaf.reset_backoff()


class _Resp:
    def __init__(self, status=200, headers=None, body=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body if body is not None else {"pe": 20}

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code} Client Error")
            err.response = self
            raise err

    def json(self):
        return self._body


def _serve(monkeypatch, *responses):
    """requests.get answers from `responses` in order (last one repeats); returns the call log."""
    calls = []
    seq = list(responses)

    def _get(url, **kw):
        calls.append(kw["params"]["name"])
        r = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(requests, "get", _get)
    return calls


class TestRateLimitCooldown:
    def test_429_returns_none_and_starts_cooldown(self, monkeypatch):
        calls = _serve(monkeypatch, _Resp(429))
        assert iaf._fetch_from_indianapi("SATIN") is None
        assert iaf._in_cooldown() is True
        assert calls == ["SATIN"]

    def test_no_request_for_any_symbol_during_cooldown(self, monkeypatch):
        calls = _serve(monkeypatch, _Resp(429))
        iaf._fetch_from_indianapi("SATIN")
        for s in ("VINCOFE", "KOHINOOR", "COMSYN", "KKCL", "DCI"):
            assert iaf._fetch_from_indianapi(s) is None
        assert calls == ["SATIN"]

    def test_cooldown_does_not_take_a_rate_limit_slot(self, monkeypatch):
        _serve(monkeypatch, _Resp(429))
        iaf._fetch_from_indianapi("SATIN")
        slots = []
        monkeypatch.setattr(iaf, "_enforce_rate_limit", lambda r: slots.append(1))
        iaf._fetch_from_indianapi("DCI")
        assert slots == []

    def test_requests_resume_after_the_cooldown(self, monkeypatch):
        calls = _serve(monkeypatch, _Resp(429), _Resp(200))
        iaf._fetch_from_indianapi("SATIN")
        iaf._COOLDOWN_UNTIL = time.monotonic() - 1
        assert iaf._fetch_from_indianapi("DCI") == {"pe": 20}
        assert calls == ["SATIN", "DCI"]

    def test_default_wait_is_120s(self, monkeypatch):
        _serve(monkeypatch, _Resp(429))
        iaf._fetch_from_indianapi("SATIN")
        assert 115 < iaf._COOLDOWN_UNTIL - time.monotonic() <= 120

    def test_retry_after_header_is_honoured_when_longer(self, monkeypatch):
        _serve(monkeypatch, _Resp(429, headers={"Retry-After": "300"}))
        iaf._fetch_from_indianapi("SATIN")
        assert 295 < iaf._COOLDOWN_UNTIL - time.monotonic() <= 300

    def test_retry_after_is_capped(self, monkeypatch):
        _serve(monkeypatch, _Resp(429, headers={"Retry-After": "99999"}))
        iaf._fetch_from_indianapi("SATIN")
        assert iaf._COOLDOWN_UNTIL - time.monotonic() <= 900

    def test_bad_retry_after_is_ignored(self, monkeypatch):
        _serve(monkeypatch, _Resp(429, headers={"Retry-After": "soon"}))
        iaf._fetch_from_indianapi("SATIN")
        assert 115 < iaf._COOLDOWN_UNTIL - time.monotonic() <= 120

    def test_consecutive_429s_double_up_to_the_cap(self, monkeypatch):
        _serve(monkeypatch, _Resp(429))
        waits = []
        for _ in range(5):
            iaf._COOLDOWN_UNTIL = 0.0
            iaf._fetch_from_indianapi("SATIN")
            waits.append(round(iaf._COOLDOWN_UNTIL - time.monotonic()))
        assert waits == [120, 240, 480, 900, 900]

    def test_success_resets_the_streak(self, monkeypatch):
        _serve(monkeypatch, _Resp(429), _Resp(200), _Resp(429))
        iaf._fetch_from_indianapi("A")
        iaf._COOLDOWN_UNTIL = 0.0
        iaf._fetch_from_indianapi("B")
        assert iaf._COOLDOWN_STREAK == 0
        iaf._fetch_from_indianapi("C")
        assert 115 < iaf._COOLDOWN_UNTIL - time.monotonic() <= 120

    def test_429_raised_through_raise_for_status_is_also_caught(self, monkeypatch):
        class _Raising(_Resp):
            def raise_for_status(self):
                err = requests.HTTPError("429 Client Error")
                err.response = _Resp(429, headers={"Retry-After": "200"})
                raise err
        _serve(monkeypatch, _Raising())
        assert iaf._fetch_from_indianapi("SATIN") is None
        assert iaf._in_cooldown() is True
        assert 195 < iaf._COOLDOWN_UNTIL - time.monotonic() <= 200

    def test_429_logs_one_warning_not_an_error(self, monkeypatch, caplog):
        _serve(monkeypatch, _Resp(429))
        with caplog.at_level("DEBUG", logger=iaf.logger.name):
            iaf._fetch_from_indianapi("SATIN")
            iaf._fetch_from_indianapi("DCI")
        assert [r.levelname for r in caplog.records if "429" in r.getMessage()] == ["WARNING"]
        assert not [r for r in caplog.records if r.levelname == "ERROR"]

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("INDIANAPI_COOLDOWN_S", "30")
        _serve(monkeypatch, _Resp(429))
        iaf._fetch_from_indianapi("SATIN")
        assert 25 < iaf._COOLDOWN_UNTIL - time.monotonic() <= 30

    def test_off_switch_restores_old_behaviour(self, monkeypatch):
        monkeypatch.setenv("INDIANAPI_COOLDOWN", "0")
        calls = _serve(monkeypatch, _Resp(429))
        assert iaf._fetch_from_indianapi("A") is None
        assert iaf._fetch_from_indianapi("B") is None
        assert calls == ["A", "B"]
        assert iaf._in_cooldown() is False

    def test_blank_and_bad_env_values_fall_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv("INDIANAPI_COOLDOWN_S", "abc")
        monkeypatch.setenv("INDIANAPI_COOLDOWN_MAX_S", "-1")
        monkeypatch.setenv("INDIANAPI_SYMBOL_FAIL_TTL_S", "nan")
        assert iaf._backoff_cfg() == (True, 120.0, 900.0, 600.0)
        monkeypatch.setenv("INDIANAPI_COOLDOWN_S", "   ")
        monkeypatch.setenv("INDIANAPI_COOLDOWN", "  ")
        assert iaf._backoff_cfg()[:2] == (True, 120.0)


class TestPerSymbolFailure:
    def test_a_failed_symbol_is_not_asked_again_at_once(self, monkeypatch):
        calls = _serve(monkeypatch, requests.ConnectionError("down"), _Resp(200))
        assert iaf._fetch_from_indianapi("KKCL") is None
        assert iaf._fetch_from_indianapi("KKCL") is None
        assert calls == ["KKCL"]

    def test_other_symbols_still_asked(self, monkeypatch):
        calls = _serve(monkeypatch, requests.ConnectionError("down"), _Resp(200))
        iaf._fetch_from_indianapi("KKCL")
        assert iaf._fetch_from_indianapi("DCI") == {"pe": 20}
        assert calls == ["KKCL", "DCI"]

    def test_spelling_case_is_ignored(self, monkeypatch):
        calls = _serve(monkeypatch, requests.ConnectionError("down"))
        iaf._fetch_from_indianapi("kkcl")
        iaf._fetch_from_indianapi("KKCL")
        assert calls == ["kkcl"]

    def test_symbol_is_asked_again_after_the_ttl(self, monkeypatch):
        calls = _serve(monkeypatch, requests.ConnectionError("down"), _Resp(200))
        iaf._fetch_from_indianapi("KKCL")
        iaf._SYMBOL_FAIL["KKCL"] = time.monotonic() - 1
        assert iaf._fetch_from_indianapi("KKCL") == {"pe": 20}
        assert calls == ["KKCL", "KKCL"]

    def test_a_non_429_http_error_blocks_only_that_symbol(self, monkeypatch):
        calls = _serve(monkeypatch, _Resp(404), _Resp(200))
        iaf._fetch_from_indianapi("NOPE")
        assert iaf._in_cooldown() is False
        assert iaf._fetch_from_indianapi("DCI") == {"pe": 20}
        assert iaf._fetch_from_indianapi("NOPE") is None
        assert calls == ["NOPE", "DCI"]

    def test_failure_is_still_logged_as_an_error(self, monkeypatch, caplog):
        _serve(monkeypatch, requests.ConnectionError("down"))
        with caplog.at_level("ERROR", logger=iaf.logger.name):
            iaf._fetch_from_indianapi("KKCL")
        assert any("IndianAPI request failed for KKCL" in r.getMessage() for r in caplog.records)

    def test_ttl_zero_disables_the_skip(self, monkeypatch):
        monkeypatch.setenv("INDIANAPI_SYMBOL_FAIL_TTL_S", "0")
        calls = _serve(monkeypatch, requests.ConnectionError("down"))
        iaf._fetch_from_indianapi("KKCL")
        iaf._fetch_from_indianapi("KKCL")
        assert calls == ["KKCL", "KKCL"]

    def test_table_is_bounded(self, monkeypatch):
        monkeypatch.setattr(iaf, "_SYMBOL_FAIL_MAX", 3)
        far = time.monotonic() + 999
        for i in range(3):
            iaf._SYMBOL_FAIL[f"S{i}"] = far
        iaf._note_symbol_failure("NEW")
        assert "NEW" not in iaf._SYMBOL_FAIL and len(iaf._SYMBOL_FAIL) == 3

    def test_expired_entries_make_room(self, monkeypatch):
        monkeypatch.setattr(iaf, "_SYMBOL_FAIL_MAX", 3)
        for i in range(3):
            iaf._SYMBOL_FAIL[f"S{i}"] = 0.0
        iaf._note_symbol_failure("NEW")
        assert list(iaf._SYMBOL_FAIL) == ["NEW"]


class TestThroughTheFallbackEntryPoint:
    def _kv(self, monkeypatch, store=None):
        store = {} if store is None else store

        class _KV:
            def get(self, k): return store.get(k)
            def set(self, k, v, ttl=None): store[k] = v
        monkeypatch.setattr(iaf, "_kv", _KV())
        return store

    def test_cached_data_is_still_served_during_a_cooldown(self, monkeypatch):
        from datetime import datetime
        store = self._kv(monkeypatch)
        store[iaf.CACHE_KEY_PREFIX + "SATIN"] = {"data": {"pe": 9}, "cached_at": datetime.now(iaf.IST).isoformat()}
        iaf._COOLDOWN_UNTIL = time.monotonic() + 100
        calls = _serve(monkeypatch, _Resp(200))
        assert iaf.get_fundamentals_with_fallback("SATIN", lambda s: None) == {"pe": 9}
        assert calls == []

    def test_stale_cache_is_returned_when_cooling_down(self, monkeypatch):
        from datetime import datetime, timedelta
        store = self._kv(monkeypatch)
        old = (datetime.now(iaf.IST) - timedelta(days=30)).isoformat()
        store[iaf.CACHE_KEY_PREFIX + "SATIN"] = {"data": {"pe": 7}, "cached_at": old}
        iaf._COOLDOWN_UNTIL = time.monotonic() + 100
        calls = _serve(monkeypatch, _Resp(200))
        assert iaf.get_fundamentals_with_fallback("SATIN", lambda s: None) == {"pe": 7}
        assert calls == []

    def test_no_cache_and_cooling_down_returns_none_without_a_request(self, monkeypatch):
        self._kv(monkeypatch)
        iaf._COOLDOWN_UNTIL = time.monotonic() + 100
        calls = _serve(monkeypatch, _Resp(200))
        assert iaf.get_fundamentals_with_fallback("VINCOFE", lambda s: None) is None
        assert calls == []

    def test_six_symbols_one_429_one_request(self, monkeypatch):
        self._kv(monkeypatch)
        calls = _serve(monkeypatch, _Resp(429))
        for s in ("VINCOFE", "SATIN", "KOHINOOR", "COMSYN", "KKCL", "DCI"):
            assert iaf.get_fundamentals_with_fallback(s, lambda x: None) is None
        assert len(calls) == 1

    def test_yahoo_success_never_touches_indianapi(self, monkeypatch):
        calls = _serve(monkeypatch, _Resp(200))
        assert iaf.get_fundamentals_with_fallback("TCS", lambda s: {"pe": 30}) == {"pe": 30}
        assert calls == []


class TestHelpersNeverRaise:
    def test_broken_config_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(iaf, "_backoff_cfg", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert iaf._in_cooldown() is False
        assert iaf._symbol_blocked("A") is False
        iaf._note_rate_limited(None)
        iaf._note_symbol_failure("A")
