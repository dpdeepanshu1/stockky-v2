"""entry_engine/regime_rules.py - rules that switch by market regime (2026-10-23). LOG ONLY: there is no enforce mode.

WHY: today the market regime is only a mood label on the chart and a score on the dashboard (the old regime gate, ENTRY_REGIME_GATE_ENABLED, is off since
group 332). This module asks the question the other way round: "if the entry rules were stricter in a weak market and unchanged in a normal / strong one, which of
today's candidates would have been turned away, and what would they have done next?" Every candidate gets one hypothetical verdict (greppable `REGIME_RULES`
log line + a ring for GET /regime-rules/status); NOTHING it says can make an entry WAIT, change a size, a stop or a target. Promote it only after the validation
tools (gate_check / calibration pack) show an edge out of sample - that is a code change on purpose, not a setting.

REGIME of a cycle (the market score the entry engine already reads, 0-100, and the adaptive gate threshold):
    weak     score < gate                      (the same line the old regime gate used)
    strong   score >= REGIME_RULES_STRONG_SCORE (default 60)
    neutral  everything in between
RULES per regime (hypotheses, REGIME_RULES_TABLE_JSON overrides): min structure R:R, min conviction, and an advisory risk scale. A candidate "would be turned away" when its
R:R or conviction is under that regime's minimum. Missing score / R:R / conviction = no opinion on that test. Pure, never raises."""
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
    """off | log. Anything unrecognised (enforce included) is log-or-off: there is no enforce mode. DEMO follows REAL."""
    try:
        return "off" if str(getattr(config, "REGIME_RULES_MODE", "log")).strip().lower() == "off" else "log"
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


def reset_stats() -> None:
    _stats.clear()
    _stats.update(evaluated=0, ok=0, no_opinion=0, would_reject=0, errors=0, by_regime={}, since=time.time())
    _recent.clear()


reset_stats()


def record(symbol: str, trade_mode: str, verdict: Dict[str, Any], price: Any = None) -> None:
    """Count one candidate, keep it in the ring and write one greppable line (the evidence for the validation tools). Never raises."""
    try:
        _stats["evaluated"] += 1
        _stats["ok" if verdict.get("opinion") == "ok" else "no_opinion"] += 1
        reg = verdict.get("regime") or "unknown"
        b = _stats["by_regime"].setdefault(reg, {"n": 0, "would_reject": 0})
        b["n"] += 1
        if verdict.get("would_reject"):
            _stats["would_reject"] += 1
            b["would_reject"] += 1
        row = {"ts": time.time(), "symbol": str(symbol or "").upper(), "mode": str(trade_mode or "").upper(), "regime": verdict.get("regime"),
               "would_reject": bool(verdict.get("would_reject")), "reasons": list(verdict.get("reasons") or []), "score": verdict.get("score"),
               "gate": verdict.get("gate"), "conviction": verdict.get("conviction"), "rr": verdict.get("rr"), "min_rr": verdict.get("min_rr"),
               "min_conviction": verdict.get("min_conviction"), "risk_scale": verdict.get("risk_scale"), "price": _f(price)}
        _recent.append(row)
        logger.info("REGIME_RULES %s", json.dumps(row, separators=(",", ":"), default=str))
    except Exception:  # noqa: BLE001
        _stats["errors"] = _stats.get("errors", 0) + 1


def stats() -> Dict[str, Any]:
    try:
        cfg = {k: getattr(config, k, None) for k in ("REGIME_RULES_MODE", "REGIME_RULES_STRONG_SCORE", "ENTRY_REGIME_GATE_ENABLED", "ENTRY_MIN_REWARD_RISK",
                                                     "CANDIDATE_MIN_CONVICTION")}
        return {**{k: v for k, v in _stats.items() if k != "since"}, "since_epoch": _stats.get("since"), "config": cfg, "table": table(),
                "recent": list(reversed(list(_recent)))[:30]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}
