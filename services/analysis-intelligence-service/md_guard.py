"""md_guard.py (group170) - one guarded door for analysis-intelligence's calls to market-data-service.

Why: the quality gate (position-stocks-service) asks /analyze for several symbols at once every cycle, and
each /analyze fans out to market-data (/fundamentals, /history, /quote) with a 35-60 s timeout and no limit.
When market-data is slow, every extra call just waits its full timeout, piles on, and ends in a ReadTimeout
that used to be logged with an empty message. This module adds, around the same httpx.get call:

  * a cap on concurrent calls (MD_MAX_CONCURRENT, default 12, 0 = unlimited); a call that cannot get a slot
    within MD_SLOT_WAIT_S (default 30) fails fast;
  * single-flight: identical requests in flight at the same moment (same URL + params) share ONE upstream call;
  * a short cool-down: after MD_BREAKER_THRESHOLD (default 8) timeouts in a row, calls fail immediately for
    MD_BREAKER_COOLDOWN_S (default 15) instead of each waiting out its own timeout. MD_BREAKER=0 turns it off.

Failures raise MarketDataUnavailable (an httpx.TransportError), so existing `except httpx.HTTPError` /
`except Exception` handlers treat it like any other transient market-data failure. No result is cached, so no
data can go stale. MD_GUARD=0 turns the whole module into a plain httpx.get.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import httpx

logger = logging.getLogger("md-guard")


def exc_detail(e) -> str:
    """Name the exception type and add its message when it has one. httpx timeouts stringify to ''."""
    try:
        msg = str(e).strip()
    except Exception:
        msg = ""
    name = type(e).__name__
    return f"{name}: {msg}" if msg else name


class MarketDataUnavailable(httpx.TransportError):
    """Raised without calling market-data (cool-down open, or no free call slot in time)."""


def _env_float(name: str, default: float, lo: float = 0.0) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return v if v == v and v >= lo else default


def _on(name: str) -> bool:
    return ((os.getenv(name) or "").strip() or "1") not in ("0", "false", "False")


# ── concurrency cap ──────────────────────────────────────────────────────────
_slot_lock = threading.Lock()
_slots: threading.BoundedSemaphore | None = None
_slots_n = -1


def _semaphore():
    """Semaphore sized from MD_MAX_CONCURRENT (re-built when the env value changes). None = unlimited."""
    global _slots, _slots_n
    n = int(_env_float("MD_MAX_CONCURRENT", 12, 0))
    with _slot_lock:
        if n != _slots_n:
            _slots_n = n
            _slots = threading.BoundedSemaphore(n) if n > 0 else None
        return _slots


# ── cool-down after repeated timeouts ────────────────────────────────────────
_br_lock = threading.Lock()
_timeout_streak = 0
_open_until = 0.0


def _breaker_remaining() -> float:
    if not _on("MD_BREAKER"):
        return 0.0
    with _br_lock:
        return max(0.0, _open_until - time.monotonic())


def _record_timeout() -> None:
    global _timeout_streak, _open_until
    if not _on("MD_BREAKER"):
        return
    threshold = int(_env_float("MD_BREAKER_THRESHOLD", 8, 1))
    cooldown = _env_float("MD_BREAKER_COOLDOWN_S", 15.0, 0)
    with _br_lock:
        _timeout_streak += 1
        if _timeout_streak >= threshold and cooldown > 0 and time.monotonic() >= _open_until:
            _open_until = time.monotonic() + cooldown
            _timeout_streak = 0
            logger.warning("md-guard: %d market-data timeouts in a row - failing fast for %.0f s "
                           "(MD_BREAKER=0 disables)", threshold, cooldown)


def _record_response() -> None:
    global _timeout_streak
    with _br_lock:
        _timeout_streak = 0


def reset_state() -> None:
    """Test helper: forget the streak, the cool-down and any in-flight bookkeeping."""
    global _timeout_streak, _open_until, _slots, _slots_n
    with _br_lock:
        _timeout_streak = 0
        _open_until = 0.0
    with _slot_lock:
        _slots, _slots_n = None, -1
    with _flights_lock:
        _flights.clear()


# ── single flight ────────────────────────────────────────────────────────────
class _Flight:
    __slots__ = ("event", "result", "error", "done")

    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.error = None
        self.done = False


_flights_lock = threading.Lock()
_flights: dict = {}


def _call(url: str, params, timeout):
    """One real upstream call, with the cool-down check and the concurrency cap."""
    left = _breaker_remaining()
    if left > 0:
        raise MarketDataUnavailable(f"market-data cooling down after repeated timeouts ({left:.0f}s left)")
    sem = _semaphore()
    got = True
    if sem is not None:
        got = sem.acquire(timeout=_env_float("MD_SLOT_WAIT_S", 30.0, 0))
        if not got:
            raise MarketDataUnavailable("market-data call queue full (no free slot in time)")
    try:
        try:
            resp = httpx.get(url, params=params, timeout=timeout) if params is not None \
                else httpx.get(url, timeout=timeout)
        except httpx.TimeoutException:
            _record_timeout()
            raise
        _record_response()
        return resp
    finally:
        if sem is not None and got:
            sem.release()


def md_get(url: str, *, params=None, timeout=35):
    """Drop-in for httpx.get(url, params=..., timeout=...) against market-data-service."""
    if not _on("MD_GUARD"):
        return httpx.get(url, params=params, timeout=timeout) if params is not None \
            else httpx.get(url, timeout=timeout)
    try:
        key = (url, tuple(sorted((params or {}).items())))
        hash(key)
    except Exception:
        return _call(url, params, timeout)
    with _flights_lock:
        fl = _flights.get(key)
        leader = fl is None
        if leader:
            fl = _flights[key] = _Flight()
    if not leader:
        try:
            wait = float(timeout) + 5.0
        except (TypeError, ValueError):
            wait = 40.0
        fl.event.wait(wait)
        if fl.done:
            if fl.error is not None:
                raise fl.error
            return fl.result
        return _call(url, params, timeout)       # leader never finished in time: make our own call
    try:
        resp = _call(url, params, timeout)
        fl.result = resp
        fl.done = True
        return resp
    except BaseException as e:
        fl.error = e
        fl.done = True
        raise
    finally:
        fl.event.set()
        with _flights_lock:
            if _flights.get(key) is fl:
                del _flights[key]
