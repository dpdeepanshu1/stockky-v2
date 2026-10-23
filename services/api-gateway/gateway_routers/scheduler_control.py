"""Scheduler alert switches for the Real Money Calibration page (E51). The scheduler is not published to the browser and has no login, so the page talks to the
gateway: the admin session is checked here (same guard as the other protected gateway routes) and the request is forwarded to the scheduler's /control routes with
the shared key SCHEDULER_CONTROL_KEY (same value on both containers) and the admin's name. Only the switch endpoints are forwarded."""
from __future__ import annotations

import os
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from admin_guard import require_admin

scheduler_control_router = APIRouter(dependencies=[Depends(require_admin)])

_ALLOWED = re.compile(r"^(flags|flags/[A-Z0-9_]{1,80}|preset/recommended|audit)$")
_METHODS = {"flags": {"GET"}, "audit": {"GET"}, "preset/recommended": {"POST"}}


def _base() -> str:
    return ((os.getenv("SCHEDULER_CONTROL_URL") or "").strip() or "http://notification-scheduler-service:8000/control").rstrip("/")


@scheduler_control_router.api_route("/scheduler-control/{path:path}", methods=["GET", "POST", "DELETE"])
@scheduler_control_router.api_route("/api/scheduler-control/{path:path}", methods=["GET", "POST", "DELETE"])
async def scheduler_control_proxy(path: str, request: Request):
    import httpx
    path = path.strip("/")
    if not _ALLOWED.match(path):
        raise HTTPException(status_code=404, detail="unknown scheduler control path")
    allowed = {"POST", "DELETE"} if path.startswith("flags/") else _METHODS[path]
    if request.method not in allowed:
        raise HTTPException(status_code=405, detail="method not allowed")
    key = (os.getenv("SCHEDULER_CONTROL_KEY") or "").strip()
    if len(key) < 16:
        raise HTTPException(status_code=503, detail="scheduler control is not configured (set SCHEDULER_CONTROL_KEY, 16+ characters, on the gateway and the scheduler)")
    user = "admin"
    try:
        auth = request.headers.get("authorization", "")
        import jwt as _jwt
        user = str((_jwt.decode(auth[7:], options={"verify_signature": False}) or {}).get("sub") or "admin")[:64]  # already verified by require_admin
    except Exception:  # noqa: BLE001
        pass
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.request(request.method, f"{_base()}/{path}", params=dict(request.query_params), content=body or None,
                                     headers={"X-Control-Key": key, "X-Admin-User": user, "Content-Type": "application/json"})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"scheduler unreachable: {type(e).__name__}") from e
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        data = {"detail": r.text[:200]}
    return JSONResponse(data, status_code=r.status_code)
