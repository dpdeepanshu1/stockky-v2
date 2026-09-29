"""
tests/test_angelone_client.py — coverage for angelone_client.py

No real AngelOne network calls. httpx.AsyncClient is monkeypatched with
a lightweight fake. pyotp.TOTP is stubbed where needed.

Run from services/market-data-service:
    python3 -m pytest tests/test_angelone_client.py -v
"""
from __future__ import annotations
import asyncio, os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import httpx

import angelone_client as ac


def run(coro): return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset module-level caches between tests."""
    ac._outbound_ip_cache["ip"] = None
    ac._outbound_ip_cache["at"] = 0.0
    ac._denied_last_logged.clear()
    # Reset singleton session
    ac._session.token = None
    ac._session.feed_token = None
    ac._session.token_expiry = None
    yield
    ac._outbound_ip_cache["ip"] = None
    ac._outbound_ip_cache["at"] = 0.0
    ac._denied_last_logged.clear()
    ac._session.token = None
    ac._session.feed_token = None
    ac._session.token_expiry = None


# ── tiny httpx fake ───────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body or {}
        self.text = text or str(body)
    def json(self): return self._body
    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class _FakeClient:
    def __init__(self, responses):
        """responses: list of _FakeResponse, consumed in order; last one repeats."""
        self._q = list(responses)
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass
    def _next(self): return self._q.pop(0) if len(self._q) > 1 else self._q[0]
    async def post(self, url, **kw): return self._next()
    async def get(self, url, **kw): return self._next()


def _patch_client(monkeypatch, *responses):
    """Make httpx.AsyncClient(...) return a _FakeClient with given responses."""
    resp_list = list(responses)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(resp_list))


def _login_ok_body():
    return {
        "status": True,
        "data": {
            "jwtToken": "jwt_abc",
            "feedToken": "feed_xyz",
        }
    }


def _configured_session(monkeypatch) -> ac.AngelOneSession:
    s = ac.AngelOneSession()
    monkeypatch.setenv("ANGELONE_CLIENT_ID", "C1")
    monkeypatch.setenv("ANGELONE_MPIN", "1234")
    monkeypatch.setenv("ANGELONE_API_KEY", "APIKEY")
    monkeypatch.setenv("ANGELONE_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    s.client_id = "C1"; s.mpin = "1234"; s.api_key = "APIKEY"
    s.totp_secret = "JBSWY3DPEHPK3PXP"
    return s


# ══════════════════════════════════════════════════════════════════════════════
# get_outbound_ip
# ══════════════════════════════════════════════════════════════════════════════

class TestGetOutboundIp:
    def test_returns_ip_on_success(self, monkeypatch):
        monkeypatch.setattr(httpx, "get",
            lambda url, timeout: _FakeResponse(200, {"ip": "1.2.3.4"}))
        assert ac.get_outbound_ip() == "1.2.3.4"

    def test_returns_none_on_exception(self, monkeypatch):
        def _boom(*a, **kw): raise RuntimeError("no network")
        monkeypatch.setattr(httpx, "get", _boom)
        assert ac.get_outbound_ip() is None

    def test_returns_none_on_http_error(self, monkeypatch):
        r = _FakeResponse(503, {})
        def _bad(*a, **kw):
            r.raise_for_status(); return r
        monkeypatch.setattr(httpx, "get", _bad)
        assert ac.get_outbound_ip() is None


# ══════════════════════════════════════════════════════════════════════════════
# _resolve_client_public_ip
# ══════════════════════════════════════════════════════════════════════════════

class TestResolveClientPublicIp:
    def test_explicit_env_wins(self, monkeypatch):
        monkeypatch.setenv("ANGELONE_STATIC_IP", "5.6.7.8")
        assert ac._resolve_client_public_ip() == "5.6.7.8"

    def test_uses_cached_ip_within_ttl(self, monkeypatch):
        monkeypatch.delenv("ANGELONE_STATIC_IP", raising=False)
        ac._outbound_ip_cache["ip"] = "9.9.9.9"
        ac._outbound_ip_cache["at"] = time.time()
        assert ac._resolve_client_public_ip() == "9.9.9.9"

    def test_refreshes_stale_cache(self, monkeypatch):
        monkeypatch.delenv("ANGELONE_STATIC_IP", raising=False)
        ac._outbound_ip_cache["ip"] = "old"
        ac._outbound_ip_cache["at"] = time.time() - ac._OUTBOUND_IP_TTL_SECONDS - 1
        monkeypatch.setattr(ac, "get_outbound_ip", lambda: "10.0.0.1")
        assert ac._resolve_client_public_ip() == "10.0.0.1"
        assert ac._outbound_ip_cache["ip"] == "10.0.0.1"

    def test_falls_back_to_loopback_and_logs_warning(self, monkeypatch, caplog):
        import logging
        monkeypatch.delenv("ANGELONE_STATIC_IP", raising=False)
        ac._outbound_ip_cache["ip"] = None
        monkeypatch.setattr(ac, "get_outbound_ip", lambda: None)
        with caplog.at_level(logging.WARNING):
            ip = ac._resolve_client_public_ip()
        assert ip == "127.0.0.1"
        assert "127.0.0.1" in caplog.text


# ══════════════════════════════════════════════════════════════════════════════
# _is_rate_limit_response
# ══════════════════════════════════════════════════════════════════════════════

class TestIsRateLimitResponse:
    def test_403_with_exceeding_access_rate_is_true(self):
        body = {"message": "Access denied because of exceeding access rate"}
        assert ac._is_rate_limit_response(403, body) is True

    def test_403_with_access_denied_is_true(self):
        assert ac._is_rate_limit_response(403, {"message": "Access denied"}) is True

    def test_403_with_unrelated_message_is_false(self):
        assert ac._is_rate_limit_response(403, {"message": "Not authorised"}) is False

    def test_429_always_true(self):
        assert ac._is_rate_limit_response(429, {}) is True

    def test_200_is_false(self):
        assert ac._is_rate_limit_response(200, {}) is False

    def test_none_body_is_false_for_403(self):
        assert ac._is_rate_limit_response(403, None) is False


# ══════════════════════════════════════════════════════════════════════════════
# _safe_json
# ══════════════════════════════════════════════════════════════════════════════

class TestSafeJson:
    def test_returns_dict_on_success(self):
        r = _FakeResponse(200, {"a": 1})
        assert ac._safe_json(r) == {"a": 1}

    def test_returns_none_on_bad_json(self):
        class _Bad:
            def json(self): raise ValueError("not json")
        assert ac._safe_json(_Bad()) is None


# ══════════════════════════════════════════════════════════════════════════════
# _log_denied
# ══════════════════════════════════════════════════════════════════════════════

class TestLogDenied:
    def test_logs_403_once_per_minute(self, caplog):
        import logging
        r = _FakeResponse(403, {}, text="forbidden body")
        with caplog.at_level(logging.WARNING):
            ac._log_denied("/quote", r)
        assert "forbidden body" in caplog.text

    def test_does_not_log_200(self, caplog):
        import logging
        r = _FakeResponse(200, {})
        with caplog.at_level(logging.WARNING):
            ac._log_denied("/quote", r)
        assert caplog.text == ""

    def test_throttled_within_window(self, caplog):
        import logging
        r = _FakeResponse(403, {}, text="denied")
        ac._denied_last_logged["/quote"] = time.time()
        with caplog.at_level(logging.WARNING):
            ac._log_denied("/quote", r)
        assert caplog.text == ""


# ══════════════════════════════════════════════════════════════════════════════
# AngelOneSession.is_configured / _get_lock
# ══════════════════════════════════════════════════════════════════════════════

class TestSessionHelpers:
    def test_not_configured_when_env_missing(self):
        s = ac.AngelOneSession()
        assert s.is_configured() is False

    def test_configured_when_all_env_set(self, monkeypatch):
        s = _configured_session(monkeypatch)
        assert s.is_configured() is True

    def test_get_lock_returns_same_lock_in_same_loop(self):
        s = ac.AngelOneSession()
        async def go():
            l1 = s._get_lock()
            l2 = s._get_lock()
            assert l1 is l2
        run(go())

    def test_get_lock_different_per_loop(self):
        s = ac.AngelOneSession()
        locks = []
        async def go(): locks.append(s._get_lock())
        asyncio.run(go())
        asyncio.run(go())
        assert locks[0] is not locks[1]


# ══════════════════════════════════════════════════════════════════════════════
# AngelOneSession._login
# ══════════════════════════════════════════════════════════════════════════════

class TestLogin:
    def test_raises_when_not_configured(self):
        s = ac.AngelOneSession()
        with pytest.raises(RuntimeError, match="not configured"):
            run(s._login())

    def test_successful_login_sets_token(self, monkeypatch):
        s = _configured_session(monkeypatch)
        _patch_client(monkeypatch, _FakeResponse(200, _login_ok_body()))
        monkeypatch.setattr(ac, "_resolve_client_public_ip", lambda: "1.2.3.4")
        run(s._login())
        assert s.token == "jwt_abc"
        assert s.feed_token == "feed_xyz"
        assert s.token_expiry is not None

    def test_login_failure_status_raises(self, monkeypatch):
        s = _configured_session(monkeypatch)
        body = {"status": False, "message": "Invalid credentials"}
        _patch_client(monkeypatch, _FakeResponse(200, body))
        monkeypatch.setattr(ac, "_resolve_client_public_ip", lambda: "1.2.3.4")
        with pytest.raises(RuntimeError, match="login failed"):
            run(s._login())

    def test_ensure_session_skips_login_when_token_valid(self, monkeypatch):
        from datetime import datetime, timedelta
        s = _configured_session(monkeypatch)
        s.token = "existing"
        s.token_expiry = datetime.utcnow() + timedelta(hours=1)
        call_count = [0]
        async def _fake_login(): call_count[0] += 1
        monkeypatch.setattr(s, "_login", _fake_login)
        run(s.ensure_session())
        assert call_count[0] == 0


# ══════════════════════════════════════════════════════════════════════════════
# get_quote
# ══════════════════════════════════════════════════════════════════════════════

class TestGetQuote:
    def _session(self, monkeypatch):
        s = _configured_session(monkeypatch)
        monkeypatch.setattr(s, "_login", lambda: None)   # sync stub – patch ensure_session
        from datetime import datetime, timedelta
        s.token = "tok"; s.token_expiry = datetime.utcnow() + timedelta(hours=1)
        monkeypatch.setattr(ac, "_resolve_client_public_ip", lambda: "1.2.3.4")
        return s

    def test_returns_empty_when_cooldown(self, monkeypatch):
        s = self._session(monkeypatch)
        monkeypatch.setattr(ac, "_rl_in_cooldown", lambda p: True)
        assert run(s.get_quote("NSE", "99926004")) == {}

    def test_returns_first_fetched_on_success(self, monkeypatch):
        s = self._session(monkeypatch)
        monkeypatch.setattr(ac, "_rl_in_cooldown", lambda p: False)
        monkeypatch.setattr(ac, "_rl_acquire", lambda *a, **kw: 0.0)
        body = {"data": {"fetched": [{"ltp": 150.0, "symbolToken": "99926004"}]}}
        _patch_client(monkeypatch, _FakeResponse(200, body))
        result = run(s.get_quote("NSE", "99926004"))
        assert result["ltp"] == 150.0

    def test_returns_empty_dict_when_fetched_empty(self, monkeypatch):
        s = self._session(monkeypatch)
        monkeypatch.setattr(ac, "_rl_in_cooldown", lambda p: False)
        monkeypatch.setattr(ac, "_rl_acquire", lambda *a, **kw: 0.0)
        _patch_client(monkeypatch, _FakeResponse(200, {"data": {"fetched": []}}))
        assert run(s.get_quote("NSE", "99926004")) == {}

    def test_rate_limit_response_sets_cooldown(self, monkeypatch):
        s = self._session(monkeypatch)
        monkeypatch.setattr(ac, "_rl_in_cooldown", lambda p: False)
        monkeypatch.setattr(ac, "_rl_acquire", lambda *a, **kw: 0.0)
        cooldown_set = []
        monkeypatch.setattr(ac, "_rl_set_cooldown", lambda p, secs: cooldown_set.append((p, secs)))
        body = {"message": "Access denied because of exceeding access rate"}
        _patch_client(monkeypatch, _FakeResponse(403, body, text=str(body)))
        result = run(s.get_quote("NSE", "99926004"))
        assert result == {}
        assert any(p == "angelone_quote" for p, _ in cooldown_set)


# ══════════════════════════════════════════════════════════════════════════════
# get_candles
# ══════════════════════════════════════════════════════════════════════════════

class TestGetCandles:
    def _session(self, monkeypatch):
        s = _configured_session(monkeypatch)
        from datetime import datetime, timedelta
        s.token = "tok"; s.token_expiry = datetime.utcnow() + timedelta(hours=1)
        monkeypatch.setattr(ac, "_resolve_client_public_ip", lambda: "1.2.3.4")
        return s

    def test_returns_empty_when_cooldown(self, monkeypatch):
        s = self._session(monkeypatch)
        monkeypatch.setattr(ac, "_rl_in_cooldown", lambda p: True)
        assert run(s.get_candles("NSE", "99926004", "ONE_DAY", "2026-01-01 09:15", "2026-01-30 15:30")) == []

    def test_returns_empty_when_try_acquire_fails(self, monkeypatch):
        s = self._session(monkeypatch)
        monkeypatch.setattr(ac, "_rl_in_cooldown", lambda p: False)
        monkeypatch.setattr(ac, "_rl_try_acquire", lambda *a, **kw: False)
        assert run(s.get_candles("NSE", "99926004", "ONE_DAY", "2026-01-01 09:15", "2026-01-30 15:30")) == []

    def test_returns_candles_on_success(self, monkeypatch):
        s = self._session(monkeypatch)
        monkeypatch.setattr(ac, "_rl_in_cooldown", lambda p: False)
        monkeypatch.setattr(ac, "_rl_try_acquire", lambda *a, **kw: True)
        candles = [["2026-01-01T09:15:00+05:30", 100, 105, 98, 102, 50000]]
        _patch_client(monkeypatch, _FakeResponse(200, {"data": candles}))
        result = run(s.get_candles("NSE", "99926004", "ONE_DAY", "2026-01-01", "2026-01-30"))
        assert result == candles

    def test_rate_limit_403_sets_cooldown(self, monkeypatch):
        s = self._session(monkeypatch)
        monkeypatch.setattr(ac, "_rl_in_cooldown", lambda p: False)
        monkeypatch.setattr(ac, "_rl_try_acquire", lambda *a, **kw: True)
        cooldown_set = []
        monkeypatch.setattr(ac, "_rl_set_cooldown", lambda p, secs: cooldown_set.append(p))
        body = {"message": "Access denied because of exceeding access rate"}
        _patch_client(monkeypatch, _FakeResponse(403, body, text=str(body)))
        result = run(s.get_candles("NSE", "99926004", "ONE_DAY", "2026-01-01", "2026-01-30"))
        assert result == []
        assert "angelone_candle" in cooldown_set


# ══════════════════════════════════════════════════════════════════════════════
# get_gainers_losers
# ══════════════════════════════════════════════════════════════════════════════

class TestGetGainersLosers:
    def _session(self, monkeypatch):
        s = _configured_session(monkeypatch)
        from datetime import datetime, timedelta
        s.token = "tok"; s.token_expiry = datetime.utcnow() + timedelta(hours=1)
        monkeypatch.setattr(ac, "_resolve_client_public_ip", lambda: "1.2.3.4")
        monkeypatch.setattr(ac, "_rl_in_cooldown", lambda p: False)
        monkeypatch.setattr(ac, "_rl_acquire", lambda *a, **kw: 0.0)
        return s

    def test_returns_empty_when_cooldown(self, monkeypatch):
        s = self._session(monkeypatch)
        monkeypatch.setattr(ac, "_rl_in_cooldown", lambda p: True)
        assert run(s.get_gainers_losers()) == []

    def test_returns_data_on_success(self, monkeypatch):
        s = self._session(monkeypatch)
        body = {"status": True, "data": [{"symbol": "RELIANCE25JANFUT"}]}
        _patch_client(monkeypatch, _FakeResponse(200, body))
        assert run(s.get_gainers_losers())[0]["symbol"] == "RELIANCE25JANFUT"

    def test_logs_warning_and_returns_empty_on_status_false(self, monkeypatch, caplog):
        import logging
        s = self._session(monkeypatch)
        body = {"status": False, "message": "F&O not activated"}
        _patch_client(monkeypatch, _FakeResponse(200, body))
        with caplog.at_level(logging.WARNING):
            result = run(s.get_gainers_losers())
        assert result == []
        assert "F&O not activated" in caplog.text

    def test_rate_limit_sets_cooldown(self, monkeypatch):
        s = self._session(monkeypatch)
        cooldown_set = []
        monkeypatch.setattr(ac, "_rl_set_cooldown", lambda p, s: cooldown_set.append(p))
        body = {"message": "Access denied because of exceeding access rate"}
        _patch_client(monkeypatch, _FakeResponse(403, body, text=str(body)))
        assert run(s.get_gainers_losers()) == []
        assert "angelone_gainers" in cooldown_set


# ══════════════════════════════════════════════════════════════════════════════
# get_quotes_batch
# ══════════════════════════════════════════════════════════════════════════════

class TestGetQuotesBatch:
    def _session(self, monkeypatch):
        s = _configured_session(monkeypatch)
        from datetime import datetime, timedelta
        s.token = "tok"; s.token_expiry = datetime.utcnow() + timedelta(hours=1)
        monkeypatch.setattr(ac, "_resolve_client_public_ip", lambda: "1.2.3.4")
        monkeypatch.setattr(ac, "_rl_in_cooldown", lambda p: False)
        monkeypatch.setattr(ac, "_rl_acquire", lambda *a, **kw: 0.0)
        return s

    def test_empty_tokens_returns_empty(self, monkeypatch):
        s = self._session(monkeypatch)
        assert run(s.get_quotes_batch("NSE", [])) == []

    def test_cooldown_returns_empty(self, monkeypatch):
        s = self._session(monkeypatch)
        monkeypatch.setattr(ac, "_rl_in_cooldown", lambda p: True)
        assert run(s.get_quotes_batch("NSE", ["111", "222"])) == []

    def test_returns_fetched_list(self, monkeypatch):
        s = self._session(monkeypatch)
        items = [{"ltp": 100}, {"ltp": 200}]
        body = {"data": {"fetched": items}}
        _patch_client(monkeypatch, _FakeResponse(200, body))
        result = run(s.get_quotes_batch("NSE", ["111", "222"]))
        assert len(result) == 2

    def test_rate_limit_sets_cooldown(self, monkeypatch):
        s = self._session(monkeypatch)
        cooldown_set = []
        monkeypatch.setattr(ac, "_rl_set_cooldown", lambda p, secs: cooldown_set.append(p))
        body = {"message": "Access denied because of exceeding access rate"}
        _patch_client(monkeypatch, _FakeResponse(403, body, text=str(body)))
        assert run(s.get_quotes_batch("NSE", ["111"])) == []
        assert "angelone_quote" in cooldown_set


# ══════════════════════════════════════════════════════════════════════════════
# get_session singleton
# ══════════════════════════════════════════════════════════════════════════════

def test_get_session_returns_module_singleton():
    assert ac.get_session() is ac._session
    assert ac.get_session() is ac.get_session()
