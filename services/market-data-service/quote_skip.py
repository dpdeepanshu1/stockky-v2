"""Round 606: two small helpers for market-data-service/main.py.

1. skip_symbols(): QUOTE_SKIP_SYMBOLS, a comma list of NSE symbols that no provider can price (AngelOne has no token, Dhan has no security id,
   Yahoo answers nothing). main.is_known_delisted() also answers True for them, so /quote, /history, the fundamentals route and the live-feed
   universe skip them at once instead of sending each one to Yahoo again and again. Empty by default: the list is the operator's decision.
2. DailyNote: lets a log line through at most once per interval (the Dhan "not in the scrip map" names are logged once a day, not on every bulk call).
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Set

_SPLIT = re.compile(r"[,\s;]+")


def _base(sym: str) -> str:
    return (sym or "").upper().replace(".NS", "").replace(".BO", "").strip()


def skip_symbols() -> Set[str]:
    """Bare upper-case symbols from QUOTE_SKIP_SYMBOLS. Blank / unset / "none" = empty set. Never raises."""
    try:
        raw = (os.getenv("QUOTE_SKIP_SYMBOLS") or "").strip()
        if not raw or raw.lower() in ("none", "off", "0"):
            return set()
        return {b for b in (_base(p) for p in _SPLIT.split(raw)) if b}
    except Exception:  # noqa: BLE001 - a bad setting must not break quote routes
        return set()


class DailyNote:
    """allow() is True the first time and then at most once per `every_s()` seconds. every_s() <= 0 = always allow (the old per-call behaviour)."""

    def __init__(self, env_name: str, default_s: float = 86400.0):
        self._env = env_name
        self._default = default_s
        self._last = None
        self._lock = threading.Lock()

    def every_s(self) -> float:
        raw = (os.getenv(self._env) or "").strip()
        try:
            v = float(raw) if raw else self._default
        except ValueError:
            return self._default
        return v if v == v and v >= 0 else self._default

    def allow(self) -> bool:
        every = self.every_s()
        if every <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            if self._last is None or now - self._last >= every:
                self._last = now
                return True
        return False
