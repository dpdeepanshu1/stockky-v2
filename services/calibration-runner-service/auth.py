"""Admin session check + a short-lived service token (group 442).

The page sends the same admin bearer token the other pages use (HS256; per-service secrets, A4b: this service verifies the real-trade / position-stocks kids and signs its own), so verifying it here needs no new login.
Jobs that call real-trade / position-stocks are given a token this service MINTS itself (same secret, role admin, short TTL, marked `svc: calibration-runner`),
because the browser token must never be written into a job file or a log.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

import jwt
from fastapi import Header, HTTPException

import config
import session_keys


_ME = "calibration-runner"
# the page sends the token it got from real-trade or position-stocks; this service never needs to accept its own token
_ACCEPT = ("real-trade", "position-stocks")


def _any_key_configured() -> bool:
    return bool(config.SESSION_SECRET) or any(session_keys.own_secret(k) for k in _ACCEPT)


def verify(token: str) -> Dict[str, Any]:
    if not _any_key_configured():
        raise HTTPException(status_code=503, detail="no session secret is set on calibration-runner-service (SESSION_SECRET_REAL_TRADE / SESSION_SECRET_POSITION_STOCKS)")
    try:
        claims = session_keys.decode(token, _ACCEPT, legacy=config.SESSION_SECRET)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=401, detail=f"invalid or expired session ({type(e).__name__})")
    return claims


def is_admin(claims: Dict[str, Any]) -> bool:
    return bool(claims.get("admin")) or claims.get("role") == "admin" or claims.get("sub") not in (None, "")


def require_admin(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="admin login required")
    claims = verify(authorization[7:].strip())
    if not is_admin(claims):
        raise HTTPException(status_code=403, detail="admin only")
    return claims


def mint_service_token(sub: str = "calibration-runner", ttl_s: Optional[int] = None) -> str:
    """Token for the jobs. Copies the claim names of the page's admin token so the other services accept it."""
    now = int(time.time())
    claims = {"sub": sub, "role": "admin", "admin": True, "svc": "calibration-runner", "iat": now, "exp": now + int(ttl_s or config.TOKEN_TTL_S)}
    try:
        return session_keys.sign(_ME, claims, legacy=config.SESSION_SECRET)  # own SESSION_SECRET_CALIBRATION_RUNNER (kid), else legacy
    except session_keys.MissingSecret as e:
        raise HTTPException(status_code=503, detail=str(e))
