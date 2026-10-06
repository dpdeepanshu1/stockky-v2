"""Short in-memory entry pauses after a Dhan rejection (group 209, item 15).

Group 192 made a symbol whose entry was rejected AFTER acceptance (dead ENTRY_LEG found by reconcile)
wait 30 minutes and stop for the day after two such entries. Two gaps remained:

* A margin / insufficient-funds rejection is about the ACCOUNT, not the symbol: the next candidate is
  rejected for the same reason, so the cycle kept firing orders that could not fill. It now pauses ALL new
  entries for ENTRY_MARGIN_PAUSE_MINUTES (default 5), and no longer counts against the symbol's own
  cooldown / day cap (the symbol did nothing wrong).
* A BUY that fails at placement for an unclassified reason (not restricted, cut-off, funds or circuit) was
  retried for the same symbol every cycle. It now rests that symbol for ENTRY_ORDER_FAILED_COOLDOWN_MINUTES
  (default 5; a transient blip costs a few minutes, a persistent failure stops hammering Dhan).

State is process-local and lost on restart; losing it only means one extra attempt, never a missed exit.
All functions are cheap, never raise, and a value of 0 minutes disables the respective pause.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

_lock = threading.Lock()
_all_until: float = 0.0
_all_reason: str = ""
_symbol_until: dict[str, tuple[float, str]] = {}


def _now() -> float:
    return time.time()


def pause_all(reason: str, minutes: float) -> None:
    """Pause every new entry for `minutes` (extends, never shortens, an active pause)."""
    global _all_until, _all_reason
    if not minutes or minutes <= 0:
        return
    until = _now() + minutes * 60.0
    with _lock:
        if until > _all_until:
            _all_until, _all_reason = until, (reason or "")[:160]


def all_paused() -> Optional[str]:
    """A human-readable skip reason while new entries are paused, else None."""
    with _lock:
        left = _all_until - _now()
        reason = _all_reason
    if left <= 0:
        return None
    return f"ENTRY_MARGIN_PAUSE:new entries paused {left / 60.0:.1f}m more after a Dhan rejection ({reason})"


def cooldown_symbol(symbol: str, minutes: float, reason: str = "") -> None:
    """Rest one symbol for `minutes` (extends, never shortens)."""
    if not minutes or minutes <= 0 or not symbol:
        return
    until = _now() + minutes * 60.0
    with _lock:
        cur = _symbol_until.get(symbol)
        if cur is None or until > cur[0]:
            _symbol_until[symbol] = (until, (reason or "")[:160])


def symbol_blocked(symbol: str) -> Optional[str]:
    """A skip reason while `symbol` is resting after a failed placement, else None."""
    with _lock:
        cur = _symbol_until.get(symbol)
        if cur is None:
            return None
        left = cur[0] - _now()
        if left <= 0:
            _symbol_until.pop(symbol, None)
            return None
        reason = cur[1]
    return f"ENTRY_ORDER_FAILED_COOLDOWN:{symbol} order placement failed, resting {left / 60.0:.1f}m more ({reason})"


def reset() -> None:
    """Clear all pauses (tests, manual override)."""
    global _all_until, _all_reason
    with _lock:
        _all_until, _all_reason = 0.0, ""
        _symbol_until.clear()
