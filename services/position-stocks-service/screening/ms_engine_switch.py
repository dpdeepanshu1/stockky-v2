"""Group 349: one place that decides whether the Market Board zones/swings come from the legacy code or from the new engine adapter.

BOARD_USE_MS_ENGINE=0 -> legacy `screening.market_structure` (unchanged behaviour).
BOARD_USE_MS_ENGINE=1           -> `stockky_ms.compat_board` (same output shape; one documented one-bar difference in zone status).
Any import or runtime error in the new path falls back to the legacy result for that call, so the board never breaks because of it.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
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


# ── group362: A/B parity comparison (observational only - what is SERVED never changes) ───────────────────────────────────────────────
# While the engine is active, each fresh zone computation is ALSO run through the legacy code and the two results are compared, so a day of
# real boards says how often (and how) the new maths differs. This is a parity check, not a performance verdict: the documented difference is
# "never adds a zone, may call a zone `tested` that legacy still calls `fresh`, may drop a zone a later bar already closed through".
# `engine_added` (a zone only the engine has) and `swings_differ` should stay 0; either one is a contract break worth reading the samples for.
# The comparison runs BEFORE the board's newest-6 cap, so `engine_added` is exact. The board keeps only the newest 6: when the engine drops one zone
# an older one can slide into that window, which is a visible difference but no contract break - counted separately as `cap_knockon`
# (served shows an extra zone that the uncapped comparison does not; `served_differs` = what the board user actually sees differing, any cause).
_ab_lock = threading.Lock()
_ab: Dict[str, Any] = {}
_ab_days: Dict[str, Dict[str, int]] = {}
_ab_samples: "deque[dict]" = deque(maxlen=50)
_AB_COUNTERS = ("compared", "identical", "zones_differ", "swings_differ", "engine_dropped", "engine_added", "status_changed", "bounds_changed",
                "other_changed", "served_differs", "cap_knockon", "errors")
_AB_MIN_COMPARES = 200
_SERVED_CAP = 6          # the default max_zones of both find_zones() - what the board actually keeps
_IST = timezone(timedelta(hours=5, minutes=30))
_EPS = 1e-9


def reset_ab() -> None:
    with _ab_lock:
        _ab.clear()
        _ab.update({k: 0 for k in _AB_COUNTERS})
        _ab.update(zones_legacy=0, zones_engine=0, since=time.time(), last_error=None)
        _ab_days.clear()
        _ab_samples.clear()


reset_ab()


def ab_enabled() -> bool:
    return bool(getattr(config, "BOARD_AB_ENABLED", True))


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) <= _EPS * max(1.0, abs(float(a)))
    return a == b


def _zkey(z: dict) -> Tuple[Any, Any]:
    return (z.get("side"), z.get("idx"))


def compare(legacy_zones: List[dict], engine_zones: List[dict], legacy_swings: List[dict], engine_swings: List[dict]) -> Dict[str, Any]:
    """Pure: how the engine's zones / swings differ from the legacy ones for the same bars. Zones are matched on (side, idx).
    Returns {identical, swings_differ, dropped[], added[], status[], bounds[], other[]} (lists hold the zone keys)."""
    lz = {_zkey(z): z for z in legacy_zones}
    ez = {_zkey(z): z for z in engine_zones}
    dropped = [k for k in lz if k not in ez]
    added = [k for k in ez if k not in lz]
    status, bounds, other = [], [], []
    for k in lz:
        if k not in ez:
            continue
        a, b = lz[k], ez[k]
        if a.get("status") != b.get("status"):
            status.append(k)
        if not (_same(a.get("top"), b.get("top")) and _same(a.get("bottom"), b.get("bottom"))):
            bounds.append(k)
        if any(not _same(a.get(f), b.get(f)) for f in ("t", "bos", "fvg", "valid", "impulse", "atr")):
            other.append(k)
    sw_a = [(x.get("idx"), x.get("kind"), x.get("price")) for x in legacy_swings]
    sw_b = [(x.get("idx"), x.get("kind"), x.get("price")) for x in engine_swings]
    swings_differ = len(sw_a) != len(sw_b) or any(not (p[0] == q[0] and p[1] == q[1] and _same(p[2], q[2])) for p, q in zip(sw_a, sw_b))
    return {"identical": not (dropped or added or status or bounds or other or swings_differ), "swings_differ": swings_differ,
            "dropped": dropped, "added": added, "status": status, "bounds": bounds, "other": other}


def _record_ab(symbol: Optional[str], bars: Any, res: Dict[str, Any], n_legacy: int, n_engine: int) -> None:
    day = datetime.fromtimestamp(bars[-1][0], _IST).strftime("%Y-%m-%d") if bars else "?"
    with _ab_lock:
        _ab["compared"] += 1
        _ab["zones_legacy"] += n_legacy
        _ab["zones_engine"] += n_engine
        d = _ab_days.setdefault(day, {"compared": 0, "identical": 0})
        d["compared"] += 1
        if res.get("served_differs"):
            _ab["served_differs"] += 1
        if res.get("cap_knockon"):
            _ab["cap_knockon"] += 1
        if res["identical"]:
            _ab["identical"] += 1
            d["identical"] += 1
            return
        if res["dropped"] or res["added"] or res["status"] or res["bounds"] or res["other"]:
            _ab["zones_differ"] += 1
        if res["swings_differ"]:
            _ab["swings_differ"] += 1
        for key, name in (("dropped", "engine_dropped"), ("added", "engine_added"), ("status", "status_changed"), ("bounds", "bounds_changed"),
                          ("other", "other_changed")):
            if res[key]:
                _ab[name] += 1
        if len(_ab_days) > 14:
            for old in sorted(_ab_days)[:-14]:
                _ab_days.pop(old, None)
        _ab_samples.append({"at": round(time.time(), 1), "symbol": symbol, "bar_day": day, "bars": len(bars), "swings_differ": res["swings_differ"],
                            "cap_knockon": bool(res.get("cap_knockon")),
                            **{k: [list(x) for x in res[k][:6]] for k in ("dropped", "added", "status", "bounds", "other")}})


def _ab_compare(symbol: Optional[str], bars: Any, atr_mult: float, full_range: bool, served: Tuple[List[dict], List[dict]]) -> None:
    """Run both maths on the same bars BEFORE the board's newest-6 cap and compare, so `engine_added` is exact (a zone only the engine has).
    The served (capped) engine zones are then checked against the capped legacy ones: an extra zone there that the uncapped comparison does not
    show is `cap_knockon` (the engine dropped one zone, so an older one slid into the newest 6). Never raises, never touches `served`."""
    try:
        cap = int(getattr(config, "BOARD_AB_MAX_COMPARES", 100000) or 0)
        if cap > 0 and _ab["compared"] >= cap:
            return
        big = 1_000_000
        lz = ms.find_zones(bars, atr_mult=atr_mult, max_zones=big, full_range=full_range)
        ez = _cb.find_zones(bars, atr_mult=atr_mult, max_zones=big, full_range=full_range)
        res = compare(lz, ez, ms.swings(bars), served[1])
        seen = compare(lz[-_SERVED_CAP:], served[0], [], [])        # what the board user sees: capped legacy vs the capped engine zones served
        res["served_differs"] = not seen["identical"]
        res["cap_knockon"] = bool(seen["added"] and not res["added"])
        _record_ab(symbol, bars, res, len(lz), len(ez))
    except Exception as e:  # noqa: BLE001
        with _ab_lock:
            _ab["errors"] += 1
            _ab["last_error"] = f"{type(e).__name__}: {e}"[:200]


def ab_report() -> Dict[str, Any]:
    """The A/B result in words: counters, per-day volume, rates, the newest differing samples and a verdict.
    verdict: disabled | insufficient_sample (< 200 comparisons) | contract_break (engine added a zone / swings differ / compare errors) | parity_ok."""
    with _ab_lock:
        c = {k: v for k, v in _ab.items() if k != "since"}
        days = {d: dict(v) for d, v in sorted(_ab_days.items())}
        samples = list(_ab_samples)[::-1]
        since = _ab.get("since")
    n = c["compared"]
    pct = (lambda x: round(100.0 * x / n, 2) if n else None)
    if not ab_enabled():
        verdict, advice = "disabled", "BOARD_AB_ENABLED=0 - no comparison is running."
    elif n < _AB_MIN_COMPARES:
        verdict, advice = "insufficient_sample", f"{n} of {_AB_MIN_COMPARES} comparisons so far; check again after a full session (engine must be active)."
    elif c["engine_added"] or c["swings_differ"] or c["errors"]:
        verdict = "contract_break"
        advice = ("The engine added a zone legacy does not have, or swings differ, or the comparison errored. Read `samples`; if they are real, "
                  "set BOARD_USE_MS_ENGINE=0 until fixed.")
    else:
        verdict = "parity_ok"
        advice = ("Only the documented one-bar differences (and zones the engine dropped) were seen - the engine can stay on. After a clean week "
                  "the legacy fallback can be retired.")
    return {"enabled": ab_enabled(), "verdict": verdict, "advice": advice, "min_compares": _AB_MIN_COMPARES, "since_epoch": since, **c,
            "pct": {"identical": pct(c["identical"]), "zones_differ": pct(c["zones_differ"]), "status_changed": pct(c["status_changed"]),
                    "engine_dropped": pct(c["engine_dropped"]), "engine_added": pct(c["engine_added"]), "swings_differ": pct(c["swings_differ"]),
                    "served_differs": pct(c["served_differs"]), "cap_knockon": pct(c["cap_knockon"])},
            "days": days, "samples": samples}


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


def zones_and_swings(bars, atr_mult: float, full_range: bool, symbol: Optional[str] = None) -> Tuple[List[dict], List[dict]]:
    if engine_active():
        try:
            out = _cb.find_zones(bars, atr_mult=atr_mult, full_range=full_range), _cb.swings(bars)
            _stats["engine_calls"] += 1
            if ab_enabled():
                _ab_compare(symbol, bars, atr_mult, full_range, out)      # group362: observational; `out` is what gets served, unchanged
            return out
        except Exception as e:  # noqa: BLE001
            _stats["fallbacks"] += 1
            _stats["last_error"] = f"{type(e).__name__}: {e}"[:200]
            logger.warning("ms engine board path failed (%s); using legacy", e)
    _stats["legacy_calls"] += 1
    return ms.find_zones(bars, atr_mult=atr_mult, full_range=full_range), ms.swings(bars)
