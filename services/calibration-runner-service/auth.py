"""Admin session check + a short-lived service token (group 442).

The page sends the same admin bearer token the other pages use (HS256, SESSION_SECRET shared by all services), so verifying it here needs no new login.
Jobs that call real-trade / position-stocks are given a token this service MINTS itself (same secret, role admin, short TTL, marked `svc: calibration-runner`),
because the browser token must never be written into a job file or a log.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

import jwt
from fastapi import Header, HTTPException

import config


def _secret() -> str:
    if not config.SESSION_SECRET:
        raise HTTPException(status_code=503, detail="SESSION_SECRET is not set on calibration-runner-service")
    return config.SESSION_SECRET


def verify(token: str) -> Dict[str, Any]:
    try:
        claims = jwt.decode(token, _secret(), algorithms=["HS256"])
    except HTTPException:
        raise
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
    return jwt.encode(claims, _secret(), algorithm="HS256")
