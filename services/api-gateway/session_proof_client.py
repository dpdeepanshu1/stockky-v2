"""session_proof_client.py - ask market-data-service whether the exchange really traded today (group 319).

market-data-service decides from PRICES (no stock/Nifty move 09:15-10:00 IST = closed, see its session_proof.py) and
serves it at GET /market/session. This module lets tz_utils.is_market_open_ist() honour that verdict without ever
blocking: the answer is cached, a stale cache is refreshed by a daemon thread, and every failure means "not closed"
(fail-open: a flaky network can never stop trading, it can only fail to stop it on an unlisted holiday).

Identical copy in real-trade-service and position-stocks-service (no shared import path between services).
Off with SESSION_PROOF_CLIENT_ENABLED=false. Never refreshes under pytest or when the caller passes its own `now`.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from typing import Optional

_TTL_S = float(os.getenv("SESSION_PROOF_CLIENT_TTL_S", "30") or 30)
_TIMEOUT_S = float(os.getenv("SESSION_PROOF_CLIENT_TIMEOUT_S", "2") or 2)

_lock = threading.Lock()
_cache: dict = {"day": None, "closed": False, "at": 0.0, "inflight": False}


def _enabled() -> bool:
    raw = (os.getenv("SESSION_PROOF_CLIENT_ENABLED") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _url() -> str:
    base = (os.getenv("MARKET_DATA_URL") or "").strip().rstrip("/")
    return f"{base}/market/session" if base else ""


def reset_state() -> None:
    """Test helper."""
    with _lock:
        _cache.update(day=None, closed=False, at=0.0, inflight=False)


def set_verdict(day: Optional[str], closed: bool) -> None:
    """Record a verdict directly (used by the refresher and by tests)."""
    with _lock:
        _cache.update(day=day, closed=bool(closed), at=time.time(), inflight=False)


def _fetch() -> None:
    try:
        url = _url()
        if not url:
            return
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        if isinstance(body, dict):
            set_verdict(str(body.get("day") or "") or None, bool(body.get("closed")))
            return
    except Exception:
        pass
    finally:
        with _lock:
            _cache["inflight"] = False
            _cache["at"] = time.time()   # back off one TTL after a failure too


def closed_for_day(day_ist: str, refresh: bool = True) -> bool:
    """True only when market-data said today (day_ist, 'YYYY-MM-DD') is a non-trading day. Never blocks, never raises."""
    try:
        if not _enabled():
            return False
        start = False
        with _lock:
            fresh = (time.time() - float(_cache["at"])) < _TTL_S
            if refresh and not fresh and not _cache["inflight"] and "PYTEST_CURRENT_TEST" not in os.environ:
                _cache["inflight"] = True
                start = True
            hit = bool(_cache["closed"]) and _cache["day"] == day_ist
        if start:
            threading.Thread(target=_fetch, daemon=True, name="session-proof-refresh").start()
        return hit
    except Exception:
        return False
