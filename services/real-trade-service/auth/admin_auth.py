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
from fastapi import HTTPException, Header

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


def require_admin_if_real(mode: str, authorization: str = Header(default="")) -> Optional[str]:
    """2026-08-25: DEMO mode is intentionally open — no login, no gate
    sequence, nothing to configure before you can start paper-trading.
    REAL mode keeps the full admin-authenticated requirement, because it's
    the one that can touch an actual brokerage account. This dependency is
    the single place that split lives: every mode-parameterized route uses
    THIS instead of require_admin, so DEMO vs REAL enforcement can never
    drift route-by-route.

    Returns the admin username for REAL (after verifying it same as
    require_admin), or None for DEMO (no identity to return — there was no
    login)."""
    if mode.upper() != "REAL":
        return None
    return require_admin(authorization)



def auth_config_diagnostics() -> dict:
    """Session 72 (#9). Non-secret snapshot of this service's admin-auth config.
    The fingerprint is the first 8 hex chars of SHA-256(SESSION_SECRET): if the
    two services report DIFFERENT fingerprints, tokens issued by one are rejected
    by the other (401 everywhere -> 'admin session broken')."""
    import hashlib
    secret = config.SESSION_SECRET or ""
    h = config.ADMIN_PASSWORD_HASH or ""
    return {
        "session_secret_configured": bool(secret),
        "session_secret_length": len(secret),
        "session_secret_fingerprint": hashlib.sha256(secret.encode()).hexdigest()[:8] if secret else None,
        "admin_username_configured": bool(config.ADMIN_USERNAME),
        "admin_password_hash_configured": bool(h),
        "admin_password_hash_looks_argon2": h.startswith("$argon2"),
        "session_idle_timeout_minutes": config.SESSION_IDLE_TIMEOUT_MINUTES,
    }


def log_auth_config(service: str) -> None:
    d = auth_config_diagnostics()
    logger.info("AUTH CONFIG [%s]: %s", service, d)
    if not d["session_secret_configured"]:
        logger.error("AUTH CONFIG [%s]: SESSION_SECRET is empty — every admin route will 401.", service)
    if not d["admin_password_hash_configured"]:
        logger.error("AUTH CONFIG [%s]: no ADMIN_PASSWORD_HASH(_B64) — login is impossible.", service)
    elif not d["admin_password_hash_looks_argon2"]:
        logger.error("AUTH CONFIG [%s]: ADMIN_PASSWORD_HASH does not start with '$argon2' — docker compose most likely "
                     "interpolated the '$' characters. Use ADMIN_PASSWORD_HASH_B64 instead.", service)
