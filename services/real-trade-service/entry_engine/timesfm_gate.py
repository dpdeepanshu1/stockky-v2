"""entry_engine/timesfm_gate.py - TimesFM parameter record on the entry path (Gate 4g, group 434). LOG ONLY.

QUESTION IT RECORDS: "what would a nightly TimesFM-style forecast of range / volatility / volume say about this candidate?" The
timesfm-forecast-service returns parameters (vol_ratio = forecast / trailing volatility, volume_pace, p_stop_close_max, fan_range_pct), never a
direction. This module turns them into a HYPOTHETICAL verdict, counts it, and writes one greppable `TIMESFM_GATE {json}` log line per candidate so
the verdict can be joined to the trade outcome later.

MODES (config.TIMESFM_GATE_MODE / TIMESFM_GATE_DEMO_MODE): off (default, nothing is fetched) | log. There is NO enforce mode on purpose: the plan
(Gate B) allows a blocking rule only after the offline probe and the replay pass, so any other value, `enforce` included, is treated as off.
NO OPINION: a service that is down, slow, stale or unsure never changes anything. The fetch reads the CACHE (cached_only=true): no model runs
in the hot path. Every public function is safe to call: evaluate() and the helpers never raise; the async fetch returns None on any failure.
"""
from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple

import config

logger = logging.getLogger("real-trade-timesfm-gate")

TAG = "timesfm"
_cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
_stats: Dict[str, Any] = {}
_recent: "Deque[Dict[str, Any]]" = deque(maxlen=100)


def _f(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def mode_for(trade_mode: str) -> str:
    """off | log for REAL / DEMO. Anything unrecognised (enforce included) is off."""
    try:
        m = config.TIMESFM_GATE_MODE if str(trade_mode).upper() == "REAL" else config.TIMESFM_GATE_DEMO_MODE
        return "log" if m == "log" else "off"
    except Exception:  # noqa: BLE001
        return "off"


def evaluate(features: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure HYPOTHETICAL verdict. Keys: opinion ('ok'|'no_opinion'), would_reject, reasons[], vol_ratio, p_stop_close_max, volume_pace, fan_range_pct,
    as_of, no_data. Never raises. Thresholds are hypotheses until the probe and replay pass (plan Gates A and B)."""
    base: Dict[str, Any] = {"opinion": "no_opinion", "would_reject": False, "reasons": [], "vol_ratio": None, "p_stop_close_max": None,
                            "volume_pace": None, "fan_range_pct": None, "as_of": None, "no_data": None}
    try:
        if not isinstance(features, dict):
            base["no_data"] = "no forecast"
            return base
        vr, ps = _f(features.get("vol_ratio")), _f(features.get("p_stop_close_max"))
        pace, fr = _f(features.get("volume_pace")), _f(features.get("fan_range_pct"))
        if vr is None and ps is None and pace is None:
            base["no_data"] = "incomplete forecast"
            return base
        base.update(opinion="ok", vol_ratio=vr, p_stop_close_max=ps, volume_pace=pace, fan_range_pct=fr, as_of=features.get("as_of"))
        reasons = []
        lim = float(config.TIMESFM_MAX_VOL_RATIO or 0.0)
        if lim > 0 and vr is not None and vr >= lim:
            reasons.append(f"forecast volatility is {vr:.2f}x the trailing 20-bar volatility (limit {lim:g}x)")
        lim = float(config.TIMESFM_MAX_P_STOP or 0.0)
        if lim > 0 and ps is not None and ps >= lim:
            reasons.append(f"forecast puts {ps * 100:.0f}% on a close at or below the stop within the horizon (limit {lim * 100:.0f}%)")
        lim = float(config.TIMESFM_MIN_VOLUME_PACE or 0.0)
        if lim > 0 and pace is not None and pace < lim:
            reasons.append(f"forecast volume pace {pace:.2f}x the trailing average (floor {lim:g}x)")
        base["reasons"] = reasons
        base["would_reject"] = bool(reasons)
        return base
    except Exception as e:  # noqa: BLE001
        logger.debug("timesfm evaluate failed (no opinion): %s", e)
        base.update(opinion="no_opinion", would_reject=False, reasons=[], no_data="evaluation error")
        return base


def reset_stats() -> None:
    _stats.clear()
    _stats.update(evaluated=0, ok=0, no_opinion=0, would_reject=0, fetch_ok=0, fetch_fail=0, fetch_cache_hit=0, errors=0, since=time.time())
    _recent.clear()


reset_stats()


def reset() -> None:
    _cache.clear()
    reset_stats()


def record(symbol: str, trade_mode: str, verdict: Dict[str, Any], gate_mode: str, price: Any = None,
           stop_pct: Any = None, target_pct: Any = None) -> None:
    """Count one evaluated candidate, keep it in the recent ring and write one greppable log line (the evidence for the shadow phase). Never raises."""
    try:
        _stats["evaluated"] += 1
        _stats["ok" if verdict.get("opinion") == "ok" else "no_opinion"] += 1
        if verdict.get("would_reject"):
            _stats["would_reject"] += 1
        row = {"ts": time.time(), "symbol": str(symbol or "").upper(), "mode": str(trade_mode or "").upper(), "gate_mode": gate_mode,
               "opinion": verdict.get("opinion"), "would_reject": bool(verdict.get("would_reject")), "vol_ratio": verdict.get("vol_ratio"),
               "p_stop_close_max": verdict.get("p_stop_close_max"), "volume_pace": verdict.get("volume_pace"),
               "fan_range_pct": verdict.get("fan_range_pct"), "as_of": verdict.get("as_of"), "reasons": list(verdict.get("reasons") or []),
               "no_data": verdict.get("no_data"), "price": _f(price), "stop_pct": _f(stop_pct), "target_pct": _f(target_pct)}
        _recent.append(row)
        logger.info("TIMESFM_GATE %s", json.dumps(row, separators=(",", ":"), default=str))
    except Exception:  # noqa: BLE001
        _stats["errors"] = _stats.get("errors", 0) + 1


def stats() -> Dict[str, Any]:
    try:
        cfg = {k: getattr(config, k, None) for k in ("TIMESFM_GATE_MODE", "TIMESFM_GATE_DEMO_MODE", "TIMESFM_GATE_REQUESTED", "TIMESFM_URL",
                                                     "TIMESFM_TIMEOUT_S", "TIMESFM_CACHE_TTL_S", "TIMESFM_MAX_VOL_RATIO", "TIMESFM_MAX_P_STOP",
                                                     "TIMESFM_MIN_VOLUME_PACE")}
        return {**{k: v for k, v in _stats.items() if k != "since"}, "since_epoch": _stats.get("since"), "config": cfg,
                "recent": list(reversed(list(_recent)))[:30]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}


async def fetch_features(symbol: str, stop_pct, target_pct) -> Optional[Dict[str, Any]]:
    """Cached parameters from the service (cached_only=true: a missing or stale forecast is 404 = no opinion). None on any failure; a failure is
    remembered TIMESFM_FAIL_TTL_S so a dead service is not hammered. Success is cached TIMESFM_CACHE_TTL_S per symbol and stop / target pair."""
    sym = str(symbol or "").strip().upper()
    sp, tp = _f(stop_pct), _f(target_pct)
    if not sym or sp is None or sp <= 0 or tp is None or tp <= 0:
        return None
    key = f"{sym}|{round(sp, 1)}|{round(tp, 1)}"
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        _stats["fetch_cache_hit"] += 1
        return hit[1]
    out: Optional[Dict[str, Any]] = None
    ttl = float(config.TIMESFM_FAIL_TTL_S)
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{config.TIMESFM_URL}/features/{sym}", params={"stop_pct": sp, "target_pct": tp, "cached_only": "true"},
                                 timeout=float(config.TIMESFM_TIMEOUT_S))
        if r.status_code == 200:
            j = r.json()
            if isinstance(j, dict) and j.get("symbol"):
                out, ttl = j, float(config.TIMESFM_CACHE_TTL_S)
    except Exception as e:  # noqa: BLE001
        logger.debug("timesfm fetch %s failed (no opinion): %s", sym, e)
    _stats["fetch_ok" if out is not None else "fetch_fail"] += 1
    _cache[key] = (now + ttl, out)
    if len(_cache) > 4000:
        for k in [k for k, v in _cache.items() if v[0] <= now][:2000]:
            _cache.pop(k, None)
    return out
