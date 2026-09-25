"""
Tests for qstash_client.py's verify_signature().

Covers the session112-round32 finding: verify_signature() fail-opened on a
missing PyJWT dependency with zero logging (unlike every other fail-open
path in this codebase), and only checked that iss/sub/exp were *present*,
never that they matched expected values. Both are fixed and covered here.
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
