"""Round 607: a per-symbol gate in front of the /quotes/bulk yfinance download (Round C, step 8).

Why: Dhan / AngelOne / the live feed already price 95-100% of a bulk request. The few names left over (not in Dhan's scrip map, held
positions, indices) still went to the yfinance download on EVERY bulk call, and several bulk calls run at once. Each call paid one token of the shared
yfinance bucket, the bucket stayed empty, and the log filled with "yfinance rate-limit bucket saturated" while the same 3-7 names were asked again
and again (and the ones Yahoo cannot price were asked again after every failure).

What: every symbol sent to Yahoo from /quotes/bulk is stamped. Within QUOTE_BULK_YF_MIN_INTERVAL_S (default 30 s) the same symbol is not sent
again; the caller gets a recent cached price if there is one, otherwise the symbol is left out of that answer (the wrapper in main.py still appends
an older cached row, with its own fetched_at, when it has one). The stamp is taken BEFORE the download, so two bulk calls running at the same
moment do not both ask Yahoo for the same names. Held positions and indices bypass the gate (the same fail-safe the other leftover gates use).
QUOTE_BULK_YF_MIN_INTERVAL_S=0 restores the old behaviour (every call asks Yahoo).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Iterable, List, Tuple

_LOCK = threading.Lock()
_LAST: dict = {}               # symbol key -> monotonic time it was last sent to Yahoo
_MAX_KEYS = 5000
STATS = {"sent": 0, "throttled": 0, "served": 0}


def min_interval_s() -> float:
    """QUOTE_BULK_YF_MIN_INTERVAL_S: seconds before the same symbol may go to Yahoo again from /quotes/bulk. Default 30; 0 = gate off;
    blank / bad / negative / above 3600 = default. Never raises."""
    raw = (os.getenv("QUOTE_BULK_YF_MIN_INTERVAL_S") or "").strip()
    try:
        v = float(raw) if raw else 30.0
    except ValueError:
        return 30.0
    if not (v == v and 0.0 <= v <= 3600.0):
        return 30.0
    return v


def _key(sym: str) -> str:
    return (sym or "").upper().replace(".NS", "").replace(".BO", "").strip()


def split(symbols: Iterable[str], bypass: Callable[[str], bool], now: float | None = None) -> Tuple[List[str], List[str]]:
    """(send, throttled). `send` symbols are stamped now, so a concurrent call sees them as throttled. A symbol for which bypass(sym) is True
    (held position, index; an error inside bypass counts as True) is always sent and stamped. Gate off (interval 0) = everything is sent
    and nothing is stamped. Order is kept. Never raises."""
    syms = list(symbols or [])
    every = min_interval_s()
    if every <= 0:
        return syms, []
    t = time.monotonic() if now is None else now
    send: List[str] = []
    held_back: List[str] = []
    with _LOCK:
        if len(_LAST) > _MAX_KEYS:
            for k in [k for k, v in _LAST.items() if t - v >= every]:
                _LAST.pop(k, None)
        for s in syms:
            k = _key(s)
            try:
                byp = bool(bypass(s))
            except Exception:  # noqa: BLE001 - unknown -> treat as held (fail safe)
                byp = True
            last = _LAST.get(k)
            if byp or last is None or t - last >= every:
                _LAST[k] = t
                send.append(s)
            else:
                held_back.append(s)
        STATS["sent"] += len(send)
        STATS["throttled"] += len(held_back)
    return send, held_back


def reset() -> None:
    """Forget every stamp and zero the counters (tests)."""
    with _LOCK:
        _LAST.clear()
        for k in STATS:
            STATS[k] = 0
