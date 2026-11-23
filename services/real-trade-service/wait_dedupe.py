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

Round 682 (opt-in, default unchanged): WAIT_DEDUPE_MODE=any_sig remembers EVERY signature written for the symbol inside the window, not only the last one, so a name that
flips between two gate tags (A, B, A, B every cycle: 23 Nov wrote 1,491 rows, the busiest names 32-39 each, more than the 22 a day one unchanged signature allows) writes
each signature once per window instead of a row at every flip. The first row of every (symbol, signature) is still written, so the name, the gate tag and the first-block
time of every refusal stay in the table; only the repeat count shrinks. Unset / blank / any other value = the rule above ("last"). Switch it on only after
scripts/wait_dedupe_sim.py shows it drops 30 % or more of a day's rows.
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
_seen: Dict[Tuple[str, str], Dict[str, float]] = {}       # round 682, any_sig mode: every signature written for the key -> time written
_MAX_SIGS = 40                                            # signatures remembered per symbol (oldest dropped first)
_stats = {"kept": 0, "dropped": 0}


def window_s() -> float:
    raw = (os.environ.get("WAIT_DEDUPE_MINUTES") or "").strip() or "15"
    try:
        v = float(raw)
    except ValueError:
        return 900.0
    return v * 60.0 if v > 0 else 0.0


def mode() -> str:
    """'any_sig' only when WAIT_DEDUPE_MODE says so (case and spaces ignored); anything else, including unset or blank, is the 'last' rule."""
    return "any_sig" if (os.environ.get("WAIT_DEDUPE_MODE") or "").strip().lower() == "any_sig" else "last"


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
                _seen.pop(key, None)
            return False
        if win <= 0 or not key[1]:
            return False
        sig = signature(obj)
        if mode() == "any_sig":
            with _lock:
                seen = _seen.get(key)
                if seen is None:
                    if len(_seen) >= _MAX_KEYS:
                        for k in sorted(_seen, key=lambda k: max(_seen[k].values(), default=0.0))[: _MAX_KEYS // 10]:
                            _seen.pop(k, None)
                    seen = _seen[key] = {}
                t0 = seen.get(sig)
                if t0 is not None and (t - t0) < win:
                    _stats["dropped"] += 1
                    return True
                seen[sig] = t
                if len(seen) > _MAX_SIGS:
                    for k in sorted(seen, key=seen.get)[: len(seen) - _MAX_SIGS]:
                        seen.pop(k, None)
                _stats["kept"] += 1
            return False
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
        s["tracked"] = len(_seen) if mode() == "any_sig" else len(_last)
    s["window_s"] = window_s()
    s["mode"] = mode()
    return s


def reset_for_tests() -> None:
    with _lock:
        _last.clear()
        _seen.clear()
        _stats.update(kept=0, dropped=0)
