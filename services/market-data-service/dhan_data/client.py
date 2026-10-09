"""dhan_data/client.py - the ONE place that talks HTTP to Dhan's data endpoints (group 270).

Responsibilities: headers, global rate limiters (quote 1 call/s, history a few per second), error classification
into DhanError subclasses, an auth/subscription pause, a circuit breaker, and counters for /internal/dhan-status.
It never logs or raises a token, a client id, or a request header.

Plain httpx on purpose (not the dhanhq SDK): the SDK pulls in pandas and a ~100 MB scrip CSV download.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from typing import Optional

from . import config, creds
from .errors import (DhanApiError, DhanAuthError, DhanError, DhanNoDataError, DhanNotConfigured,
                     DhanRateLimitError, DhanSubscriptionError)

logger = logging.getLogger("dhan-data.client")


# ── rate limiter ───────────────────────────────────────────────────────────────────────────────────────────
class Limiter:
    """Spaces calls at least `min_interval` apart across all threads. slow_down() widens it temporarily."""

    def __init__(self, min_interval_fn):
        self._fn = min_interval_fn
        self._last = 0.0
        self._lock = threading.Lock()
        self._factor = 1.0
        self._factor_until = 0.0

    def slow_down(self, factor: float = 2.0, seconds: float = 30.0) -> None:
        with self._lock:
            self._factor = max(1.0, float(factor))
            self._factor_until = time.monotonic() + float(seconds)

    def interval(self) -> float:
        base = max(0.0, float(self._fn()))
        with self._lock:
            if self._factor > 1.0 and time.monotonic() < self._factor_until:
                return base * self._factor
        return base

    def wait(self) -> float:
        """Block until a call may start; returns the seconds slept."""
        slept = 0.0
        with self._lock:
            f = self._factor if (self._factor > 1.0 and time.monotonic() < self._factor_until) else 1.0
            gap = max(0.0, float(self._fn())) * f - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
                slept = gap
            self._last = time.monotonic()
        return slept


quote_limiter = Limiter(config.quote_min_interval_s)
hist_limiter = Limiter(lambda: 1.0 / max(0.2, config.hist_max_per_sec()))


# ── state / counters ───────────────────────────────────────────────────────────────────────────────────────
_state_lock = threading.Lock()
_pause_until = 0.0
_pause_reason = ""
_stats = {"calls": 0, "errors": 0, "last_error": None, "last_error_at": None, "last_ok_at": None,
          "last_latency_ms": None, "auth_errors": 0, "subscription_errors": 0, "rate_limit_errors": 0}
_recent: deque = deque(maxlen=2000)       # monotonic timestamps of calls, for calls_last_minute

_http = None
_http_lock = threading.Lock()


def _client():
    global _http
    with _http_lock:
        if _http is None:
            import httpx
            _http = httpx.Client(timeout=config.http_timeout_s(),
                                 limits=httpx.Limits(max_connections=8, max_keepalive_connections=4))
        return _http


def _breaker():
    try:
        from circuit_breaker import get_breaker
        return get_breaker("dhan_data", failure_threshold=config.breaker_fails(),
                           recovery_timeout=config.breaker_recovery_s())
    except Exception:  # noqa: BLE001
        return None


def paused() -> Optional[str]:
    """Reason string while the Dhan stage is paused (auth/subscription failure), else None."""
    with _state_lock:
        if time.monotonic() < _pause_until:
            return _pause_reason or "paused"
    return None


def available() -> bool:
    """Cheap pre-check used by every stage: enabled, not paused, breaker not open. Does not touch the network."""
    if not config.enabled():
        return False
    if paused():
        return False
    br = _breaker()
    if br is not None and not br.allow():
        return False
    return True


_last_logged_reason = ""
_last_logged_at = 0.0


def _pause(reason: str, seconds: Optional[float] = None) -> None:
    """Skip the Dhan stage for a while. The WARNING is logged once per distinct reason (and again after an hour),
    never once per symbol or once per retry."""
    global _pause_until, _pause_reason, _last_logged_reason, _last_logged_at
    secs = config.auth_pause_s() if seconds is None else seconds
    with _state_lock:
        _pause_until = time.monotonic() + secs
        _pause_reason = reason
        loud = reason != _last_logged_reason or (time.monotonic() - _last_logged_at) > 3600.0
        if loud:
            _last_logged_reason, _last_logged_at = reason, time.monotonic()
    if loud:
        logger.warning("dhan data paused for %.0fs: %s", secs, reason)
    else:
        logger.debug("dhan data paused for %.0fs: %s", secs, reason)


def note_failure(exc: Exception) -> None:
    """Record a failure from a stage and apply the right back-off. Safe to call with any exception."""
    if isinstance(exc, DhanNotConfigured):
        # no usable token yet / expired: not a Dhan fault, do not open the breaker, just stop asking for a while
        _pause(f"not configured ({exc})", seconds=max(config.auth_pause_s(), 300.0))
        return
    if isinstance(exc, DhanAuthError):
        creds.invalidate()
        _pause("auth error - token invalid or expired")
        return
    if isinstance(exc, DhanSubscriptionError):
        _pause("Data API subscription missing or inactive")
        return
    if isinstance(exc, DhanRateLimitError):
        quote_limiter.slow_down(2.0, 30.0)
        hist_limiter.slow_down(2.0, 30.0)
        return
    if isinstance(exc, DhanNoDataError):
        return                      # an answer, just an empty one: not a failure of the service
    br = _breaker()
    if br is not None:
        br.record_failure(str(exc)[:120])


def note_success() -> None:
    br = _breaker()
    if br is not None:
        br.record_success()


# ── error classification ───────────────────────────────────────────────────────────────────────────────────
_SUBSCRIPTION = {"806", "DH-902"}
_AUTH = {"807", "808", "809", "810", "DH-901", "DH-903"}
_RATE = {"805", "DH-904"}
_NODATA = {"811", "812", "813", "814", "DH-905", "DH-907"}
_CODE_RE = re.compile(r"\b(DH-9\d\d|8[01]\d)\b", re.I)


def classify(status: int, body, text: str = "") -> DhanError:
    """Map a failed response to a DhanError subclass. Codes follow Dhan's data-API annexure (VERIFY against current docs)."""
    codes: set = set()
    msg = ""
    if isinstance(body, dict):
        for k in ("errorCode", "error_code"):
            if body.get(k):
                codes.add(str(body[k]).upper())
        data = body.get("data")
        if isinstance(data, dict):
            codes |= {str(k).upper() for k in data.keys() if str(k).isdigit()}
        for k in ("errorMessage", "error_message", "message", "remarks"):
            v = body.get(k)
            if v:
                msg = str(v) if not isinstance(v, dict) else str(v.get("error_message") or v)
                break
    if not codes and text:
        codes |= {m.upper() for m in _CODE_RE.findall(text[:300])}
    msg = (msg or text or "")[:160]
    label = f"HTTP {status} {','.join(sorted(codes))} {msg}".strip()
    if codes & _SUBSCRIPTION:
        return DhanSubscriptionError(label)
    if codes & _AUTH or status in (401, 403):
        return DhanAuthError(label)
    if codes & _RATE or status == 429:
        return DhanRateLimitError(label)
    if codes & _NODATA:
        return DhanNoDataError(label)
    return DhanApiError(label)


# ── the call ────────────────────────────────────────────────────────────────────────────────────────────────
def post(path: str, payload: dict, *, limiter: Limiter) -> dict:
    """POST {base}/{path}. Returns the parsed JSON body of a successful call, raises a DhanError otherwise.
    Counts the call and records latency. Does not decide fall-through: callers call note_failure()/note_success()."""
    client_id, token = creds.get_credentials()      # may raise DhanNotConfigured
    headers = {"access-token": token, "client-id": client_id,
               "Content-Type": "application/json", "Accept": "application/json"}
    limiter.wait()
    t0 = time.monotonic()
    with _state_lock:
        _stats["calls"] += 1
        _recent.append(t0)
    try:
        r = _client().post(f"{config.base_url()}/{path.lstrip('/')}", json=payload, headers=headers)
    except Exception as e:  # noqa: BLE001 - network error text never contains headers
        _record_error(DhanApiError(f"network: {type(e).__name__}"))
        raise DhanApiError(f"network: {type(e).__name__}") from None
    latency = (time.monotonic() - t0) * 1000.0
    body = None
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = None
    failed = r.status_code != 200 or (isinstance(body, dict) and str(body.get("status", "")).lower() == "failure")
    if failed:
        err = classify(r.status_code, body, r.text if body is None else "")
        _record_error(err)
        raise err
    if not isinstance(body, (dict, list)):
        err = DhanApiError("empty or non-JSON body")
        _record_error(err)
        raise err
    with _state_lock:
        _stats["last_latency_ms"] = round(latency, 1)
        _stats["last_ok_at"] = time.time()
    return body


def _record_error(err: DhanError) -> None:
    with _state_lock:
        _stats["errors"] += 1
        _stats["last_error"] = f"{type(err).__name__}: {str(err)[:140]}"
        _stats["last_error_at"] = time.time()
        if isinstance(err, DhanAuthError):
            _stats["auth_errors"] += 1
        elif isinstance(err, DhanSubscriptionError):
            _stats["subscription_errors"] += 1
        elif isinstance(err, DhanRateLimitError):
            _stats["rate_limit_errors"] += 1


def stats() -> dict:
    now = time.monotonic()
    with _state_lock:
        out = dict(_stats)
        out["calls_last_minute"] = sum(1 for t in _recent if now - t <= 60.0)
    out["paused"] = paused()
    br = _breaker()
    out["breaker"] = br.state() if br is not None else None
    return out


def _reset_for_tests() -> None:
    global _pause_until, _pause_reason, _last_logged_reason, _last_logged_at
    with _state_lock:
        _pause_until, _pause_reason = 0.0, ""
        _last_logged_reason, _last_logged_at = "", 0.0
        for k in list(_stats):
            _stats[k] = 0 if isinstance(_stats[k], int) else None
        _recent.clear()
    quote_limiter._last = 0.0
    hist_limiter._last = 0.0
    quote_limiter._factor = hist_limiter._factor = 1.0
