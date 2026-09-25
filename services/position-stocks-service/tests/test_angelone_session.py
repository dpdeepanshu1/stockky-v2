"""
tests/test_angelone_session.py

Covers feed/angelone_session.py (session112 round 17).

AngelOneSession is a thin async TOTP+JWT session manager. The key test
concerns are:
  * is_configured() — true only when all 4 env vars are set
  * ensure_session() — only re-logs when token is missing or expired;
    concurrent calls serialised via asyncio.Lock
  * _login() — builds the correct payload/headers, handles AngelOne's
    status=false response, surfaces jwtToken/feedToken, sets expiry
  * _resolve_client_public_ip / _get_outbound_ip — fallback chain
    (ANGELONE_STATIC_IP → cache → live fetch → 127.0.0.1)

All network calls (httpx.AsyncClient.post, httpx.get) are fully
monkeypatched — no real network required.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_angelone_session.py -q \\
        --cov=feed.angelone_session --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

# Set env vars BEFORE importing the module so config.py reads them
os.environ["ANGELONE_CLIENT_ID"] = "TEST_CLIENT"
os.environ["ANGELONE_MPIN"] = "1234"
os.environ["ANGELONE_API_KEY"] = "TESTAPIKEY"
os.environ["ANGELONE_TOTP_SECRET"] = "JBSWY3DPEHPK3PXP"  # valid base32 for pyotp
os.environ.setdefault("ANGELONE_STATIC_IP", "")

import feed.angelone_session as aosess
from feed.angelone_session import AngelOneSession


# ─── helpers ────────────────────────────────────────────────────────────────

def _make_session(**kwargs) -> AngelOneSession:
    """Fresh session with overrideable credentials."""
    sess = AngelOneSession.__new__(AngelOneSession)
    sess.client_id   = kwargs.get("client_id", "TEST_CLIENT")
    sess.mpin        = kwargs.get("mpin", "1234")
    sess.api_key     = kwargs.get("api_key", "TESTAPIKEY")
    sess.totp_secret = kwargs.get("totp_secret", "JBSWY3DPEHPK3PXP")
    sess.token       = kwargs.get("token", None)
    sess.feed_token  = kwargs.get("feed_token", None)
    sess.token_expiry = kwargs.get("token_expiry", None)
    sess._lock = asyncio.Lock()
    return sess


def _make_login_response(jwt="JWT123", feed_token="FEED456", status=True, message="SUCCESS"):
    """Build a fake httpx.Response-like object for the login endpoint."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "status": status,
        "message": message,
        "data": {"jwtToken": jwt, "feedToken": feed_token},
    }
    return resp


# ══════════════════════════════════════════════════════════════════════════════
# is_configured
# ══════════════════════════════════════════════════════════════════════════════

class TestIsConfigured:
    def test_all_set(self):
        sess = _make_session()
        assert sess.is_configured() is True

    def test_missing_client_id(self):
        sess = _make_session(client_id="")
        assert sess.is_configured() is False

    def test_missing_mpin(self):
        sess = _make_session(mpin="")
        assert sess.is_configured() is False

    def test_missing_api_key(self):
        sess = _make_session(api_key="")
        assert sess.is_configured() is False

    def test_missing_totp_secret(self):
        sess = _make_session(totp_secret="")
        assert sess.is_configured() is False


# ══════════════════════════════════════════════════════════════════════════════
# _get_outbound_ip / _resolve_client_public_ip
# ══════════════════════════════════════════════════════════════════════════════

class TestOutboundIp:
    def test_static_ip_returned_directly(self, monkeypatch):
        monkeypatch.setattr(aosess.config, "ANGELONE_STATIC_IP", "1.2.3.4")
        result = aosess._resolve_client_public_ip()
        assert result == "1.2.3.4"

    def test_cache_hit(self, monkeypatch):
        monkeypatch.setattr(aosess.config, "ANGELONE_STATIC_IP", "")
        now = time.time()
        aosess._outbound_ip_cache["ip"] = "5.6.7.8"
        aosess._outbound_ip_cache["at"] = now  # just set → not expired
        result = aosess._resolve_client_public_ip()
        assert result == "5.6.7.8"

    def test_cache_miss_triggers_fetch(self, monkeypatch):
        monkeypatch.setattr(aosess.config, "ANGELONE_STATIC_IP", "")
        aosess._outbound_ip_cache["ip"] = None
        aosess._outbound_ip_cache["at"] = 0.0

        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock()
        fake_resp.json.return_value = {"ip": "9.10.11.12"}

        with patch("httpx.get", return_value=fake_resp):
            result = aosess._resolve_client_public_ip()
        assert result == "9.10.11.12"
        assert aosess._outbound_ip_cache["ip"] == "9.10.11.12"

    def test_fetch_failure_falls_back_to_localhost(self, monkeypatch):
        monkeypatch.setattr(aosess.config, "ANGELONE_STATIC_IP", "")
        aosess._outbound_ip_cache["ip"] = None
        aosess._outbound_ip_cache["at"] = 0.0

        with patch("httpx.get", side_effect=Exception("net down")):
            result = aosess._resolve_client_public_ip()
        assert result == "127.0.0.1"

    def test_get_outbound_ip_success(self):
        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock()
        fake_resp.json.return_value = {"ip": "1.1.1.1"}
        with patch("httpx.get", return_value=fake_resp):
            result = aosess._get_outbound_ip()
        assert result == "1.1.1.1"

    def test_get_outbound_ip_failure_returns_none(self):
        with patch("httpx.get", side_effect=RuntimeError("fail")):
            result = aosess._get_outbound_ip()
        assert result is None

    def test_expired_cache_refetched(self, monkeypatch):
        monkeypatch.setattr(aosess.config, "ANGELONE_STATIC_IP", "")
        aosess._outbound_ip_cache["ip"] = "OLD"
        aosess._outbound_ip_cache["at"] = time.time() - aosess._OUTBOUND_IP_TTL_SECONDS - 1

        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock()
        fake_resp.json.return_value = {"ip": "NEW"}
        with patch("httpx.get", return_value=fake_resp):
            result = aosess._resolve_client_public_ip()
        assert result == "NEW"


# ══════════════════════════════════════════════════════════════════════════════
# _login
# ══════════════════════════════════════════════════════════════════════════════

class TestLogin:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_successful_login_sets_token(self):
        sess = _make_session()
        fake_resp = _make_login_response(jwt="JWT_OK", feed_token="FEED_OK")
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=fake_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch.object(aosess, "_resolve_client_public_ip", return_value="1.2.3.4"):
                self._run(sess._login())

        assert sess.token == "JWT_OK"
        assert sess.feed_token == "FEED_OK"
        assert sess.token_expiry is not None
        assert sess.token_expiry > datetime.utcnow()

    def test_login_status_false_raises(self):
        sess = _make_session()
        fake_resp = _make_login_response(status=False, message="INVALID_CREDENTIALS")
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=fake_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch.object(aosess, "_resolve_client_public_ip", return_value="1.2.3.4"):
                with pytest.raises(RuntimeError, match="INVALID_CREDENTIALS"):
                    self._run(sess._login())

    def test_login_not_configured_raises(self):
        sess = _make_session(client_id="", mpin="", api_key="", totp_secret="")
        with pytest.raises(RuntimeError, match="not configured"):
            self._run(sess._login())

    def test_login_sets_expiry_to_20h_from_now(self):
        sess = _make_session()
        fake_resp = _make_login_response()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=fake_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch.object(aosess, "_resolve_client_public_ip", return_value="1.2.3.4"):
                self._run(sess._login())

        # Expiry should be roughly 20 hours from now (±5 minutes tolerance)
        expected = datetime.utcnow() + timedelta(hours=20)
        diff = abs((sess.token_expiry - expected).total_seconds())
        assert diff < 300

    def test_login_correct_headers(self):
        sess = _make_session()
        fake_resp = _make_login_response()
        captured = {}

        async def fake_post(url, json=None, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return fake_resp

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = fake_post

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch.object(aosess, "_resolve_client_public_ip", return_value="42.42.42.42"):
                self._run(sess._login())

        assert captured["headers"]["X-PrivateKey"] == "TESTAPIKEY"
        assert captured["headers"]["X-ClientPublicIP"] == "42.42.42.42"
        assert "loginByPassword" in captured["url"]


# ══════════════════════════════════════════════════════════════════════════════
# ensure_session
# ══════════════════════════════════════════════════════════════════════════════

class TestEnsureSession:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _patch_login(self, sess, call_log):
        original_login = sess._login

        async def fake_login():
            call_log.append(1)
            sess.token = "JWT_FAKE"
            sess.feed_token = "FEED_FAKE"
            sess.token_expiry = datetime.utcnow() + timedelta(hours=20)

        sess._login = fake_login

    def test_no_token_triggers_login(self):
        sess = _make_session(token=None)
        calls = []
        self._patch_login(sess, calls)
        self._run(sess.ensure_session())
        assert len(calls) == 1
        assert sess.token == "JWT_FAKE"

    def test_fresh_token_skips_login(self):
        sess = _make_session(
            token="EXISTING",
            token_expiry=datetime.utcnow() + timedelta(hours=10),
        )
        calls = []
        self._patch_login(sess, calls)
        self._run(sess.ensure_session())
        assert len(calls) == 0  # still valid

    def test_expired_token_triggers_relogin(self):
        sess = _make_session(
            token="OLD_TOKEN",
            token_expiry=datetime.utcnow() - timedelta(minutes=1),  # expired
        )
        calls = []
        self._patch_login(sess, calls)
        self._run(sess.ensure_session())
        assert len(calls) == 1
        assert sess.token == "JWT_FAKE"

    def test_concurrent_ensure_serialized(self):
        """Two concurrent ensure_session calls should only trigger one _login."""
        sess = _make_session(token=None)
        calls = []
        self._patch_login(sess, calls)

        async def _two_concurrent():
            await asyncio.gather(sess.ensure_session(), sess.ensure_session())

        self._run(_two_concurrent())
        assert len(calls) == 1  # lock ensures only one login fires


# ══════════════════════════════════════════════════════════════════════════════
# get_session (module-level singleton)
# ══════════════════════════════════════════════════════════════════════════════

class TestGetSession:
    def test_returns_angelone_session_instance(self):
        sess = aosess.get_session()
        assert isinstance(sess, AngelOneSession)

    def test_returns_same_singleton(self):
        a = aosess.get_session()
        b = aosess.get_session()
        assert a is b
