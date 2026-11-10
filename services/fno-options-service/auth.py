"""auth.py (fno-options-service) - admin session check. The Calibration / F&O pages send the same admin bearer token the other pages use (issued by real-trade-service or
position-stocks-service, HS256, per-service secrets via session_keys); this service only VERIFIES it and never issues one, so there is no new login."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import Header, HTTPException

import config
import session_keys

_ACCEPT = ("real-trade", "position-stocks")
_ACCEPT_RUNNER = _ACCEPT + ("calibration-runner",)     # the runner may only start / read backtests (require_admin_or_runner); it can NOT arm, disarm or square off


def _any_key_configured() -> bool:
    return bool(config.SESSION_SECRET) or any(session_keys.own_secret(k) for k in _ACCEPT_RUNNER)


def verify(token: str, accept: tuple = _ACCEPT) -> Dict[str, Any]:
    if not _any_key_configured():
        raise HTTPException(status_code=503, detail="no session secret is set on fno-options-service (SESSION_SECRET_REAL_TRADE / SESSION_SECRET_POSITION_STOCKS)")
    try:
        return session_keys.decode(token, accept, legacy=config.SESSION_SECRET)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=401, detail=f"invalid or expired session ({type(e).__name__})")


def is_admin(claims: Dict[str, Any]) -> bool:
    """Round 298: a token that SAYS it is not an admin is refused (`admin` present and not true, or a `role` other than admin). The login tokens real-trade-service and position-stocks-service issue carry only
    `sub` (the admin username, signed after the password check), so a bare non-empty `sub` is still accepted: requiring `admin: true` would lock the operator out of ARM and square-off."""
    if "admin" in claims and claims["admin"] is not True:
        return False
    role = claims.get("role")
    if role not in (None, "") and role != "admin":
        return False
    sub = claims.get("sub")
    return claims.get("admin") is True or role == "admin" or (isinstance(sub, str) and sub.strip() != "")


def require_admin(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="admin login required")
    claims = verify(authorization[7:].strip())
    if not is_admin(claims):
        raise HTTPException(status_code=403, detail="admin only")
    return claims


def require_admin_or_runner(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    """Backtest routes only: also accepts the calibration-runner's service token, so a scheduled pack can start a backtest."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="admin login required")
    claims = verify(authorization[7:].strip(), _ACCEPT_RUNNER)
    if not is_admin(claims):
        raise HTTPException(status_code=403, detail="admin only")
    return claims
