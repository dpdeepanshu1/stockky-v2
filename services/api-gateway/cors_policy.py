"""cors_policy.py - group 454: which browser origins may call the api-gateway.

Before: allow_origins=["*"] with allow_credentials=True, and a second middleware that stamped `Access-Control-Allow-Origin: *` on every response, so no
setting could ever narrow it. (Credentials with "*" is also invalid per the CORS spec; the app sends a Bearer header, never cookies.)

Now GATEWAY_CORS_ORIGINS (comma separated, e.g. "https://stockky.example.com,http://localhost:5173") decides:
  * unset / empty / contains "*"  -> wildcard, exactly as before (kept as the default so an existing deploy does not break the first time it is
                                     rebuilt). Credentials are no longer advertised. The dangerous routes are behind the admin login (group 453).
  * a list of origins             -> ONLY those origins get CORS headers (exact match on scheme + host + port); the /ws websocket refuses other origins.
Requests without an Origin header (curl, server-to-server, same-origin through nginx) are never affected by CORS.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

WILDCARD_HEADERS: Dict[str, str] = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "*",
    "Access-Control-Allow-Headers": "*",
}


def normalize_origin(origin: str) -> str:
    return (origin or "").strip().rstrip("/").lower()


def parse_origins(raw: Optional[str]) -> List[str]:
    return [o for o in (normalize_origin(p) for p in (raw or "").split(",")) if o]


def configured_origins() -> List[str]:
    """Read live so tests (and a restart with a new .env) see the current value."""
    return parse_origins(os.getenv("GATEWAY_CORS_ORIGINS"))


def is_wildcard(origins: Optional[List[str]] = None) -> bool:
    o = configured_origins() if origins is None else origins
    return (not o) or ("*" in o)


def origin_allowed(origin: Optional[str], origins: Optional[List[str]] = None) -> bool:
    o = configured_origins() if origins is None else origins
    if is_wildcard(o):
        return True
    return normalize_origin(origin or "") in o


def _request_origin(request) -> str:
    headers = getattr(request, "headers", None)
    try:
        return (headers.get("origin") or "") if headers is not None else ""
    except Exception:
        return ""


def response_headers(request) -> Dict[str, str]:
    """CORS headers to stamp on a response the CORS middleware cannot reach (the 500 handler). Wildcard mode = the old three headers."""
    origins = configured_origins()
    if is_wildcard(origins):
        return dict(WILDCARD_HEADERS)
    origin = _request_origin(request)
    if origin and origin_allowed(origin, origins):
        return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
    return {}
