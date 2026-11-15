"""fno/vix_rank.py - round 550 (P1): where today's India VIX sits inside its own recent range. READ ONLY, for the Chain tab. Nothing here is read by the engine.

WHY VIX AND NOT THE ATM IV: the ATM IV of a weekly chain swings with the days left to expiry (it climbs into expiry day whatever the market is doing), so a rank of it against
older days of other weekdays compares unlike things. India VIX is a constant 30-day measure of NIFTY option prices, so today's value against past days is a fair comparison.

WHERE THE HISTORY COMES FROM: the engine already writes a chain snapshot every minute with the VIX attached (fno/runtime.py -> recorder.record, <DATA_DIR>/snapshots/<day>/<UND>.jsonl).
The last VIX of each past day is the history. Past days never change, so each is read once and remembered in <DATA_DIR>/vix_daily.json (derived data: delete it and it is rebuilt).
Today is never part of the history. Fewer than MIN_DAYS days of history = no rank (analytics.iv_rank refuses), the reply says how many days there are.

Pure stdlib. Never raises: any problem returns a reply with ready=False and a plain note.
"""
from __future__ import annotations

import json
import math
import os
import threading
from typing import Any, Dict, List, Optional

from . import analytics, recorder

MIN_DAYS = 20                 # analytics.iv_rank needs this many values
MAX_DAYS = 260                # about one trading year
SCAN_PER_CALL = 8             # past days read per call, so the first call after a restart cannot stall the route; the rest are read on the next calls
CACHE_NAME = "vix_daily.json"
_UNDERLYINGS = ("NIFTY", "SENSEX")        # NIFTY first: India VIX is the NIFTY volatility index
_lock = threading.Lock()


def _ok(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


def _last_vix_of_day(root: str, day: str) -> Optional[float]:
    """The last usable VIX recorded on `day` (NIFTY file first, then SENSEX), or None. One line is parsed at a time (recorder.index_day)."""
    for und in _UNDERLYINGS:
        _path, rows = recorder.index_day(root, day, und)
        for _ts, _off, _spot, vix, _exp in reversed(rows):
            if _ok(vix):
                return float(vix)
    return None


def _load(path: str) -> Dict[str, Optional[float]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            return {str(k): (float(v) if _ok(v) else None) for k, v in d.items()}
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _save(path: str, doc: Dict[str, Optional[float]]) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def history(root: str, today: str) -> Dict[str, Any]:
    """{"values": [vix of each past recorded day, oldest first, at most MAX_DAYS], "days_recorded": n of past days with a snapshot folder, "pending": past days not read yet}."""
    path = os.path.join(root, CACHE_NAME)
    with _lock:
        cache = _load(path)
        past = [d for d in recorder.list_days(root) if d < today][-MAX_DAYS:]
        todo = [d for d in past if d not in cache]
        changed = False
        for d in todo[:SCAN_PER_CALL]:
            cache[d] = _last_vix_of_day(root, d)
            changed = True
        if changed:
            _save(path, cache)
    values = [cache[d] for d in past if cache.get(d) is not None]
    return {"values": values, "days_recorded": len(past), "pending": max(0, len(todo) - SCAN_PER_CALL)}


def read(root: str, today: str, current_vix: Optional[float]) -> Dict[str, Any]:
    """The reply the chain route adds as `vix_rank`. ready=True only with a current VIX, every recorded past day read (pending == 0) and at least MIN_DAYS past days of VIX."""
    try:
        h = history(root, today)
        vals: List[float] = h["values"]
        out: Dict[str, Any] = {"vix": round(float(current_vix), 2) if _ok(current_vix) else None, "days": len(vals), "need": MIN_DAYS, "pending": h["pending"],
                               "rank": None, "percentile": None, "low": None, "high": None, "ready": False, "note": ""}
        if vals:
            out["low"], out["high"] = round(min(vals), 2), round(max(vals), 2)
        if not _ok(current_vix):
            out["note"] = "India VIX is not available right now"
            return out
        if h["pending"] > 0:       # the history is still being read from disk (a few days per call): a rank from part of it would mislead, so none is given yet
            out["note"] = f"reading the recorded days ({h['pending']} left); the rank appears when that is done"
            return out
        r = analytics.iv_rank(float(current_vix), vals)
        if r is None:
            out["note"] = f"{len(vals)} of {MIN_DAYS} days of VIX history recorded so far; the rank starts at {MIN_DAYS}"
            return out
        out.update({"rank": r["iv_rank"], "percentile": r["iv_percentile"], "ready": True,
                    "note": f"today's VIX against the last {len(vals)} recorded days (rank 0 = the lowest of them, 100 = the highest)"})
        return out
    except Exception as e:  # noqa: BLE001 - a display helper must never break the chain view
        return {"vix": None, "days": 0, "need": MIN_DAYS, "pending": 0, "rank": None, "percentile": None, "low": None, "high": None, "ready": False,
                "note": "VIX rank unavailable (" + type(e).__name__ + ")"}