"""orders/short_refusals.py - E285 round 133: what the short gate counters cannot say. Pure; memory only; never raises.

`short_entry._stats["by_code"]` counts every ATTEMPT: the same few shortlisted names are refused again each cycle (2 Nov: 484 refusals over 107 cycles, 5 shortlist rows).
This keeps (1) how many DISTINCT symbols each refusal code hit and (2) the latest code + reason text per symbol - the text carries the numbers, so "MAX_POSITIONS:5" (the whole
book of longs + shorts is full) is told apart from "2 live shorts open (max 2)" (the short cap), which share the code SHORT_MAX_POSITIONS. Bounded; reset with the counters.
Read through GET /short/shadow (`symbols_by_code`, `last_refusal`, and `code` / `why` on each REFUSED shortlist row).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("position-stocks-short-refusals")

SYMS_CAP = 200          # distinct symbols remembered per code
LAST_CAP = 40           # latest-refusal rows kept (newest last)

_code_syms: Dict[str, set] = {}
_last: Dict[str, tuple] = {}


def reset() -> None:
    _code_syms.clear()
    _last.clear()


def note(code: str, symbol: Optional[str], reason: Any = "") -> None:
    try:
        if not symbol:
            return
        sym = str(symbol)
        ss = _code_syms.setdefault(str(code), set())
        if len(ss) < SYMS_CAP:
            ss.add(sym)
        _last.pop(sym, None)                               # re-insert: the dict stays ordered oldest -> newest
        _last[sym] = (str(code), str(reason)[:200])
        while len(_last) > LAST_CAP:
            _last.pop(next(iter(_last)), None)
    except Exception:  # noqa: BLE001
        logger.debug("short_refusals: note ignored (diagnostics only)", exc_info=True)


def why(symbol: Optional[str]) -> Dict[str, str]:
    """{"code", "why"} of the symbol's latest refusal, or {}."""
    try:
        c = _last.get(str(symbol))
        return {"code": c[0], "why": c[1]} if c else {}
    except Exception:  # noqa: BLE001
        return {}


def snapshot() -> Dict[str, Any]:
    try:
        return {"symbols_by_code": {k: len(v) for k, v in _code_syms.items()},
                "last_refusal": {s: {"code": c, "reason": r} for s, (c, r) in _last.items()}}
    except Exception:  # noqa: BLE001
        return {"symbols_by_code": {}, "last_refusal": {}}
