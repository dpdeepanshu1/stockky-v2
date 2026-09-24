"""
tests/test_admin_auth.py

Covers auth/admin_auth.py — previously 29%, missing lines 45-56, 60-66,
72-80, 88-94, 103-106, 118-125. This is the Layer-1 gate in front of every
mutating position-stocks-service route (real money), so it gets exhaustive
branch coverage rather than just the missing lines: every function, every
branch, every failure mode. Ported from real-trade-service's
tests/test_admin_auth.py after confirming the two auth/admin_auth.py
copies are identical apart from docstrings and one function this service
doesn't have — this service has no DEMO/REAL mode split (it's real-money-
only), so there is no require_admin_if_real() here and no equivalent test
class; every other function and test body carries over unchanged.

  verify_admin_password
    - no hash configured                    -> AdminAuthError
    - wrong username                        -> False, Argon2 never consulted
    - right username + right password       -> True (real Argon2id hash)
    - right username + wrong password       -> False (VerifyMismatchError)
    - malformed stored hash                 -> False (InvalidHashError)
    - unexpected verifier crash             -> False + WARNING, password never logged
  issue_session_token
    - no secret                             -> AdminAuthError
    - happy path                            -> decodable HS256 JWT, expiry = now + timeout
  decode_session_token
    - no secret / expired / garbage / wrong-secret / alg=none / missing `sub`
    - happy path
  require_admin
    - missing header, non-Bearer scheme, bad token, expired token -> 401
    - valid token -> username
  auth_config_diagnostics / log_auth_config
    - every ERROR branch (no secret, no hash, non-argon2 hash) and the
      all-good path (no ERROR emitted). Secret itself never appears in output.

Uses REAL Argon2id + REAL PyJWT (no mocking of the crypto) so a library
upgrade that changes exception behaviour fails here, not in production.
The Argon2 cost params are dialled down purely for test speed —
PasswordHasher.verify() reads the params from the hash itself, so the
production code path is unchanged.

    cd services/position-stocks-service
    python -m pytest tests/test_admin_auth.py -v --cov=auth.admin_auth --cov-report=term-missing
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import jwt
import pytest
from argon2 import PasswordHasher
from fastapi import HTTPException

import config
from auth import admin_auth
from auth.admin_auth import (
    AdminAuthError,
    auth_config_diagnostics,
    decode_session_token,
    issue_session_token,
    log_auth_config,
    require_admin,
    verify_admin_password,
)

SECRET = "unit-test-session-secret-not-for-prod"
USERNAME = "admin"
PASSWORD = "correct horse battery staple"

# Cheap params: test speed only. verify() takes its params from the hash.
_fast_hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
GOOD_HASH = _fast_hasher.hash(PASSWORD)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    """Known-good baseline for every test; individual tests override."""
    monkeypatch.setattr(config, "ADMIN_USERNAME", USERNAME)
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", GOOD_HASH)
    monkeypatch.setattr(config, "SESSION_SECRET", SECRET)
    monkeypatch.setattr(config, "SESSION_IDLE_TIMEOUT_MINUTES", 30)


# ───────────────────────── verify_admin_password ─────────────────────────

class TestVerifyAdminPassword:
    def test_no_hash_configured_raises(self, monkeypatch):
        monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "")
        with pytest.raises(AdminAuthError, match="ADMIN_PASSWORD_HASH"):
            verify_admin_password(USERNAME, PASSWORD)

    def test_none_hash_also_raises(self, monkeypatch):
        monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", None)
        with pytest.raises(AdminAuthError):
            verify_admin_password(USERNAME, PASSWORD)

    def test_wrong_username_returns_false_without_touching_hasher(self, monkeypatch):
        class _NeverCalled:  # PasswordHasher uses __slots__, so swap the whole object
            def verify(self, *a, **k):
                raise AssertionError("hasher consulted for a wrong username")  # pragma: no cover

        monkeypatch.setattr(admin_auth, "_hasher", _NeverCalled())
        assert verify_admin_password("mallory", PASSWORD) is False

    def test_empty_username_returns_false(self):
        assert verify_admin_password("", PASSWORD) is False

    def test_username_is_case_sensitive(self):
        assert verify_admin_password("ADMIN", PASSWORD) is False

    def test_correct_credentials_return_true(self):
        assert verify_admin_password(USERNAME, PASSWORD) is True

    def test_wrong_password_returns_false(self):
        assert verify_admin_password(USERNAME, "not the password") is False

    def test_empty_password_returns_false(self):
        assert verify_admin_password(USERNAME, "") is False

    def test_password_is_case_sensitive(self):
        assert verify_admin_password(USERNAME, PASSWORD.upper()) is False

    def test_custom_admin_username_is_honoured(self, monkeypatch):
        monkeypatch.setattr(config, "ADMIN_USERNAME", "deepanshu")
        assert verify_admin_password("deepanshu", PASSWORD) is True
        assert verify_admin_password("admin", PASSWORD) is False

    def test_malformed_stored_hash_returns_false(self, monkeypatch):
        """e.g. docker-compose ate the '$' characters — InvalidHashError path.
        Must fail closed (False), not crash the login route with a 500."""
        monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "argon2idv19m8t1p1notarealhash")
        assert verify_admin_password(USERNAME, PASSWORD) is False

    def test_unexpected_verifier_error_fails_closed_and_logs_no_password(
        self, monkeypatch, caplog
    ):
        class _Exploding:  # PasswordHasher uses __slots__, so swap the whole object
            def verify(self, *a, **k):
                raise RuntimeError("argon2 backend exploded")

        monkeypatch.setattr(admin_auth, "_hasher", _Exploding())
        with caplog.at_level(logging.WARNING, logger="position-stocks-auth"):
            assert verify_admin_password(USERNAME, PASSWORD) is False

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "argon2 backend exploded" in warnings[0].getMessage()
        # the plaintext password must never reach the logs
        assert PASSWORD not in caplog.text


# ───────────────────────── issue_session_token ─────────────────────────

class TestIssueSessionToken:
    def test_no_secret_raises(self, monkeypatch):
        monkeypatch.setattr(config, "SESSION_SECRET", "")
        with pytest.raises(AdminAuthError, match="SESSION_SECRET"):
            issue_session_token(USERNAME)

    def test_happy_path_token_is_valid_hs256_with_expected_claims(self):
        before = datetime.now(timezone.utc)
        token, expires_at = issue_session_token(USERNAME)
        after = datetime.now(timezone.utc)

        assert jwt.get_unverified_header(token)["alg"] == "HS256"
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
        assert payload["sub"] == USERNAME
        assert payload["exp"] > payload["iat"]

        # returned expiry == now + idle timeout, and matches the token's exp
        assert before + timedelta(minutes=30) <= expires_at <= after + timedelta(minutes=30)
        assert expires_at.tzinfo is not None
        assert abs(payload["exp"] - expires_at.timestamp()) < 1

    def test_expiry_follows_configured_idle_timeout(self, monkeypatch):
        monkeypatch.setattr(config, "SESSION_IDLE_TIMEOUT_MINUTES", 5)
        _, expires_at = issue_session_token(USERNAME)
        remaining = expires_at - datetime.now(timezone.utc)
        assert timedelta(minutes=4, seconds=55) < remaining <= timedelta(minutes=5)

    def test_token_is_not_verifiable_with_a_different_secret(self):
        token, _ = issue_session_token(USERNAME)
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(token, "some-other-secret", algorithms=["HS256"])


# ───────────────────────── decode_session_token ─────────────────────────

def _forge(claims: dict, secret: str = SECRET, alg: str = "HS256") -> str:
    return jwt.encode(claims, secret, algorithm=alg)


class TestDecodeSessionToken:
    def test_round_trip_returns_username(self):
        token, _ = issue_session_token(USERNAME)
        assert decode_session_token(token) == USERNAME

    def test_no_secret_returns_none_even_for_a_formerly_valid_token(self, monkeypatch):
        token, _ = issue_session_token(USERNAME)
        monkeypatch.setattr(config, "SESSION_SECRET", "")
        assert decode_session_token(token) is None

    def test_expired_token_returns_none(self):
        now = datetime.now(timezone.utc)
        token = _forge({
            "sub": USERNAME,
            "iat": (now - timedelta(hours=2)).timestamp(),
            "exp": (now - timedelta(hours=1)).timestamp(),
        })
        assert decode_session_token(token) is None

    def test_garbage_token_returns_none(self):
        assert decode_session_token("this.is.not-a-jwt") is None
        assert decode_session_token("") is None
        assert decode_session_token("x") is None

    def test_token_signed_with_other_secret_returns_none(self):
        exp = (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
        token = _forge({"sub": USERNAME, "exp": exp}, secret="attacker-secret")
        assert decode_session_token(token) is None

    def test_tampered_payload_returns_none(self):
        token, _ = issue_session_token(USERNAME)
        header, _payload, sig = token.split(".")
        forged_payload = jwt.utils.base64url_encode(
            b'{"sub":"root","exp":9999999999}'
        ).decode()
        assert decode_session_token(f"{header}.{forged_payload}.{sig}") is None

    def test_alg_none_token_is_rejected(self):
        """Classic JWT downgrade attack: unsigned token claiming alg=none."""
        exp = (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
        unsigned = jwt.encode({"sub": USERNAME, "exp": exp}, key=None, algorithm="none")
        assert decode_session_token(unsigned) is None

    def test_other_hmac_algorithm_is_rejected(self):
        """Only HS256 is allow-listed; an HS512 token with the right secret
        must not be accepted."""
        exp = (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
        token = _forge({"sub": USERNAME, "exp": exp}, alg="HS512")
        assert decode_session_token(token) is None

    def test_valid_signature_but_no_sub_claim_returns_none(self):
        exp = (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
        assert decode_session_token(_forge({"exp": exp})) is None


# ───────────────────────── require_admin ─────────────────────────

class TestRequireAdmin:
    def test_missing_header_is_401(self):
        with pytest.raises(HTTPException) as ei:
            require_admin("")
        assert ei.value.status_code == 401
        assert "Missing" in ei.value.detail

    @pytest.mark.parametrize("hdr", ["Basic abc123", "bearer lowercase-scheme", "Token abc", "abc"])
    def test_non_bearer_scheme_is_401(self, hdr):
        with pytest.raises(HTTPException) as ei:
            require_admin(hdr)
        assert ei.value.status_code == 401

    def test_bearer_with_garbage_token_is_401(self):
        with pytest.raises(HTTPException) as ei:
            require_admin("Bearer not-a-real-token")
        assert ei.value.status_code == 401
        assert "Invalid or expired" in ei.value.detail

    def test_bearer_with_empty_token_is_401(self):
        with pytest.raises(HTTPException) as ei:
            require_admin("Bearer ")
        assert ei.value.status_code == 401

    def test_expired_token_is_401(self):
        now = datetime.now(timezone.utc)
        token = _forge({
            "sub": USERNAME,
            "iat": (now - timedelta(hours=2)).timestamp(),
            "exp": (now - timedelta(minutes=1)).timestamp(),
        })
        with pytest.raises(HTTPException) as ei:
            require_admin(f"Bearer {token}")
        assert ei.value.status_code == 401

    def test_valid_token_returns_username(self):
        token, _ = issue_session_token(USERNAME)
        assert require_admin(f"Bearer {token}") == USERNAME

    def test_surrounding_whitespace_in_token_is_tolerated(self):
        token, _ = issue_session_token(USERNAME)
        assert require_admin(f"Bearer   {token}  ") == USERNAME

    def test_secret_rotated_after_issue_invalidates_session(self, monkeypatch):
        token, _ = issue_session_token(USERNAME)
        monkeypatch.setattr(config, "SESSION_SECRET", "rotated-secret")
        with pytest.raises(HTTPException) as ei:
            require_admin(f"Bearer {token}")
        assert ei.value.status_code == 401


# ─────────────────── auth_config_diagnostics / log_auth_config ───────────────────

class TestAuthConfigDiagnostics:
    def test_fully_configured_snapshot(self):
        d = auth_config_diagnostics()
        assert d == {
            "session_secret_configured": True,
            "session_secret_length": len(SECRET),
            "session_secret_fingerprint": hashlib.sha256(SECRET.encode()).hexdigest()[:8],
            "admin_username_configured": True,
            "admin_password_hash_configured": True,
            "admin_password_hash_looks_argon2": True,
            "session_idle_timeout_minutes": 30,
        }

    def test_snapshot_never_contains_the_secret_or_hash(self):
        blob = repr(auth_config_diagnostics())
        assert SECRET not in blob
        assert GOOD_HASH not in blob

    def test_missing_secret_and_hash(self, monkeypatch):
        monkeypatch.setattr(config, "SESSION_SECRET", "")
        monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "")
        monkeypatch.setattr(config, "ADMIN_USERNAME", "")
        d = auth_config_diagnostics()
        assert d["session_secret_configured"] is False
        assert d["session_secret_length"] == 0
        assert d["session_secret_fingerprint"] is None
        assert d["admin_username_configured"] is False
        assert d["admin_password_hash_configured"] is False
        assert d["admin_password_hash_looks_argon2"] is False

    def test_none_values_are_handled_like_empty(self, monkeypatch):
        monkeypatch.setattr(config, "SESSION_SECRET", None)
        monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", None)
        d = auth_config_diagnostics()
        assert d["session_secret_configured"] is False
        assert d["session_secret_fingerprint"] is None
        assert d["admin_password_hash_configured"] is False

    def test_dollar_stripped_hash_is_flagged_not_argon2(self, monkeypatch):
        monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "argon2idv19m65536t3p4saltHASH")
        d = auth_config_diagnostics()
        assert d["admin_password_hash_configured"] is True
        assert d["admin_password_hash_looks_argon2"] is False

    def test_same_secret_gives_same_fingerprint_different_secret_differs(self, monkeypatch):
        fp1 = auth_config_diagnostics()["session_secret_fingerprint"]
        assert auth_config_diagnostics()["session_secret_fingerprint"] == fp1
        monkeypatch.setattr(config, "SESSION_SECRET", SECRET + "-other")
        assert auth_config_diagnostics()["session_secret_fingerprint"] != fp1


class TestLogAuthConfig:
    def _run(self, caplog):
        with caplog.at_level(logging.INFO, logger="position-stocks-auth"):
            log_auth_config("position-stocks-service")
        return caplog.records

    def test_all_good_logs_info_only_no_errors(self, caplog):
        recs = self._run(caplog)
        assert [r.levelno for r in recs] == [logging.INFO]
        msg = recs[0].getMessage()
        assert "AUTH CONFIG [position-stocks-service]" in msg
        assert SECRET not in msg and GOOD_HASH not in msg

    def test_missing_secret_logs_error(self, monkeypatch, caplog):
        monkeypatch.setattr(config, "SESSION_SECRET", "")
        errors = [r.getMessage() for r in self._run(caplog) if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "SESSION_SECRET is empty" in errors[0]
        assert "[position-stocks-service]" in errors[0]

    def test_missing_hash_logs_login_impossible_error(self, monkeypatch, caplog):
        monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "")
        errors = [r.getMessage() for r in self._run(caplog) if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "login is impossible" in errors[0]
        assert "does not start with" not in errors[0]

    def test_non_argon2_hash_logs_dollar_interpolation_hint(self, monkeypatch, caplog):
        monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "argon2idv19m65536t3p4saltHASH")
        errors = [r.getMessage() for r in self._run(caplog) if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "does not start with '$argon2'" in errors[0]
        assert "ADMIN_PASSWORD_HASH_B64" in errors[0]

    def test_everything_missing_logs_both_errors(self, monkeypatch, caplog):
        monkeypatch.setattr(config, "SESSION_SECRET", "")
        monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "")
        errors = [r.getMessage() for r in self._run(caplog) if r.levelno == logging.ERROR]
        assert len(errors) == 2
        assert any("SESSION_SECRET is empty" in e for e in errors)
        assert any("login is impossible" in e for e in errors)
