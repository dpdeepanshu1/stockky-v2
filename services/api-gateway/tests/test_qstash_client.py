"""
Tests for qstash_client.py: enabled(), publish(), schedule_gateway_tick(),
and verify_signature().

verify_signature() coverage: session112-round32 finding — it fail-opened on
a missing PyJWT dependency with zero logging (unlike every other fail-open
path in this codebase), and only checked that iss/sub/exp were *present*,
never that they matched expected values. Both are fixed and covered here.

enabled()/publish()/schedule_gateway_tick() coverage added this round to
bring qstash_client.py to 100% (previously untested; only verify_signature
had tests).
"""
import builtins
import hashlib
import base64
import logging
import time

import pytest
import jwt as pyjwt

import qstash_client


DEST_URL = "https://gateway.example.com/ops/qstash/tick"


def _b64url_body_hash(body: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode("ascii").rstrip("=")


def _make_token(
    key: str,
    *,
    body: bytes = b"",
    iss: str = "Upstash",
    sub: str = DEST_URL,
    exp_delta: int = 300,
    include_body_claim: bool = True,
    algorithm: str = "HS256",
) -> str:
    claims = {
        "iss": iss,
        "sub": sub,
        "exp": int(time.time()) + exp_delta,
        "iat": int(time.time()),
    }
    if include_body_claim:
        claims["body"] = _b64url_body_hash(body)
    return pyjwt.encode(claims, key, algorithm=algorithm)


@pytest.fixture(autouse=True)
def _signing_keys(monkeypatch):
    """Default: both signing keys configured, distinct values."""
    monkeypatch.setattr(qstash_client, "SIGN_CURRENT", "current-signing-key")
    monkeypatch.setattr(qstash_client, "SIGN_NEXT", "next-signing-key")
    yield


class TestNoKeysConfigured:
    def test_accepts_when_no_signing_keys_set(self, monkeypatch):
        monkeypatch.setattr(qstash_client, "SIGN_CURRENT", "")
        monkeypatch.setattr(qstash_client, "SIGN_NEXT", "")
        assert qstash_client.verify_signature("anything", b"body") is True

    def test_accepts_empty_header_when_no_keys_set(self, monkeypatch):
        monkeypatch.setattr(qstash_client, "SIGN_CURRENT", "")
        monkeypatch.setattr(qstash_client, "SIGN_NEXT", "")
        assert qstash_client.verify_signature("", b"body") is True


class TestMalformedHeader:
    def test_rejects_empty_signature_header(self):
        assert qstash_client.verify_signature("", b"body") is False

    def test_rejects_header_without_three_segments(self):
        assert qstash_client.verify_signature("not.a.valid.jwt.shape", b"body") is False
        assert qstash_client.verify_signature("onlyonesegment", b"body") is False


class TestValidSignature:
    def test_accepts_valid_current_key_signature(self):
        body = b'{"source": "qstash"}'
        token = _make_token(qstash_client.SIGN_CURRENT, body=body)
        assert qstash_client.verify_signature(token, body, expected_url=DEST_URL) is True

    def test_accepts_valid_next_key_signature_as_fallback(self):
        """Current-key verification fails, next-key succeeds — rotation support."""
        body = b"{}"
        token = _make_token(qstash_client.SIGN_NEXT, body=body)
        assert qstash_client.verify_signature(token, body, expected_url=DEST_URL) is True

    def test_accepts_when_expected_url_not_provided(self):
        """Backward-compatible: omitting expected_url skips the sub pin."""
        body = b"{}"
        token = _make_token(qstash_client.SIGN_CURRENT, body=body, sub="https://anything/at/all")
        assert qstash_client.verify_signature(token, body) is True

    def test_accepts_when_body_claim_absent(self):
        """QStash always sends a body claim, but don't hard-fail if a token omits it."""
        body = b"{}"
        token = _make_token(qstash_client.SIGN_CURRENT, body=body, include_body_claim=False)
        assert qstash_client.verify_signature(token, body, expected_url=DEST_URL) is True


class TestInvalidSignature:
    def test_rejects_signature_from_unknown_key(self):
        body = b"{}"
        token = _make_token("some-other-key-not-configured", body=body)
        assert qstash_client.verify_signature(token, body, expected_url=DEST_URL) is False

    def test_rejects_expired_token(self):
        body = b"{}"
        token = _make_token(qstash_client.SIGN_CURRENT, body=body, exp_delta=-60)
        assert qstash_client.verify_signature(token, body, expected_url=DEST_URL) is False

    def test_rejects_wrong_issuer(self):
        """The 'iss' claim must be exactly 'Upstash' per QStash's spec — this is
        the core of the finding: previously only *presence* of iss was checked,
        never its value, so any issuer string would pass."""
        body = b"{}"
        token = _make_token(qstash_client.SIGN_CURRENT, body=body, iss="NotUpstash")
        assert qstash_client.verify_signature(token, body, expected_url=DEST_URL) is False

    def test_rejects_sub_mismatch_when_expected_url_given(self):
        """The 'sub' claim must match the destination URL — previously any
        sub value would pass as long as the field was present."""
        body = b"{}"
        token = _make_token(qstash_client.SIGN_CURRENT, body=body, sub="https://attacker.example.com/evil")
        assert qstash_client.verify_signature(token, body, expected_url=DEST_URL) is False

    def test_rejects_body_hash_mismatch(self):
        """A validly-signed token for a *different* body must not verify this one —
        guards against replaying an old valid callback with tampered content."""
        original_body = b'{"source": "qstash"}'
        tampered_body = b'{"source": "attacker"}'
        token = _make_token(qstash_client.SIGN_CURRENT, body=original_body)
        assert qstash_client.verify_signature(token, tampered_body, expected_url=DEST_URL) is False

    def test_rejects_when_neither_key_matches_and_iss_also_wrong(self):
        """Sanity: failure reasons compose — last_err reflects the last key tried,
        overall result is still False."""
        body = b"{}"
        token = _make_token("unconfigured-key", body=body, iss="NotUpstash")
        assert qstash_client.verify_signature(token, body, expected_url=DEST_URL) is False


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", has_content=True):
        self.status_code = status_code
        self._json_data = {} if json_data is None else json_data
        self.text = text
        self.content = b"x" if has_content else b""

    def json(self):
        return self._json_data


class _FakeClient:
    """Stand-in for httpx.Client used as a context manager in publish()."""

    def __init__(self, response=None, raise_exc=None, captured=None, **kwargs):
        self._response = response
        self._raise_exc = raise_exc
        self._captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def post(self, url, json=None, headers=None):
        if self._captured is not None:
            self._captured["url"] = url
            self._captured["json"] = json
            self._captured["headers"] = headers
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


class TestEnabled:
    def test_false_when_token_not_set(self, monkeypatch):
        monkeypatch.setattr(qstash_client, "QSTASH_TOKEN", "")
        assert qstash_client.enabled() is False

    def test_true_when_token_set(self, monkeypatch):
        monkeypatch.setattr(qstash_client, "QSTASH_TOKEN", "some-token")
        assert qstash_client.enabled() is True


class TestPublish:
    def test_returns_error_when_token_not_set(self, monkeypatch):
        monkeypatch.setattr(qstash_client, "QSTASH_TOKEN", "")
        result = qstash_client.publish("https://gateway.example.com/ops/tick")
        assert result == {"ok": False, "error": "QSTASH_TOKEN not set"}

    def test_rejects_non_http_destination(self, monkeypatch):
        monkeypatch.setattr(qstash_client, "QSTASH_TOKEN", "tok")
        result = qstash_client.publish("not-a-url")
        assert result["ok"] is False
        assert "invalid destination" in result["error"]

    def test_successful_publish_builds_headers_and_returns_data(self, monkeypatch):
        monkeypatch.setattr(qstash_client, "QSTASH_TOKEN", "tok")
        captured = {}
        response = _FakeResponse(status_code=200, json_data={"messageId": "abc123"})
        monkeypatch.setattr(
            qstash_client.httpx,
            "Client",
            lambda *a, **kw: _FakeClient(response=response, captured=captured),
        )
        result = qstash_client.publish(
            "  https://gateway.example.com/ops/tick  ",
            {"a": 1},
            delay_seconds=30,
            retries=99,
            headers={"X-Custom": "value"},
        )
        assert result == {"ok": True, "messageId": "abc123"}
        assert captured["url"] == f"{qstash_client.QSTASH_URL.rstrip('/')}/https://gateway.example.com/ops/tick"
        assert captured["headers"]["Authorization"] == "Bearer tok"
        assert captured["headers"]["Upstash-Retries"] == "5"  # clamped to max 5
        assert captured["headers"]["Upstash-Delay"] == "30s"
        assert captured["headers"]["Upstash-Forward-X-Custom"] == "value"

    def test_successful_publish_with_no_body_and_no_delay(self, monkeypatch):
        monkeypatch.setattr(qstash_client, "QSTASH_TOKEN", "tok")
        captured = {}
        response = _FakeResponse(status_code=200, json_data={"messageId": "xyz"})
        monkeypatch.setattr(
            qstash_client.httpx,
            "Client",
            lambda *a, **kw: _FakeClient(response=response, captured=captured),
        )
        result = qstash_client.publish("https://gateway.example.com/ops/tick")
        assert result == {"ok": True, "messageId": "xyz"}
        assert captured["json"] == {}
        assert "Upstash-Delay" not in captured["headers"]

    def test_error_status_returns_ok_false_with_body_snippet(self, monkeypatch):
        monkeypatch.setattr(qstash_client, "QSTASH_TOKEN", "tok")
        response = _FakeResponse(status_code=500, text="server exploded")
        monkeypatch.setattr(
            qstash_client.httpx, "Client", lambda *a, **kw: _FakeClient(response=response)
        )
        result = qstash_client.publish("https://gateway.example.com/ops/tick")
        assert result == {"ok": False, "status": 500, "body": "server exploded"}

    def test_empty_response_content_returns_empty_data(self, monkeypatch):
        monkeypatch.setattr(qstash_client, "QSTASH_TOKEN", "tok")
        response = _FakeResponse(status_code=200, has_content=False)
        monkeypatch.setattr(
            qstash_client.httpx, "Client", lambda *a, **kw: _FakeClient(response=response)
        )
        result = qstash_client.publish("https://gateway.example.com/ops/tick")
        assert result == {"ok": True}

    def test_network_exception_returns_ok_false_with_error(self, monkeypatch):
        monkeypatch.setattr(qstash_client, "QSTASH_TOKEN", "tok")
        monkeypatch.setattr(
            qstash_client.httpx,
            "Client",
            lambda *a, **kw: _FakeClient(raise_exc=RuntimeError("connection refused")),
        )
        result = qstash_client.publish("https://gateway.example.com/ops/tick")
        assert result == {"ok": False, "error": "connection refused"}


class TestScheduleGatewayTick:
    def test_error_when_api_gateway_url_missing(self, monkeypatch):
        monkeypatch.delenv("API_GATEWAY_URL", raising=False)
        result = qstash_client.schedule_gateway_tick()
        assert result == {"ok": False, "error": "API_GATEWAY_URL not set"}

    def test_calls_publish_with_composed_url_and_default_body(self, monkeypatch):
        monkeypatch.setenv("API_GATEWAY_URL", "https://gw.example.com/")
        captured = {}

        def _fake_publish(destination_url, body=None, *, delay_seconds=0, retries=2, headers=None):
            captured["destination_url"] = destination_url
            captured["body"] = body
            captured["delay_seconds"] = delay_seconds
            return {"ok": True}

        monkeypatch.setattr(qstash_client, "publish", _fake_publish)
        result = qstash_client.schedule_gateway_tick(delay_seconds=15)
        assert result == {"ok": True}
        assert captured["destination_url"] == "https://gw.example.com/ops/qstash/tick"
        assert captured["body"] == {"source": "qstash"}
        assert captured["delay_seconds"] == 15

    def test_calls_publish_with_custom_path_and_body(self, monkeypatch):
        monkeypatch.setenv("API_GATEWAY_URL", "https://gw.example.com")
        captured = {}

        def _fake_publish(destination_url, body=None, *, delay_seconds=0, retries=2, headers=None):
            captured["destination_url"] = destination_url
            captured["body"] = body
            return {"ok": True}

        monkeypatch.setattr(qstash_client, "publish", _fake_publish)
        result = qstash_client.schedule_gateway_tick("/custom/path", body={"foo": "bar"})
        assert result == {"ok": True}
        assert captured["destination_url"] == "https://gw.example.com/custom/path"
        assert captured["body"] == {"foo": "bar"}


class TestMissingPyJWTFailsOpenWithLogging:
    """The actual bug fixed this round: fail-open on ImportError, but now with
    a loud logger.warning instead of silent acceptance."""

    def test_fails_open_when_pyjwt_not_installed(self, monkeypatch):
        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "jwt":
                raise ImportError("No module named 'jwt'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        # A syntactically-valid 3-segment header so we reach the import.
        result = qstash_client.verify_signature("a.b.c", b"body")
        assert result is True

    def test_logs_warning_when_pyjwt_not_installed(self, monkeypatch, caplog):
        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "jwt":
                raise ImportError("No module named 'jwt'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        with caplog.at_level(logging.WARNING, logger="qstash"):
            qstash_client.verify_signature("a.b.c", b"body")
        assert any(
            "PyJWT not installed" in r.message and "fail-open" in r.message
            for r in caplog.records
        ), "expected a fail-open warning to be logged, found: %r" % (
            [r.message for r in caplog.records],
        )
