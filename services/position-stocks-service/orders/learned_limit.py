"""orders/learned_limit.py - E275 round 39: a learned exit number may move by at most 25 % a week (plan: "Guard rails for every learned value").

setup_stats.exit_numbers already shrinks a learned value toward the old constant (n / (n + 30)), but a cell that suddenly gets a few lucky or unlucky trades can still
swing its median time-to-peak or typical gain a long way in one day. This module keeps one ANCHOR per learned value and lets the value in use stay within +-LEARNED_WEEKLY_LIMIT_PCT
(default 25) of that anchor:

  - the first time a value is seen, the anchor is its starting constant (the fixed minutes the old rule used) when there is one, otherwise the value itself;
  - the anchor moves up to the value in use once the window (7 days) has passed, so a real change in the setup is followed at up to 25 % a week;
  - the value in use is always the learned value clamped to [anchor x (1 - p), anchor x (1 + p)].

Anchors are kept in memory and in a small JSON file (LEARNED_LIMIT_FILE, default <BAR_RECORD_DIR>/_learned_limits.json, written atomically, at most every 30 s). A missing or
unreadable file starts fresh (the starting constants), and a write that fails is ignored: the limit is a safety rail, it must never stop an exit. LEARNED_WEEKLY_LIMIT_ENABLED = false
gives the shrunk value unchanged. Pure helpers never raise.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

import config

logger = logging.getLogger("position-stocks-learned-limit")

WINDOW_S = 7 * 24 * 3600.0
_FLUSH_EVERY_S = 30.0

_lock = threading.Lock()
_state: Dict[str, Dict[str, float]] = {}
_loaded = False
_dirty = False
_last_flush = 0.0


def enabled() -> bool:
    return bool(getattr(config, "LEARNED_WEEKLY_LIMIT_ENABLED", True))


def limit_frac() -> float:
    """The largest weekly change as a fraction (0.25 default); bounded 0.01 .. 1.0 so a typo can neither freeze nor free a value."""
    try:
        return max(0.01, min(1.0, float(getattr(config, "LEARNED_WEEKLY_LIMIT_PCT", 25.0)) / 100.0))
    except (TypeError, ValueError):
        return 0.25


def _path() -> str:
    p = str(getattr(config, "LEARNED_LIMIT_FILE", "") or "").strip()
    return p or os.path.join(str(getattr(config, "BAR_RECORD_DIR", "/data/bars")), "_learned_limits.json")


def clamp_step(anchor: float, value: float, frac: float) -> float:
    """`value` held within anchor x (1 - frac) .. anchor x (1 + frac). A non-positive anchor cannot be a base for a percentage: the value passes."""
    if anchor <= 0:
        return value
    return max(anchor * (1.0 - frac), min(anchor * (1.0 + frac), value))


def _load_locked() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(_path(), encoding="utf-8") as f:
            raw = json.load(f)
        for k, v in (raw.get("anchors") or {}).items():
            _state[str(k)] = {"anchor": float(v["anchor"]), "since": float(v["since"]), "used": float(v.get("used", v["anchor"]))}
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning("learned limit: could not read %s (starting from the constants): %s", _path(), e)


def _flush_locked(now: float, force: bool = False) -> None:
    global _dirty, _last_flush
    if not _dirty or (not force and now - _last_flush < _FLUSH_EVERY_S):
        return
    _last_flush = now
    try:
        path = _path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"anchors": _state}, f, separators=(",", ":"))
        os.replace(tmp, path)
        _dirty = False
    except Exception as e:  # noqa: BLE001
        logger.debug("learned limit: write failed (kept in memory): %s", e)


def limit(key: str, value: Optional[float], start: Optional[float] = None, now_ts: Optional[float] = None) -> Tuple[Optional[float], bool]:
    """(value in use, was_clamped) for one learned value. `start` is the old constant, used as the anchor the first time `key` is seen. Never raises; on any error or when
    the switch is off the value passes unchanged."""
    global _dirty
    try:
        if value is None or not enabled():
            return value, False
        v = float(value)
        now = float(now_ts) if now_ts is not None else time.time()
        frac = limit_frac()
        with _lock:
            _load_locked()
            e = _state.get(key)
            if e is None:
                base = float(start) if start is not None and float(start) > 0 else v
                e = _state[key] = {"anchor": base, "since": now, "used": base}
                _dirty = True
            elif now - e["since"] >= WINDOW_S:
                e["anchor"], e["since"] = e["used"], now                      # the window rolled: the value in use becomes the new base
                _dirty = True
            used = clamp_step(e["anchor"], v, frac)
            if abs(used - e["used"]) > 1e-9:
                e["used"] = used
                _dirty = True
            _flush_locked(now)
            return round(used, 4), abs(used - v) > 1e-9
    except Exception as e:  # noqa: BLE001
        logger.debug("learned limit failed for %s: %s", key, e)
        return value, False


def limit_exit_numbers(cell_key: str, consumer: str, num: Dict[str, Any], ttp_start: Optional[float], now_ts: Optional[float] = None) -> Dict[str, Any]:
    """Apply the weekly limit to the learned parts of setup_stats.exit_numbers() output (a copy is returned). `consumer` keeps the time stop and the momentum exit apart because
    their constants differ. Only values whose source says "learned" are limited: a fallback constant is already the old number. A limited value adds " capped 25%/wk" to its source."""
    out = dict(num or {})
    try:
        tag = f" capped {limit_frac() * 100:.0f}%/wk"
        if str(out.get("ttp_source", "")).startswith("learned"):
            v, hit = limit(f"{cell_key}|{consumer}|ttp_min", out.get("ttp_min"), ttp_start, now_ts)
            out["ttp_min"] = v
            if hit:
                out["ttp_source"] = f"{out['ttp_source']}{tag}"
        if str(out.get("gain_source", "")).startswith("learned"):
            v, hit = limit(f"{cell_key}|{consumer}|typical_gain_pct", out.get("typical_gain_pct"), None, now_ts)
            out["typical_gain_pct"] = v
            if hit:
                out["gain_source"] = f"{out['gain_source']}{tag}"
    except Exception as e:  # noqa: BLE001
        logger.debug("learned limit: exit numbers for %s failed: %s", cell_key, e)
    return out


def snapshot() -> Dict[str, Dict[str, float]]:
    """A copy of the anchors (for status and tests)."""
    with _lock:
        _load_locked()
        return {k: dict(v) for k, v in _state.items()}


def flush() -> None:
    with _lock:
        _flush_locked(time.time(), force=True)


def reset() -> None:
    """Forget everything in memory (tests). The file is left alone."""
    global _loaded, _dirty, _last_flush
    with _lock:
        _state.clear()
        _loaded, _dirty, _last_flush = False, False, 0.0
