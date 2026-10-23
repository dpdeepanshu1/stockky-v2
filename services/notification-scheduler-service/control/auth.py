"""control/auth.py (E51): who may call the scheduler's /control routes.

The scheduler has no login and is NOT published by nginx. The only caller is api-gateway (routes /scheduler-control/*), which first checks the admin session
and then forwards the request with the shared key SCHEDULER_CONTROL_KEY (same value in both containers) and the admin's name. No key set, or a key shorter
than 16 characters = every control route answers 503 (fails closed); a wrong key = 401."""
from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException

MIN_KEY_LEN = 16


def require_gateway(x_control_key: str = Header(default=""), x_admin_user: str = Header(default="")) -> str:
    key = (os.environ.get("SCHEDULER_CONTROL_KEY") or "").strip()
    if len(key) < MIN_KEY_LEN:
        raise HTTPException(status_code=503, detail="scheduler control is not configured (set SCHEDULER_CONTROL_KEY, 16+ characters, on the gateway and the scheduler)")
    if not hmac.compare_digest(x_control_key.encode(), key.encode()):
        raise HTTPException(status_code=401, detail="bad control key")
    return (x_admin_user or "admin").strip()[:64] or "admin"
