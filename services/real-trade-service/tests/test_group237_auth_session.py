"""
group237: GET /auth/session answers "is this Bearer token a usable admin session?" with booleans only and never a
401, so the dashboard can check a stored token once before its first protected REAL request.

    cd services/real-trade-service
    python -m pytest tests/test_group237_auth_session.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt
import pytest

import main
from auth import admin_auth

_SECRET = "test-secret-group237-0123456789abcdef"


def _call(authorization: str = ""):
    return asyncio.run(main.auth_session(authorization))


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(admin_auth.config, "SESSION_SECRET", _SECRET)


def _token(exp_offset=600, secret=_SECRET, sub="admin"):
    now = time.time()
    return jwt.encode({"sub": sub, "iat": now, "exp": now + exp_offset}, secret, algorithm="HS256")


def test_valid_token_is_valid():
    assert _call(f"Bearer {_token()}") == {"valid": True}


def test_expired_token_is_not_valid_and_does_not_raise():
    assert _call(f"Bearer {_token(exp_offset=-10)}") == {"valid": False}


def test_garbage_token():
    assert _call("Bearer not-a-jwt") == {"valid": False}


def test_wrong_secret_token():
    assert _call(f"Bearer {_token(secret='another-secret-another-secret-123456')}") == {"valid": False}


def test_missing_header_and_wrong_scheme():
    assert _call("") == {"valid": False}
    assert _call("Basic abc") == {"valid": False}


def test_token_without_subject():
    now = time.time()
    t = jwt.encode({"iat": now, "exp": now + 600}, _SECRET, algorithm="HS256")
    assert _call(f"Bearer {t}") == {"valid": False}


def test_no_secret_configured_is_not_valid(monkeypatch):
    monkeypatch.setattr(admin_auth.config, "SESSION_SECRET", "")
    assert _call(f"Bearer {_token()}") == {"valid": False}


def test_route_is_registered_without_auth_dependency():
    route = next(r for r in main.app.routes if getattr(r, "path", "") == "/auth/session")
    assert "GET" in route.methods
    deps = [d.call.__name__ for d in route.dependant.dependencies]
    assert "require_admin" not in deps and "require_admin_if_real" not in deps
