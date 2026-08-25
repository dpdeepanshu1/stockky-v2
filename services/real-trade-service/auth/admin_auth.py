"""
auth/admin_auth.py — Layer 1 auth (Stockky admin, not Dhan).

Argon2id password check against config.ADMIN_PASSWORD_HASH (set once via
Render env, generated offline — see config.py's docstring for the exact
command). Session tokens are short-lived signed JWTs; every mutating route
in main.py re-validates via require_admin(), not just at login. This is
deliberately its OWN session mechanism, separate from any existing Stockky
user session, because this surface's blast radius (real money) should never
share a trust boundary with the rest of the app.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from fastapi import Depends, HTTPException, Header

import config

logger = logging.getLogger("real-trade-auth")
_hasher = PasswordHasher()


class AdminAuthError(Exception):
    pass


def verify_admin_password(username: str, password: str) -> bool:
    """True only if username matches config.ADMIN_USERNAME AND the password
    verifies against the stored Argon2id hash. Never logs the password;
    never distinguishes "wrong username" from "wrong password" in the
    response (main.py returns a generic 401 either way) to avoid
    username enumeration."""
    if not config.ADMIN_PASSWORD_HASH:
        raise AdminAuthError("ADMIN_PASSWORD_HASH not configured on this deploy.")
    if username != config.ADMIN_USERNAME:
        return False
    try:
        _hasher.verify(config.ADMIN_PASSWORD_HASH, password)
        return True
    except (VerifyMismatchError, InvalidHashError):
        return False
    except Exception as e:
        logger.warning("admin password verify failed unexpectedly: %s", e)
        return False


def issue_session_token(username: str) -> tuple[str, datetime]:
    if not config.SESSION_SECRET:
        raise AdminAuthError("SESSION_SECRET not configured on this deploy.")
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=config.SESSION_IDLE_TIMEOUT_MINUTES)
    payload = {"sub": username, "iat": now.timestamp(), "exp": expires_at.timestamp()}
    token = jwt.encode(payload, config.SESSION_SECRET, algorithm="HS256")
    return token, expires_at


def decode_session_token(token: str) -> Optional[str]:
    """Returns the admin username if the token is valid and unexpired, else
    None. jwt.decode already enforces `exp` for us."""
    if not config.SESSION_SECRET:
        return None
    try:
        payload = jwt.decode(token, config.SESSION_SECRET, algorithms=["HS256"])
        return payload.get("sub")
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def require_admin(authorization: str = Header(default="")) -> str:
    """FastAPI dependency — put on every route that reads/mutates real-trade
    state. Expects `Authorization: Bearer <token>`. Raises 401 on missing/
    invalid/expired token — this is the re-validation the module docstring
    promises, not a one-time login check."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing admin session token")
    token = authorization[len("Bearer "):].strip()
    username = decode_session_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="Invalid or expired admin session")
    return username
