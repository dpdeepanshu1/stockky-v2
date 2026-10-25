"""admin_guard.py - group 453: admin-session login for the api-gateway's dangerous routes.

Before: the gateway's POST/PUT/DELETE routes had no login, and nginx publishes the gateway at /api/. Anyone who could reach the domain could
redirect the alert channels (/notifications/config), switch the app off (/ops/power-off), start a training run or wipe the data feed.

Now the routes in PROTECTED (wired in main.py with `dependencies=[Depends(_require_admin)]`) need the SAME admin session token that
real-trade-service / position-stocks-service issue at login (JWT HS256, signed with that service's own secret - see session_keys.py): `Authorization: Bearer <token>`.
Nothing new to log in to. The frontend attaches the stored token automatically (frontend/src/gatewayAuth.ts).

  * no session secret set on the gateway -> 503 "admin guard is not configured" (fails CLOSED: a missing secret must not mean an open door).
  * GATEWAY_ADMIN_GUARD=false             -> guard off (emergency / rollout switch only; the default is ON).
Read-only routes and the routine data-feed / scan / watchlist routes the UI uses without a login are NOT touched.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

import jwt
from fastapi import Header, HTTPException

import session_keys

logger = logging.getLogger("gateway-admin-guard")

# What main.py protects. A test checks main.py against this list, so the two cannot drift apart.
PROTECTED = (
    "/notifications/config",
    "/notifications/config/{channel}",
    "/notifications/call/me",
    "/notifications/test",
    "/notifications/send-picks",
    "/training/train",
    "/ops/power-off",
    "/ops/circuit-reset",
    "/ops/overnight/config",
    "/ops/overnight/run",
    "/ops/resume-activity",
    "/data-feed/hard-reset",
    "/api/data-feed/hard-reset",
    "/api/feed/hard-reset",
    # E149 (route-auth matrix): open before, no caller in the repo (UI, scripts, workflows, other services)
    "/ops/qstash/publish",
    "/api/feed/purge-over-cap",
    "/data-feed/purge-over-cap",
    # E150: Telegram senders, the IPO row delete and the universe-cache clear (the buttons show the login hint on a 401)
    "/ipo/notify-top-picks",
    "/surprise/ipo/notify-top-picks",
    "/surprise/notify-top-picks",
    "/stockky-hot/notify-top-picks",
    "/ipo/purge-non-equity",
    "/surprise/ipo/purge-non-equity",
    "/scan/universe/cache",
)

# E148 (F-12): GET/POST /training/{path} is a generic proxy to the training service, so POST /training/api/train, /training/train/run,
# /training/api/models/promote/<v>, the lock clears, /training/api/admin/* and /training/debug/abort reached the same actions as the guarded
# POST /training/train with no login. Those system-level actions now need the same admin token (the training proxy in
# gateway_support/bound_routes_h.py calls training_path_needs_admin() before it forwards). Reads and the paper-trade / scan-ingest routes
# the UI uses without a login stay open.
_TRAINING_ADMIN = re.compile(r"^(?:api/(?:train|lock/clear|models/promote/[^/]+|admin(?:/.*)?)|train(?:/(?:run|clear-lock))?|lock|debug/abort)$")
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def training_path_needs_admin(method: str, path: str) -> bool:
    """True when this method + sub-path of /training/ is a system-level training action. Case, `//`, `/./` and a trailing `/` do not hide it;
    a `..` segment is always treated as protected."""
    if (method or "").upper() in _SAFE_METHODS:
        return False
    segs = [s for s in str(path or "").lower().split("/") if s and s != "."]
    if ".." in segs:
        return True
    return bool(_TRAINING_ADMIN.match("/".join(segs)))


_OFF_VALUES = {"0", "false", "no", "off"}


def guard_enabled() -> bool:
    return (os.getenv("GATEWAY_ADMIN_GUARD") or "").strip().lower() not in _OFF_VALUES


# A4b: the gateway only VERIFIES. It accepts the two login services' tokens; it holds no key for the calibration runner's service token.
_ACCEPT = ("real-trade", "position-stocks")


def _decode(token: str, legacy: str) -> Optional[str]:
    try:
        payload = session_keys.decode(token, _ACCEPT, legacy=legacy, options={"require": ["exp"]})
    except jwt.InvalidTokenError:  # expired, bad signature, malformed, missing exp, unknown kid / no key for it
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) and sub else None


def _any_key_configured(legacy: str) -> bool:
    return bool(legacy) or any(session_keys.own_secret(k) for k in _ACCEPT)


def require_admin(authorization: str = Header(default="")) -> str:
    """FastAPI dependency. Returns the admin username; raises 401 (bad / missing / expired token) or 503 (no SESSION_SECRET)."""
    if not guard_enabled():
        return "guard-disabled"
    secret = (os.getenv("SESSION_SECRET") or "").strip()  # legacy shared secret (tokens without a kid)
    if not _any_key_configured(secret):
        logger.error("admin guard: no session secret is set on the gateway (SESSION_SECRET_REAL_TRADE / SESSION_SECRET_POSITION_STOCKS) - refusing protected routes")
        raise HTTPException(status_code=503, detail="admin guard is not configured")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing admin session token")
    username = _decode(authorization[len("Bearer "):].strip(), secret)
    if not username:
        raise HTTPException(status_code=401, detail="Invalid or expired admin session")
    return username
