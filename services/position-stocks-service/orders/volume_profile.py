"""E284 round 83: the 'normal pace' of the day's volume learned from the recorded 1-minute bars (the volume half of ADAPTIVE_ENTRY_PARAMS).

liquidity_gate compares today's volume with `normal daily volume x share of the session elapsed`, and that share was a guessed curve, (minutes / 375) ** 0.6.
The recorder keeps real minute volumes (`BAR_RECORD_DIR/<day>/<symbol>.json`, volume = per-minute delta), so the share can be MEASURED: for every complete
recorded day and symbol, the cumulative share of the day's volume traded by each minute; the median over all of them is the curve.

Guard rails (plan, 'Guard rails for every learned value'):
  * needs MIN_DAYS (5) complete recorded days AND MIN_SYMBOL_DAYS (300) symbol-days, else the old curve is used unchanged;
  * shrunk toward the old curve with n / (n + 300), n = symbol-days;
  * the result stays within +-25 % of the old curve value at that minute (a hard bound, so a thin or odd sample cannot move the floor far);
  * built once in a background thread and cached 6 hours; until it is ready, or on any error, the old curve is used (fails open, never raises, never waits).
Only the curve changes; ENTRY_MIN_RVOL, the 09:45 start and every other liquidity test are untouched. VOLUME_PROFILE_LEARNED_ENABLED=0 = the old curve.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

import config

logger = logging.getLogger("position-stocks-volume-profile")

SESSION_MIN = 375
MIN_DAYS = 5
MIN_SYMBOL_DAYS = 300
PRIOR = 300
MAX_DAYS_READ = 20
BAND = 0.25
TTL_S = 6 * 3600.0
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_IST = 19800

_lock = threading.Lock()
_state: Dict[str, Any] = {"profile": None, "built_at": 0.0, "building": False, "error": None}


def reset() -> None:
    with _lock:
        _state.update(profile=None, built_at=0.0, building=False, error=None)


def _median(xs: List[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _day_curve(bars: Any, date: str) -> Optional[List[float]]:
    """Cumulative share (0..1) of the day's volume by minute offset 0..374, or None when the file is not a complete day."""
    try:
        y, m, d = (int(x) for x in date.split("-"))
        import calendar
        t0 = calendar.timegm((y, m, d, 0, 0, 0)) - _IST + (9 * 3600 + 15 * 60)
        vol = [0.0] * SESSION_MIN
        seen = 0
        first = None
        for b in bars:
            off = int(round((float(b[0]) - t0) / 60.0))
            if 0 <= off < SESSION_MIN:
                v = max(0.0, float(b[5]))
                vol[off] += v
                seen += 1
                first = off if first is None else min(first, off)
        if first is None or first > 5 or seen < 0.8 * SESSION_MIN:
            return None
        total = sum(vol)
        if total <= 0 or sum(1 for v in vol if v > 0) < 0.5 * SESSION_MIN:        # a file with no real minute volumes (e.g. a backfill that carried none) says nothing
            return None
        run, out = 0.0, []
        for v in vol:
            run += v
            out.append(run / total)
        return out
    except Exception:  # noqa: BLE001
        return None


def build(root: str) -> Optional[Dict[str, Any]]:
    """Read the recorder folders and return {curve [375], days, symbol_days} or None when there is not enough data. Never raises."""
    try:
        if not os.path.isdir(root):
            return None
        days = sorted((p for p in os.listdir(root) if _DAY.match(p) and os.path.isdir(os.path.join(root, p))), reverse=True)[:MAX_DAYS_READ]
        per_off: List[List[float]] = [[] for _ in range(SESSION_MIN)]
        used_days, sym_days = 0, 0
        skipped = {"unreadable": 0, "index": 0, "not_complete_or_no_volume": 0, "backfilled_used": 0}
        for d in days:
            got = 0
            for fn in os.listdir(os.path.join(root, d)):
                if not fn.endswith(".json") or fn.startswith("_"):
                    continue
                try:
                    with open(os.path.join(root, d, fn)) as fh:
                        doc = json.load(fh)
                    if unquote(fn[:-5]).startswith("^"):
                        skipped["index"] += 1
                        continue
                    c = _day_curve(doc["bars"], d)
                except Exception:  # noqa: BLE001
                    skipped["unreadable"] += 1
                    continue
                if c is None:
                    skipped["not_complete_or_no_volume"] += 1
                    continue
                skipped["backfilled_used"] += 1 if doc.get("backfilled") else 0
                for k in range(SESSION_MIN):
                    per_off[k].append(c[k])
                got += 1
            if got:
                used_days += 1
                sym_days += got
        if used_days < MIN_DAYS or sym_days < MIN_SYMBOL_DAYS:
            return {"curve": None, "days": used_days, "symbol_days": sym_days, "files": skipped}
        curve, last = [], 0.0
        for k in range(SESSION_MIN):
            last = max(last, _median(per_off[k]))           # a cumulative share never falls
            curve.append(min(1.0, last))
        return {"curve": curve, "days": used_days, "symbol_days": sym_days, "files": skipped}
    except Exception as e:  # noqa: BLE001
        logger.debug("volume profile build failed: %s", e)
        return None


def _bg_build() -> None:
    try:
        prof = build(str(getattr(config, "BAR_RECORD_DIR", "/data/bars")))
        with _lock:
            _state.update(profile=prof, built_at=time.time(), error=None if prof is not None else "build failed")
    except Exception as e:  # noqa: BLE001
        with _lock:
            _state.update(built_at=time.time(), error=str(e)[:120])
    finally:
        with _lock:
            _state["building"] = False


def _profile() -> Optional[Dict[str, Any]]:
    with _lock:
        stale = (time.time() - _state["built_at"]) > TTL_S
        if stale and not _state["building"]:
            _state["building"] = True
            threading.Thread(target=_bg_build, name="volume-profile", daemon=True).start()
        return _state["profile"]


def adjust(old_fraction: float, minutes_since_open: float) -> float:
    """The learned share of the day's volume expected by now, bounded to +-25 % of the old curve value; the old value when unavailable. Never raises, never waits."""
    try:
        if not getattr(config, "VOLUME_PROFILE_LEARNED_ENABLED", True) or not getattr(config, "ADAPTIVE_ENTRY_PARAMS", True) or old_fraction <= 0:     # E285 round 110: master switch
            return old_fraction
        prof = _profile()
        curve = (prof or {}).get("curve")
        if not curve:
            return old_fraction
        k = min(SESSION_MIN - 1, max(0, int(minutes_since_open)))
        learned = curve[k]
        n = float(prof.get("symbol_days", 0))
        w = n / (n + PRIOR)
        v = w * learned + (1.0 - w) * old_fraction
        return min(1.0, min(old_fraction * (1 + BAND), max(old_fraction * (1 - BAND), v)))
    except Exception:  # noqa: BLE001
        return old_fraction


def warm() -> None:
    """E285 round 96: start the background build now (at service start) so the curve is ready before the first liquidity check, instead of on that first call. Never raises, never waits."""
    try:
        if getattr(config, "VOLUME_PROFILE_LEARNED_ENABLED", True) and getattr(config, "ADAPTIVE_ENTRY_PARAMS", True):
            _profile()
    except Exception as _e:  # noqa: BLE001
        logger.debug("volume_profile.warm: skipped (%s)", _e)                        # E285 round 98: logged, no longer silent (the group322 pin); a warm-up never blocks start


def snapshot() -> Dict[str, Any]:
    """`building` is True while the background read of the recorder folders is still running (it reads every day's files, so it can take longer than a few seconds): then ready / days read as unknown, not as 'no data'."""
    with _lock:
        p = _state["profile"] or {}
        return {"ready": bool(p.get("curve")), "building": bool(_state["building"]), "days": p.get("days"), "symbol_days": p.get("symbol_days"), "files": p.get("files"),
                "built_age_s": round(time.time() - _state["built_at"]) if _state["built_at"] else None, "error": _state["error"]}
