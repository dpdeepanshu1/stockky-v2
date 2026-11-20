"""Round 659: one WAIT decision row per symbol + reason per window (D3 of the 20 Nov plan).

Why: real-trade wrote 6,417 WAIT rows in 3 days (about 2,100 a day), nearly all the same symbol and the same reason every 180 s cycle.
Rows that say nothing new cost DB writes, 176 MB + 80 MB of table, and slow every decisions query.

Rule (applied to pending ENTRY / WAIT rows just before the entry cycle flushes or commits, from write_behind.divert_pending_waits):
  - a WAIT row is written when it is the first for its (mode, symbol), when its signature differs from the last row written for that symbol
    (a change of reason, gate tag or verdict reason - numbers in the text are ignored so "0.83%" -> "0.85%" is not a change), or when
    WAIT_DEDUPE_MINUTES (default 15) have passed since the last row written;
  - otherwise the row is dropped from the session (the cycle's in-memory entry_details, counters and the shortlist are unaffected);
  - an ENTER row (or any non-WAIT ENTRY row) for the symbol resets its state, so the next WAIT after it is always written.
Exit rows, orders, fills and every non-WAIT row are never touched. WAIT_DEDUPE_MINUTES=0 turns it off. Nothing here raises.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Dict, Optional, Tuple

_NUM = re.compile(r"-?\d+(?:[.,]\d+)*")
_SPACE = re.compile(r"\s+")
_MAX_KEYS = 20000

_lock = threading.Lock()
_last: Dict[Tuple[str, str], Tuple[str, float]] = {}
_stats = {"kept": 0, "dropped": 0}


def window_s() -> float:
    raw = (os.environ.get("WAIT_DEDUPE_MINUTES") or "").strip() or "15"
    try:
        v = float(raw)
    except ValueError:
        return 900.0
    return v * 60.0 if v > 0 else 0.0


def signature(obj) -> str:
    """Gate tag + reasoning + risk verdict reason with every number replaced by '#', whitespace collapsed, 300 chars."""
    parts = (getattr(obj, "gate_tag", None), getattr(obj, "reasoning", None), getattr(obj, "risk_verdict", None),
             getattr(obj, "risk_verdict_reason", None))
    txt = "|".join(str(p or "") for p in parts)
    return _SPACE.sub(" ", _NUM.sub("#", txt)).strip()[:300]


def is_repeat(obj, now: Optional[float] = None) -> bool:
    """True when this pending WAIT row repeats the last one written for its symbol inside the window. Records the row when it is kept."""
    try:
        win = window_s()
        key = (str(getattr(obj, "mode", "") or ""), str(getattr(obj, "symbol", "") or ""))
        t = time.monotonic() if now is None else now
        if getattr(obj, "action", None) != "WAIT":          # ENTER or anything else: the next WAIT is a change, write it
            with _lock:
                _last.pop(key, None)
            return False
        if win <= 0 or not key[1]:
            return False
        sig = signature(obj)
        with _lock:
            prev = _last.get(key)
            if prev is not None and prev[0] == sig and (t - prev[1]) < win:
                _stats["dropped"] += 1
                return True
            if len(_last) >= _MAX_KEYS and key not in _last:
                for k in sorted(_last, key=lambda k: _last[k][1])[: _MAX_KEYS // 10]:
                    _last.pop(k, None)
            _last[key] = (sig, t)
            _stats["kept"] += 1
        return False
    except Exception:  # noqa: BLE001
        return False


def status() -> dict:
    with _lock:
        s = dict(_stats)
        s["tracked"] = len(_last)
    s["window_s"] = window_s()
    return s


def reset_for_tests() -> None:
    with _lock:
        _last.clear()
        _stats.update(kept=0, dropped=0)
