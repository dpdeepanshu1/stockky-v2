"""entry_engine/regime_rules.py - rules that switch by market regime. LIVE since E273 round 7 (was log only, 2026-10-23).

WHY: the market regime used to be only a mood label on the chart and a score on the dashboard (the old regime gate, ENTRY_REGIME_GATE_ENABLED, is off since
group 332). Every candidate still gets one verdict (greppable `REGIME_RULES` log line + a ring for GET /regime-rules/status), and since E273 round 7 that verdict
acts as a SOFT PENALTY: a candidate that passes today's live floors (ENTRY_MIN_REWARD_RISK, CANDIDATE_MIN_CONVICTION) but misses the stricter floors of a weak
market is SIZED DOWN, in proportion to how far it misses; it is refused only when it misses by a lot (a stricter table than the default is needed for that).
A candidate already under a live floor is left to the old gates and their exemptions (upper circuit etc.). Neutral and strong markets never change a size.

REGIME of a cycle (the market score the entry engine already reads, 0-100, and the adaptive gate threshold):
    weak     score < gate                      (the same line the old regime gate used)
    strong   score >= REGIME_RULES_STRONG_SCORE (default 60)
    neutral  everything in between
RULES per regime (REGIME_RULES_TABLE_JSON overrides): min structure R:R, min conviction, and a risk scale (the size multiple at the full cut). A candidate
"misses" when its R:R or conviction is under that regime's minimum. Missing score / R:R / conviction = no opinion on that test. Pure, never raises.

PENALTY (penalty()): depth = the worse of the two shortfalls, each as a fraction of the regime floor, counted only when the value is at or above the live floor.
    mult = 1 - (1 - risk_scale) * min(1, depth / REGIME_RULES_FULL_DEPTH)       (REGIME_RULES_FULL_DEPTH default 0.20)
    skip = depth >= REGIME_RULES_SKIP_DEPTH (default 0.60) or mult <= 0
The worse test decides, never a product of both. The mult is never above 1. REGIME_RULES_MODE is live (default) | off; there is no log or record-only mode."""
from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

import config

logger = logging.getLogger("real-trade-regime-rules")

REGIMES = ("weak", "neutral", "strong")
# min_rr / min_conviction None = "same as the live rule" (ENTRY_MIN_REWARD_RISK / CANDIDATE_MIN_CONVICTION). risk_scale is advisory only.
DEFAULT_TABLE: Dict[str, Dict[str, Optional[float]]] = {
    "weak": {"min_rr": 2.5, "min_conviction": 65.0, "risk_scale": 0.5},
    "neutral": {"min_rr": None, "min_conviction": None, "risk_scale": 1.0},
    "strong": {"min_rr": None, "min_conviction": None, "risk_scale": 1.0},
}
_stats: Dict[str, Any] = {}
_recent: "Deque[Dict[str, Any]]" = deque(maxlen=100)


def _f(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def mode_for(trade_mode: str = "REAL") -> str:
    """live | off. Anything unrecognised (log and enforce included) reads as live: there is no record-only mode. DEMO follows REAL."""
    try:
        return "off" if str(getattr(config, "REGIME_RULES_MODE", "live")).strip().lower() == "off" else "live"
    except Exception:  # noqa: BLE001
        return "off"


def regime_of(score: Any, gate: Any) -> Optional[str]:
    """weak | neutral | strong, or None when the score is unknown."""
    s, g = _f(score), _f(gate)
    if s is None:
        return None
    if g is not None and s < g:
        return "weak"
    strong = _f(getattr(config, "REGIME_RULES_STRONG_SCORE", 60.0))
    return "strong" if strong is not None and s >= strong else "neutral"


_warned_override: set = set()


def table() -> Dict[str, Dict[str, Optional[float]]]:
    """DEFAULT_TABLE with REGIME_RULES_TABLE_JSON merged over it (a malformed override is ignored, field by field)."""
    out = {k: dict(v) for k, v in DEFAULT_TABLE.items()}
    try:
        raw = getattr(config, "REGIME_RULES_TABLE_JSON", "") or ""
        over = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw if isinstance(raw, dict) else {})
        for reg, vals in (over.items() if isinstance(over, dict) else []):
            if reg in out and isinstance(vals, dict):
                for k in ("min_rr", "min_conviction", "risk_scale"):
                    if k in vals and (vals[k] is None or _f(vals[k]) is not None):
                        out[reg][k] = None if vals[k] is None else float(vals[k])
    except Exception as e:  # noqa: BLE001
        # E148 (F-17): still ignored (the default table stays in force), but no longer silent; one warning per distinct bad value.
        key = str(getattr(config, "REGIME_RULES_TABLE_JSON", ""))[:200]
        if key not in _warned_override:
            _warned_override.add(key)
            logger.warning("regime_rules: REGIME_RULES_TABLE_JSON is not usable (%s: %s) - the default table is in force", type(e).__name__, e)
    return out


def evaluate(score: Any, gate: Any, conviction: Any, rr: Any) -> Dict[str, Any]:
    """Hypothetical verdict. Keys: regime, opinion ('ok'|'no_opinion'), would_reject, reasons[], min_rr, min_conviction, risk_scale, score, gate. Never raises."""
    out: Dict[str, Any] = {"regime": None, "opinion": "no_opinion", "would_reject": False, "reasons": [], "min_rr": None, "min_conviction": None,
                           "risk_scale": None, "score": _f(score), "gate": _f(gate), "conviction": _f(conviction), "rr": _f(rr)}
    try:
        reg = regime_of(score, gate)
        if reg is None:
            return out
        rules = table()[reg]
        min_rr = _f(rules.get("min_rr")) if rules.get("min_rr") is not None else _f(getattr(config, "ENTRY_MIN_REWARD_RISK", None))
        min_cv = _f(rules.get("min_conviction")) if rules.get("min_conviction") is not None else _f(getattr(config, "CANDIDATE_MIN_CONVICTION", None))
        out.update(regime=reg, opinion="ok", min_rr=min_rr, min_conviction=min_cv, risk_scale=_f(rules.get("risk_scale")))
        reasons = []
        if min_rr is not None and out["rr"] is not None and out["rr"] < min_rr:
            reasons.append(f"R:R {out['rr']:.2f} under the {reg}-market floor {min_rr:g}")
        if min_cv is not None and out["conviction"] is not None and out["conviction"] < min_cv:
            reasons.append(f"conviction {out['conviction']:.0f} under the {reg}-market floor {min_cv:g}")
        out["reasons"], out["would_reject"] = reasons, bool(reasons)
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("regime rules evaluate failed (no opinion): %s", e)
        return {**out, "regime": None, "opinion": "no_opinion", "would_reject": False, "reasons": []}


def _depth_cfg(name: str, default: float) -> float:
    """A positive finite number from config, else the default (junk, zero and negatives read as the default)."""
    x = _f(getattr(config, name, None))
    return x if x is not None and x > 0 else default


def _no_penalty() -> Dict[str, Any]:
    return {"mult": 1.0, "depth": 0.0, "skip": False, "reason": ""}


def penalty(verdict: Dict[str, Any]) -> Dict[str, Any]:
    """The soft penalty for one evaluate() verdict: {mult, depth, skip, reason}. mult in [0, 1], never above 1. Never raises.

    Only a test the candidate FAILS against the regime floor but PASSES against the live floor counts; a value under the live floor belongs to the old gates."""
    try:
        if not isinstance(verdict, dict) or verdict.get("opinion") != "ok" or not verdict.get("would_reject"):
            return _no_penalty()
        live = {"rr": _f(getattr(config, "ENTRY_MIN_REWARD_RISK", None)), "conviction": _f(getattr(config, "CANDIDATE_MIN_CONVICTION", None))}
        depth, reasons = 0.0, []
        for key, floor_key, label in (("rr", "min_rr", "R:R"), ("conviction", "min_conviction", "conviction")):
            val, floor = _f(verdict.get(key)), _f(verdict.get(floor_key))
            if val is None or floor is None or floor <= 0 or val >= floor:
                continue
            if live[key] is not None and val < live[key]:
                continue                                              # under the live floor already: the old gates decide
            d = (floor - val) / floor
            reasons.append(f"{label} {val:.2f} under the {verdict.get('regime') or 'regime'}-market floor {floor:g}" if key == "rr" else
                           f"{label} {val:.0f} under the {verdict.get('regime') or 'regime'}-market floor {floor:g}")
            depth = max(depth, d)
        if depth <= 0:
            return _no_penalty()
        scale = _f(verdict.get("risk_scale"))
        scale = 1.0 if scale is None else min(1.0, max(0.0, scale))
        full = _depth_cfg("REGIME_RULES_FULL_DEPTH", 0.20)
        mult = max(0.0, min(1.0, 1.0 - (1.0 - scale) * min(1.0, depth / full)))
        skip = depth >= _depth_cfg("REGIME_RULES_SKIP_DEPTH", 0.60) or mult <= 0.0
        return {"mult": 0.0 if skip else round(mult, 4), "depth": round(depth, 4), "skip": bool(skip), "reason": "; ".join(reasons)}
    except Exception as e:  # noqa: BLE001
        logger.debug("regime rules penalty failed (no penalty): %s", e)
        return _no_penalty()


def reset_stats() -> None:
    _stats.clear()
    _stats.update(evaluated=0, ok=0, no_opinion=0, would_reject=0, penalised=0, skipped=0, errors=0, by_regime={}, since=time.time())
    _recent.clear()


reset_stats()


def record(symbol: str, trade_mode: str, verdict: Dict[str, Any], price: Any = None) -> None:
    """Count one candidate, keep it in the ring and write one greppable line (the evidence for the validation tools; carries the penalty when the caller set it). Never raises."""
    try:
        _stats["evaluated"] += 1
        _stats["ok" if verdict.get("opinion") == "ok" else "no_opinion"] += 1
        reg = verdict.get("regime") or "unknown"
        b = _stats["by_regime"].setdefault(reg, {"n": 0, "would_reject": 0})
        b["n"] += 1
        if verdict.get("would_reject"):
            _stats["would_reject"] += 1
            b["would_reject"] += 1
        pm = _f(verdict.get("penalty_mult"))
        if verdict.get("skipped"):
            _stats["skipped"] += 1
        elif pm is not None and pm < 1.0:
            _stats["penalised"] += 1
        row = {"ts": time.time(), "symbol": str(symbol or "").upper(), "mode": str(trade_mode or "").upper(), "regime": verdict.get("regime"),
               "would_reject": bool(verdict.get("would_reject")), "reasons": list(verdict.get("reasons") or []), "score": verdict.get("score"),
               "gate": verdict.get("gate"), "conviction": verdict.get("conviction"), "rr": verdict.get("rr"), "min_rr": verdict.get("min_rr"),
               "min_conviction": verdict.get("min_conviction"), "risk_scale": verdict.get("risk_scale"), "penalty_mult": pm, "skipped": bool(verdict.get("skipped")),
               "price": _f(price)}
        _recent.append(row)
        logger.info("REGIME_RULES %s", json.dumps(row, separators=(",", ":"), default=str))
    except Exception:  # noqa: BLE001
        _stats["errors"] = _stats.get("errors", 0) + 1


def stats() -> Dict[str, Any]:
    try:
        cfg = {k: getattr(config, k, None) for k in ("REGIME_RULES_MODE", "REGIME_RULES_STRONG_SCORE", "ENTRY_REGIME_GATE_ENABLED", "ENTRY_MIN_REWARD_RISK",
                                                     "CANDIDATE_MIN_CONVICTION", "REGIME_RULES_FULL_DEPTH", "REGIME_RULES_SKIP_DEPTH")}
        return {**{k: v for k, v in _stats.items() if k != "since"}, "since_epoch": _stats.get("since"), "config": cfg, "table": table(),
                "recent": list(reversed(list(_recent)))[:30]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}
