"""entry_engine/kronos_gate.py - Kronos forecast gate on the entry path (group 425).

QUESTION IT ANSWERS: "does a sampled forecast of the next few daily bars leave any edge after round-trip costs?" The kronos-forecast-service worker
returns statistics of N independently sampled paths (p_up, quantiles of the final return, P(target before stop), ev_pct); this module turns
them into a verdict.

MODES (config.KRONOS_GATE_MODE / KRONOS_GATE_DEMO_MODE): off (default, nothing is fetched) | log (fetch + count + store, never blocks, never
changes a target) | enforce (a rejection is a WAIT with gate_tag="kronos"; Gate 5.6 may use the forecast EV as its edge).

NO OPINION: a worker that is down, slow, or returns garbage never blocks and never helps. Every public function here is safe to call: evaluate()
and the helpers never raise; the async fetch returns None on any failure and caches the failure for KRONOS_FAIL_TTL_S.
Every threshold is a hypothesis until scripts/kronos_backtest.py has been run on history.
"""
from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple

import config

logger = logging.getLogger("real-trade-kronos-gate")

TAG = "kronos"
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
    """off | log | enforce for REAL / DEMO. Anything unrecognised is off."""
    try:
        m = config.KRONOS_GATE_MODE if str(trade_mode).upper() == "REAL" else config.KRONOS_GATE_DEMO_MODE
        return m if m in ("log", "enforce") else "off"
    except Exception:  # noqa: BLE001
        return "off"


def cost_pct_for(entry_price) -> float:
    """Round-trip cost as a % of trade value, from the cost model at a nominal trade size (the gate runs before sizing). Falls back to 0.35."""
    try:
        px = _f(entry_price)
        if px is None or px <= 0:
            return 0.35
        import cost_model
        qty = max(1, int(config.KRONOS_COST_NOTIONAL // px))
        est = cost_model.estimate_round_trip_cost(px, qty, exit_price=px)
        pct = est.total / (px * qty) * 100.0
        return round(pct, 4) if math.isfinite(pct) and pct > 0 else 0.35
    except Exception:  # noqa: BLE001
        return 0.35


def evaluate(forecast: Optional[Dict[str, Any]], cost_pct: float) -> Dict[str, Any]:
    """Pure verdict. Keys: opinion ('ok'|'no_opinion'), would_reject, reasons[], tag, reason, ev_pct, p_target_first, no_data. Never raises."""
    base: Dict[str, Any] = {"opinion": "no_opinion", "would_reject": False, "reasons": [], "tag": None, "reason": None,
                            "ev_pct": None, "p_target_first": None, "cost_pct": _f(cost_pct), "no_data": None}
    try:
        if not isinstance(forecast, dict):
            base["no_data"] = "no forecast"
            return base
        ev, pt = _f(forecast.get("ev_pct")), _f(forecast.get("p_target_first"))
        n = _f(forecast.get("n_paths"))
        if ev is None or pt is None or n is None or n < 2:
            base["no_data"] = "incomplete forecast"
            return base
        base.update(opinion="ok", ev_pct=ev, p_target_first=pt)
        cost = _f(cost_pct) or 0.0
        reasons = []
        k = float(config.KRONOS_MIN_EDGE_X_COST or 0.0)
        if k > 0 and ev < k * cost:
            reasons.append(f"forecast expected move {ev:+.2f}% does not beat {k:g}x round-trip cost ({cost:.2f}%)")
        lim = float(config.KRONOS_MIN_P_TARGET or 0.0)
        if lim > 0 and pt < lim:
            reasons.append(f"only {pt * 100:.0f}% of {int(n)} forecast paths reach the target before the stop (min {lim * 100:.0f}%)")
        base["reasons"] = reasons
        if reasons:
            base.update(would_reject=True, tag=TAG, reason="KRONOS: " + "; ".join(reasons))
        return base
    except Exception as e:  # noqa: BLE001
        logger.debug("kronos evaluate failed (no opinion): %s", e)
        base.update(opinion="no_opinion", would_reject=False, reasons=[], tag=None, reason=None, no_data="evaluation error")
        return base


def effective_target_pct(fixed_target_pct, verdict: Optional[Dict[str, Any]], gate_mode: str) -> float:
    """Gate 5.6 edge: min(fixed target, forecast EV) in enforce mode when the forecast has an opinion and a positive EV; otherwise the fixed
    target, unchanged (log / off / no opinion never alter the cost check). Never raises, never returns more than the fixed target."""
    try:
        fixed = float(fixed_target_pct)
        if gate_mode != "enforce" or not isinstance(verdict, dict) or verdict.get("opinion") != "ok":
            return fixed
        ev = _f(verdict.get("ev_pct"))
        if ev is None or ev <= 0:
            return fixed          # a non-positive EV is Gate 4f's job; the cost gate must not be fed a zero edge by a missing opinion
        return min(fixed, ev)
    except Exception:  # noqa: BLE001
        return fixed_target_pct


def snapshot(forecast: Optional[Dict[str, Any]], verdict: Dict[str, Any], gate_mode: str, enforced: bool) -> str:
    """Compact JSON for logs / evidence."""
    f = forecast if isinstance(forecast, dict) else {}
    keep = ("symbol", "last_bar", "n_paths", "horizon", "p_up", "q10_ret_pct", "q50_ret_pct", "q90_ret_pct", "mean_ret_pct",
            "p_target_first", "p_stop_first", "ev_pct", "stop_pct", "target_pct", "cached")
    doc = {"v": 1, "gate_mode": gate_mode, "enforced": bool(enforced), "opinion": verdict.get("opinion"),
           "would_reject": bool(verdict.get("would_reject")), "reasons": verdict.get("reasons") or [], "no_data": verdict.get("no_data"),
           "cost_pct": verdict.get("cost_pct"), "forecast": {k: f.get(k) for k in keep if f.get(k) is not None}}
    try:
        return json.dumps(doc, separators=(",", ":"), default=str)[:3000]
    except Exception:  # noqa: BLE001
        return "{}"


def reset_stats() -> None:
    _stats.clear()
    _stats.update(evaluated=0, ok=0, no_opinion=0, would_reject=0, enforced=0, fetch_ok=0, fetch_fail=0, fetch_cache_hit=0, errors=0,
                  since=time.time())
    _recent.clear()


reset_stats()


def reset() -> None:
    _cache.clear()
    reset_stats()


def record(symbol: str, trade_mode: str, verdict: Dict[str, Any], gate_mode: str, enforced: bool, price: Any = None) -> None:
    """Count one evaluated candidate, keep it in the recent ring and write one greppable log line (this is the evidence for the log mode). Never raises."""
    try:
        _stats["evaluated"] += 1
        _stats["ok" if verdict.get("opinion") == "ok" else "no_opinion"] += 1
        if verdict.get("would_reject"):
            _stats["would_reject"] += 1
        if enforced:
            _stats["enforced"] += 1
        row = {"ts": time.time(), "symbol": str(symbol or "").upper(), "mode": str(trade_mode or "").upper(), "gate_mode": gate_mode,
               "opinion": verdict.get("opinion"), "would_reject": bool(verdict.get("would_reject")), "enforced": bool(enforced),
               "ev_pct": verdict.get("ev_pct"), "p_target_first": verdict.get("p_target_first"), "cost_pct": verdict.get("cost_pct"),
               "reasons": list(verdict.get("reasons") or []), "no_data": verdict.get("no_data"), "price": _f(price)}
        _recent.append(row)
        logger.info("KRONOS_GATE %s", json.dumps(row, separators=(",", ":"), default=str))
    except Exception:  # noqa: BLE001
        _stats["errors"] = _stats.get("errors", 0) + 1


def stats() -> Dict[str, Any]:
    try:
        cfg = {k: getattr(config, k, None) for k in ("KRONOS_GATE_MODE", "KRONOS_GATE_DEMO_MODE", "KRONOS_URL", "KRONOS_TIMEOUT_S", "KRONOS_CACHE_TTL_S",
                                                     "KRONOS_HORIZON_BARS", "KRONOS_PATHS", "KRONOS_MIN_EDGE_X_COST", "KRONOS_MIN_P_TARGET")}
        return {**{k: v for k, v in _stats.items() if k != "since"}, "since_epoch": _stats.get("since"), "config": cfg,
                "recent": list(reversed(list(_recent)))[:30]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}


async def fetch_forecast(symbol: str, ref_price, stop_pct, target_pct) -> Optional[Dict[str, Any]]:
    """Forecast statistics from the worker; None on any failure (the failure is cached KRONOS_FAIL_TTL_S). Cached per symbol KRONOS_CACHE_TTL_S."""
    sym = str(symbol or "").strip().upper()
    px, sp, tp = _f(ref_price), _f(stop_pct), _f(target_pct)
    if not sym or px is None or px <= 0 or sp is None or sp <= 0 or tp is None or tp <= 0:
        return None
    now = time.monotonic()
    hit = _cache.get(sym)
    if hit is not None and hit[0] > now:
        _stats["fetch_cache_hit"] += 1
        return hit[1]
    out: Optional[Dict[str, Any]] = None
    ttl = float(config.KRONOS_FAIL_TTL_S)
    try:
        import httpx
        body = {"symbol": sym, "ref_price": px, "stop_pct": sp, "target_pct": tp,
                "horizon": int(config.KRONOS_HORIZON_BARS), "n_paths": int(config.KRONOS_PATHS)}
        async with httpx.AsyncClient() as client:
            r = await client.post(f"{config.KRONOS_URL}/forecast", json=body, timeout=float(config.KRONOS_TIMEOUT_S))
        if r.status_code == 200:
            j = r.json()
            if isinstance(j, dict) and j.get("ev_pct") is not None:
                out, ttl = j, float(config.KRONOS_CACHE_TTL_S)
    except Exception as e:  # noqa: BLE001
        logger.debug("kronos fetch %s failed (no opinion): %s", sym, e)
    _stats["fetch_ok" if out is not None else "fetch_fail"] += 1
    _cache[sym] = (now + ttl, out)
    if len(_cache) > 2000:
        for k in [k for k, v in _cache.items() if v[0] <= now][:1000]:
            _cache.pop(k, None)
    return out
