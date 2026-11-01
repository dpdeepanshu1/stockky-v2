"""execution/order_rate.py - E282 round 77: a per-second budget for this service's Dhan order calls (plan E282 item 3).

Dhan's order API allows 10 requests a second for the whole account (secondary sources; verify on Dhan's page). Position-stocks and real-trade share one account. Nothing counted
calls per second before: a market-wide drop can make the stop / target modifies of every open position (trailing, breakeven, tape, momentum, event and reconcile paths, each with
its own per-position timer) land in the same second, next to a swing stop sync from real-trade-service.

How it works (one process-wide rolling one-second window, ORDER_RATE_PER_S calls, default 4, so this service leaves 6 a second for real-trade-service):
  * `acquire(kind, wait=True)`   a stop / target MODIFY waits until the last second holds fewer than the budget. A single caller never waits longer than one second (the oldest
                                 call leaves the window within that time); a hard cap of MAX_WAIT_S then lets the call through anyway, so a busy window can slow a modify, never starve it.
  * `acquire(kind)` (wait=False) every other order call (entry, cancel, exit, convert) is COUNTED and never delayed: an exit or a protective stop must not wait behind a trailing
                                 move. Counted calls use up the budget, so modifies yield to them.
Fail open: any problem returns 0.0 and the call goes ahead. ORDER_RATE_LIMIT_ENABLED = false removes the wait (calls are still counted). Real-trade-service never waits: its
broker-stop calls are all protective.

E285 round 87 (the cross-service budget): the window above is per PROCESS, so it cannot see real-trade-service's calls. `execution/shared_rate_window.py` keeps ONE account-wide
one-second window in a file on the shared-runtime volume; every call counted here is also appended to it (real-trade-service appends its own), and a MODIFY waits while that
window holds ORDER_RATE_ACCOUNT_PER_S calls (default 8 of Dhan's 10). ORDER_RATE_SHARED_ENABLED = false, or a file that cannot be used, = the per-process window only.

  acquire(kind, wait=False) -> seconds waited        call right before the broker call
  snapshot()                -> counters (calls, by kind, waits, longest wait, forced, peak calls in one second)
  reset()                   -> clear (tests)
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Dict

import config
from execution import shared_rate_window

logger = logging.getLogger("position-stocks-order-rate")

WINDOW_S = 1.0
MAX_WAIT_S = 2.0

_lock = threading.Lock()
_win: Deque[float] = deque()
_clock = time.monotonic
_sleep = time.sleep
_stats: Dict[str, Any] = {}


def _blank() -> Dict[str, Any]:
    return {"calls": 0, "by_kind": {}, "waits": 0, "waited_s": 0.0, "max_wait_s": 0.0, "forced": 0, "peak_in_window": 0,
            "account_waits": 0, "shared_recorded": 0, "shared_unusable": 0}


_stats.update(_blank())


def enabled() -> bool:
    return bool(getattr(config, "ORDER_RATE_LIMIT_ENABLED", True))


def per_second() -> int:
    try:
        return max(1, min(9, int(getattr(config, "ORDER_RATE_PER_S", 4))))
    except (TypeError, ValueError):
        return 4


def shared_enabled() -> bool:
    return bool(getattr(config, "ORDER_RATE_SHARED_ENABLED", True))


def account_per_s() -> int:
    try:
        return max(2, min(10, int(getattr(config, "ORDER_RATE_ACCOUNT_PER_S", 8))))
    except (TypeError, ValueError):
        return 8


def _admit(now: float, kind: str) -> None:
    _win.append(now)
    _stats["calls"] += 1
    _stats["by_kind"][kind] = _stats["by_kind"].get(kind, 0) + 1
    _stats["peak_in_window"] = max(_stats["peak_in_window"], len(_win))


def acquire(kind: str, wait: bool = False) -> float:
    """Count one order call and, for wait=True, hold it until the last second has room (this process's budget AND, when the shared window is on and usable, the account-wide
    budget). Returns the seconds waited. Never raises."""
    try:
        budget = per_second()
        waited = 0.0
        can_wait = bool(wait) and enabled()
        use_shared = shared_enabled()
        acct_limit = account_per_s()
        while True:
            acct_ok = True
            if can_wait and use_shared:
                got = shared_rate_window.total_last_s()                     # file read outside the process lock; None = unusable = this process's window only
                if got is None:
                    with _lock:
                        _stats["shared_unusable"] += 1
                else:
                    acct_ok = got[0] < acct_limit
            with _lock:
                now = _clock()
                while _win and now - _win[0] >= WINDOW_S:
                    _win.popleft()
                local_ok = len(_win) < budget
                if not can_wait or (local_ok and acct_ok):
                    _admit(now, kind)
                    break
                if waited >= MAX_WAIT_S:
                    _stats["forced"] += 1
                    _admit(now, kind)
                    logger.warning("order rate: %s let through after %.1fs wait (%d calls in this process's last second, budget %d; account-wide ok=%s)", kind, waited, len(_win), budget, acct_ok)
                    break
                if not acct_ok:
                    _stats["account_waits"] += 1
                need = (_win[0] + WINDOW_S - now) if (not local_ok and _win) else 0.05      # an account-wide wait polls the file every 50 ms (its oldest entry leaves within a second)
            step = min(max(need, 0.005), MAX_WAIT_S - waited)
            _sleep(step)
            waited += step
        if use_shared:
            ok = shared_rate_window.record("ps", kind)
            with _lock:
                _stats["shared_recorded" if ok else "shared_unusable"] += 1
        if waited > 0:
            with _lock:
                _stats["waits"] += 1
                _stats["waited_s"] = round(_stats["waited_s"] + waited, 4)
                _stats["max_wait_s"] = round(max(_stats["max_wait_s"], waited), 4)
        return waited
    except Exception as e:  # noqa: BLE001
        logger.debug("order rate acquire failed (call goes ahead): %s", e)
        return 0.0


def snapshot() -> Dict[str, Any]:
    with _lock:
        out = dict(_stats)
        out["by_kind"] = dict(_stats["by_kind"])
        out["budget_per_s"] = per_second()
        out["enabled"] = enabled()
        out["shared_enabled"] = shared_enabled()
        out["account_per_s"] = account_per_s()
        return out


def reset() -> None:
    with _lock:
        _win.clear()
        _stats.clear()
        _stats.update(_blank())
