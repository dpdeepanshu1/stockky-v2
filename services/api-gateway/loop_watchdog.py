"""group302: event-loop stall watchdog for the api-gateway.

Why this exists: after a full ``docker compose down`` / ``up`` the gateway sometimes stopped answering (/health timed
out, tabs stuck on "Auditing...") for a while, then recovered by itself. Nothing in the log said what held the loop.

How it works: a small coroutine stamps a heartbeat on the event loop every ``HEARTBEAT_S``. A daemon *thread* (it keeps
running while the loop is blocked) checks the stamp; when it is older than ``GATEWAY_LOOP_STALL_WARN_S`` (default 3 s)
it logs ONE warning with the stack of the thread that runs the loop - i.e. the exact line that is blocking it - then
stays quiet for ``GATEWAY_LOOP_STALL_LOG_EVERY_S`` (default 30 s). It also remembers the worst stall seen, exposed
through ``snapshot()`` (GET /ops/loop-lag).

Cost: one tiny task per 0.5 s and one thread that sleeps. ``GATEWAY_LOOP_WATCHDOG=0`` turns it off.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import traceback
from typing import Optional

logger = logging.getLogger("api-gateway")

HEARTBEAT_S = 0.5

_state = {
    "hb": 0.0,
    "loop_thread": None,
    "started": False,
    "worst_stall_s": 0.0,
    "worst_stall_at": None,
    "stall_count": 0,
    "last_stack": None,
}
_lock = threading.Lock()


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.1, float(((os.getenv(name) or "").strip() or default)))
    except ValueError:
        return float(default)


def enabled() -> bool:
    return (os.getenv("GATEWAY_LOOP_WATCHDOG") or "").strip().lower() not in ("0", "false", "no", "off")


async def _heartbeat() -> None:
    _state["loop_thread"] = threading.get_ident()
    while True:
        _state["hb"] = time.monotonic()
        await asyncio.sleep(HEARTBEAT_S)


def _loop_thread_stack() -> Optional[str]:
    try:
        tid = _state["loop_thread"]
        frame = sys._current_frames().get(tid) if tid is not None else None
        if frame is None:
            return None
        return "".join(traceback.format_stack(frame)[-8:]).rstrip()
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return None


def check_once(now: float, warn_after: float, log_every: float, last_logged: float) -> float:
    """One watchdog pass. Returns the (possibly updated) time of the last logged warning."""
    hb = _state["hb"]
    if not hb:
        return last_logged
    stall = now - hb
    if stall < warn_after:
        return last_logged
    with _lock:
        _state["stall_count"] += 1
        if stall > _state["worst_stall_s"]:
            _state["worst_stall_s"] = round(stall, 1)
            _state["worst_stall_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if now - last_logged < log_every:
        return last_logged
    stack = _loop_thread_stack()
    _state["last_stack"] = stack
    logger.warning(
        "event loop blocked for %.1fs - the loop thread is currently at:\n%s", stall, stack or "(stack unavailable)"
    )
    return now


def _watch() -> None:
    warn_after = _env_float("GATEWAY_LOOP_STALL_WARN_S", 3.0)
    log_every = _env_float("GATEWAY_LOOP_STALL_LOG_EVERY_S", 30.0)
    last_logged = 0.0
    while True:
        time.sleep(1.0)
        last_logged = check_once(time.monotonic(), warn_after, log_every, last_logged)


def start(loop: Optional[asyncio.AbstractEventLoop] = None) -> bool:
    """Start the heartbeat task and the watchdog thread (idempotent). Returns True when running."""
    if not enabled():
        return False
    with _lock:
        if _state["started"]:
            return True
        _state["started"] = True
    lp = loop or asyncio.get_running_loop()
    lp.create_task(_heartbeat())
    t = threading.Thread(target=_watch, name="loop-watchdog", daemon=True)
    t.start()
    return True


def snapshot() -> dict:
    hb = _state["hb"]
    return {
        "enabled": enabled(),
        "running": bool(_state["started"]),
        "current_lag_s": round(max(0.0, time.monotonic() - hb - HEARTBEAT_S), 2) if hb else None,
        "worst_stall_s": _state["worst_stall_s"],
        "worst_stall_at": _state["worst_stall_at"],
        "stall_count": _state["stall_count"],
        "last_stack": _state["last_stack"],
    }
