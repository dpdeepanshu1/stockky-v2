"""Round 298 (#9): a token that says it is not an admin is refused; the login tokens real-trade / position-stocks issue (only `sub`) and this service's own token still pass."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("jwt")
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("SESSION_SECRET", "route-test-secret-0123456789")

import auth  # noqa: E402


@pytest.mark.parametrize("claims,ok", [
    ({"sub": "deepanshu"}, True), ({"sub": "calibration-runner", "role": "admin", "admin": True, "svc": "calibration-runner"}, True), ({"role": "admin"}, True), ({"admin": True}, True),
    ({"sub": "x", "admin": False}, False), ({"sub": "x", "admin": "false"}, False), ({"sub": "x", "role": "viewer"}, False),
    ({}, False), ({"sub": ""}, False), ({"sub": "  "}, False), ({"sub": None}, False),
])
def test_is_admin(claims, ok):
    assert auth.is_admin(claims) is ok


def test_the_token_this_service_mints_is_still_an_admin():
    assert auth.is_admin({"sub": "calibration-runner", "role": "admin", "admin": True, "svc": "calibration-runner"})
