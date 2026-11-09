"""fno/creds.py - borrow the Dhan token from real-trade-service when the operator ARMS F&O REAL for the day.

The token lives encrypted in real-trade-service. GET /dhan/fno-credentials there (admin session, audited, no-store) returns it; this service keeps it IN MEMORY ONLY,
sets it on `config` (so execution.real_allowed sees credentials) and builds the broker. Disarm / restart drops it. Env FNO_DHAN_* (if set) always wins: nothing is fetched."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple


class CredsError(Exception):
    pass


def fetch(base_url: str, bearer: str, timeout_s: float = 8.0, opener: Optional[Callable[..., Any]] = None) -> Dict[str, str]:
    if not bearer:
        raise CredsError("no admin session token to present to real-trade-service")
    req = urllib.request.Request(base_url.rstrip("/") + "/dhan/fno-credentials", headers={"Authorization": bearer if bearer.startswith("Bearer ") else f"Bearer {bearer}", "Accept": "application/json"})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout_s) as r:
            body = json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8") or "{}").get("detail")
        except ValueError:
            detail = None
        if e.code == 401:
            raise CredsError("real-trade-service did not accept this login: log in on the Auto Trade page first (its own session is needed)") from None
        raise CredsError(f"real-trade-service said {e.code}: {str(detail or 'error')[:200]}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise CredsError(f"cannot reach real-trade-service ({type(e).__name__})") from None
    cid, tok = str(body.get("client_id") or ""), str(body.get("access_token") or "")
    if not cid or not tok:
        raise CredsError("real-trade-service returned no credentials")
    return {"client_id": cid, "access_token": tok, "base_url": str(body.get("base_url") or ""), "dhan_env": str(body.get("dhan_env") or "")}


def apply(cfg: Any, creds: Dict[str, str]) -> Tuple[str, str]:
    """Put the borrowed values on the config module (memory only). Returns what to restore on drop."""
    prev = (str(getattr(cfg, "DHAN_CLIENT_ID", "")), str(getattr(cfg, "DHAN_ACCESS_TOKEN", "")))
    cfg.DHAN_CLIENT_ID, cfg.DHAN_ACCESS_TOKEN = creds["client_id"], creds["access_token"]
    if creds.get("base_url"):
        cfg.DHAN_BASE_URL = creds["base_url"].rstrip("/")
    return prev


def drop(cfg: Any, prev: Tuple[str, str] = ("", "")) -> None:
    cfg.DHAN_CLIENT_ID, cfg.DHAN_ACCESS_TOKEN = prev
