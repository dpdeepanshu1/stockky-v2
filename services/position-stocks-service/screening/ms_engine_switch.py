"""Group 349: one place that decides whether the Market Board zones/swings come from the legacy code or from the new engine adapter.

BOARD_USE_MS_ENGINE=1 (default, live since group354; config.py) -> `stockky_ms.compat_board` (same output shape; one documented one-bar difference in zone status).
BOARD_USE_MS_ENGINE=0                                             -> legacy `screening.market_structure` (unchanged behaviour; the rollback switch).
Any import or runtime error in the new path falls back to the legacy result for that call, so the board never breaks because of it.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import config
from screening import market_structure as ms

logger = logging.getLogger("position-stocks.ms_switch")

try:
    from stockky_ms import compat_board as _cb
except Exception:  # noqa: BLE001 - package not vendored in this image
    _cb = None


# group 351: observability. Process-local counters; read by /board/status. Never raise.
_stats = {"engine_calls": 0, "legacy_calls": 0, "fallbacks": 0, "last_error": None}


def stats() -> dict:
    """What the board is actually running on: requested flag, whether the adapter is importable/active, engine version, call counts."""
    ver = None
    try:
        from stockky_ms.ms_types import MS_ENGINE_VERSION as ver  # noqa: N811
    except Exception:  # noqa: BLE001
        ver = None
    return {"requested": bool(getattr(config, "BOARD_USE_MS_ENGINE", False)), "vendored": _cb is not None, "active": engine_active(),
            "engine_version": ver, **_stats, "ab": ab_report()}


def reset_stats() -> None:
    _stats.update(engine_calls=0, legacy_calls=0, fallbacks=0, last_error=None)


def engine_active() -> bool:
    return bool(getattr(config, "BOARD_USE_MS_ENGINE", False)) and _cb is not None


_AB_COUNTERS = ("compared", "identical", "zones_differ", "swings_differ", "engine_dropped", "engine_added", "status_changed", "bounds_changed",
                "other_changed", "served_differs", "cap_knockon", "errors")
_AB_MIN_SAMPLE = 200
_AB_SAMPLES = 50
_AB_DAYS = 14
_AB_BIG = 10_000                      # "no cap": the comparison runs before the board's newest-6 cap
_ab_lock = threading.Lock()
_ab: Dict[str, Any] = {}


def reset_ab() -> None:
    with _ab_lock:
        _ab.clear()
        _ab.update({k: 0 for k in _AB_COUNTERS})
        _ab.update(last_error=None, samples=[], days={})


reset_ab()


def _close(a: Any, b: Any) -> bool:
    try:
        fa, fb = float(a), float(b)
        return abs(fa - fb) <= 1e-9 * max(1.0, abs(fa), abs(fb))
    except (TypeError, ValueError):
        return a == b


def compare(legacy_zones: List[dict], engine_zones: List[dict], legacy_swings: List[dict], engine_swings: List[dict]) -> Dict[str, Any]:
    """Pure. Zones are matched on (side, idx); `dropped` = only legacy has it, `added` = only the engine has it; matched zones are checked for
    status, bounds (top/bottom) and every other shared field. Swings must match one-to-one on (idx, kind, price)."""
    lm = {(z.get("side"), z.get("idx")): z for z in legacy_zones or []}
    em = {(z.get("side"), z.get("idx")): z for z in engine_zones or []}
    dropped = sorted(k for k in lm if k not in em)
    added = sorted(k for k in em if k not in lm)
    status: list = []
    bounds: list = []
    other: list = []
    for k in sorted(k for k in lm if k in em):
        a, b = lm[k], em[k]
        if a.get("status") != b.get("status"):
            status.append(k)
        if not (_close(a.get("top"), b.get("top")) and _close(a.get("bottom"), b.get("bottom"))):
            bounds.append(k)
        if any(not _close(a.get(f), b.get(f)) for f in a if f in b and f not in ("side", "idx", "status", "top", "bottom")):
            other.append(k)
    ls, es = legacy_swings or [], engine_swings or []
    swings_differ = len(ls) != len(es) or any(
        x.get("idx") != y.get("idx") or x.get("kind") != y.get("kind") or not _close(x.get("price"), y.get("price")) for x, y in zip(ls, es))
    identical = not (dropped or added or status or bounds or other or swings_differ)
    return {"identical": identical, "dropped": dropped, "added": added, "status": status, "bounds": bounds, "other": other, "swings_differ": swings_differ}


def _day_of(bars) -> str:
    try:
        t = float(bars[-1][0])
        if t > 1e12:
            t /= 1000.0
        return (datetime.fromtimestamp(t, timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return "unknown"


def _record_ab(symbol: Optional[str], bars, res: Dict[str, Any], served_differs: Any, cap_knockon: Any) -> None:
    zones_differ = bool(res["dropped"] or res["added"] or res["status"] or res["bounds"] or res["other"])
    day = _day_of(bars)
    with _ab_lock:
        _ab["compared"] += 1
        _ab["identical"] += 1 if res["identical"] else 0
        _ab["zones_differ"] += 1 if zones_differ else 0
        _ab["swings_differ"] += 1 if res["swings_differ"] else 0
        _ab["engine_dropped"] += len(res["dropped"])
        _ab["engine_added"] += len(res["added"])
        _ab["status_changed"] += len(res["status"])
        _ab["bounds_changed"] += len(res["bounds"])
        _ab["other_changed"] += len(res["other"])
        _ab["served_differs"] += 1 if served_differs else 0
        _ab["cap_knockon"] += 1 if cap_knockon else 0
        d = _ab["days"].setdefault(day, {"compared": 0, "identical": 0})
        d["compared"] += 1
        d["identical"] += 1 if res["identical"] else 0
        for old in sorted(_ab["days"])[:-_AB_DAYS]:
            _ab["days"].pop(old, None)
        if not res["identical"]:
            _ab["samples"].insert(0, {"symbol": symbol, "day": day, "dropped": [list(k) for k in res["dropped"]], "added": [list(k) for k in res["added"]],
                                      "status": [list(k) for k in res["status"]], "bounds": [list(k) for k in res["bounds"]],
                                      "other": [list(k) for k in res["other"]], "swings_differ": res["swings_differ"],
                                      "served_differs": bool(served_differs), "cap_knockon": bool(cap_knockon)})
            del _ab["samples"][_AB_SAMPLES:]


def _ab_compare(symbol: Optional[str], bars, atr_mult: float, full_range: bool, served: Tuple[List[dict], List[dict]]) -> None:
    """Run the legacy maths on the same bars and count the differences against what the engine served. Never raises, never touches `served`."""
    try:
        cap = int(getattr(config, "BOARD_AB_MAX_COMPARES", 0) or 0)
        with _ab_lock:
            if cap > 0 and _ab["compared"] >= cap:
                return
        eng_all = _cb.find_zones(bars, atr_mult=atr_mult, max_zones=_AB_BIG, full_range=full_range)
        leg_all = ms.find_zones(bars, atr_mult=atr_mult, max_zones=_AB_BIG, full_range=full_range)
        leg_swings = ms.swings(bars)
        res = compare(leg_all, eng_all, leg_swings, served[1])
        leg_served = ms.find_zones(bars, atr_mult=atr_mult, full_range=full_range)
        served_differs = not compare(leg_served, served[0], [], [])["identical"]
        # the visible difference is only the newest-6 window sliding after the engine dropped a zone: not a contract break
        knockon = bool(served_differs and res["dropped"] and not (res["added"] or res["status"] or res["bounds"] or res["other"]))
        _record_ab(symbol, bars, res, served_differs, knockon)
    except Exception as e:  # noqa: BLE001
        with _ab_lock:
            _ab["errors"] += 1
            _ab["last_error"] = f"{type(e).__name__}: {e}"[:200]
        logger.debug("board A/B comparison failed (ignored): %s", e)


def ab_report() -> Dict[str, Any]:
    with _ab_lock:
        r = {k: _ab[k] for k in _AB_COUNTERS}
        r.update(last_error=_ab["last_error"], samples=[dict(x) for x in _ab["samples"]], days={k: dict(v) for k, v in _ab["days"].items()})
    n = r["compared"]
    r["pct"] = {k: (round(100.0 * r[k] / n, 1) if n else 0.0) for k in _AB_COUNTERS if k not in ("compared", "errors")}
    if not bool(getattr(config, "BOARD_AB_ENABLED", True)):
        r["verdict"] = "disabled"
    elif n < _AB_MIN_SAMPLE:
        r["verdict"] = "insufficient_sample"
    elif r["engine_added"] or r["swings_differ"] or r["errors"]:
        r["verdict"] = "contract_break"
    else:
        r["verdict"] = "parity_ok"
    r["enabled"] = bool(getattr(config, "BOARD_AB_ENABLED", True))
    r["min_sample"] = _AB_MIN_SAMPLE
    return r


def zones_and_swings(bars, atr_mult: float, full_range: bool, symbol: Optional[str] = None) -> Tuple[List[dict], List[dict]]:
    if engine_active():
        try:
            out = _cb.find_zones(bars, atr_mult=atr_mult, full_range=full_range), _cb.swings(bars)
            _stats["engine_calls"] += 1
        except Exception as e:  # noqa: BLE001
            out = None
            _stats["fallbacks"] += 1
            _stats["last_error"] = f"{type(e).__name__}: {e}"[:200]
            logger.warning("ms engine board path failed (%s); using legacy", e)
        if out is not None:
            if getattr(config, "BOARD_AB_ENABLED", True):
                _ab_compare(symbol, bars, atr_mult, full_range, out)      # observational: runs after the engine result, never alters it
            return out
    _stats["legacy_calls"] += 1
    return ms.find_zones(bars, atr_mult=atr_mult, full_range=full_range), ms.swings(bars)
