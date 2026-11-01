"""orders/event_weights.py - E285 round 113: the weight of each POSITIVE news event type is nudged by what trades on that type actually did (plan item: learned event weights).

The event feed (analysis-intelligence `TYPE_WEIGHT`: regulatory 0.9, results 0.85, deal 0.7, order 0.65, rating 0.45, management 0.4, other 0.2) is a fixed table. Round 106 stamped the typed
event into every entry and reports each type's follow-through (orders/event_stats). This turns that report into a multiplier on the strength the entry side reads:

  learned multiplier  = 1 + clip(avg R of the type's closed trades, -0.5, +0.5)          (a type averaging +0.3 R reads 1.3; one averaging -0.4 R reads 0.6)
  used multiplier     = adaptive_params.learned_value(learned, 1.0, r_n, prior 30, min_n 30), held inside [EVENT_WEIGHT_MIN_MULT (0.6), EVENT_WEIGHT_MAX_MULT (1.2)] (a hard bound)
So a type has NO effect until it has 30 closed trades with a readable R (the constant 1.0), then the move is shrunk with w = n / (n + 30).

ONLY POSITIVE (UP) EVENTS ARE TOUCHED: a negative event's strength drives the NEG_EVENT veto and the short setups, and learning must never weaken a veto. The new strength is
min(1, strength x multiplier). The age decay (one-hour half-life) is not touched here.

WHERE IT IS READ: screening/quality_gate._typed_event_from (the event the entry's quality signal, the gap-against-event check and the setup stamp see) and screening/premarket_plan (the morning
event candidates). Both are CACHE READS (`mult`): no database, no I/O. The cache is filled by slot_ladder._load (the closed-trade rows the exits and the size ladder already read, every
SLOT_LADDER_CACHE_S seconds) through `update(rows)`. Nothing is learned until a trade-reading call has run in this process; until then every multiplier is 1.0.
Switches EVENT_WEIGHTS_LEARNED_ENABLED and the master ADAPTIVE_ENTRY_PARAMS. Never raises.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Iterable, Optional

import config
from orders import adaptive_params as _ap
from orders import event_stats

logger = logging.getLogger("position-stocks-event-weights")

PRIOR_TRADES = 30
MIN_TRADES = 30
R_CLIP = 0.5

_lock = threading.Lock()
_weights: Dict[str, Dict[str, Any]] = {}


def enabled() -> bool:
    return bool(getattr(config, "EVENT_WEIGHTS_LEARNED_ENABLED", True)) and bool(getattr(config, "ADAPTIVE_ENTRY_PARAMS", True))


def _cfg_f(name: str, default: float) -> float:
    try:
        v = float(getattr(config, name, default))
        return v if v == v and v > 0 else default
    except (TypeError, ValueError):
        return default


def reset() -> None:
    with _lock:
        _weights.clear()


def compute(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """{"<type>": {"mult", "source", "weight", "n", "avg_r", "verdict"}} for every positive event type with at least one trade, from setup-stamped closed trades. Pure, never raises."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        lo, hi = _cfg_f("EVENT_WEIGHT_MIN_MULT", 0.6), _cfg_f("EVENT_WEIGHT_MAX_MULT", 1.2)
        lo, hi = min(lo, 1.0), max(hi, 1.0)
        for key, c in event_stats.summarize(rows).items():
            etype, _, side = key.partition("|")
            if side != "UP" or not etype:
                continue
            avg_r, r_n = c.get("avg_r"), int(c.get("r_n") or 0)
            learned = None if avg_r is None else 1.0 + max(-R_CLIP, min(R_CLIP, float(avg_r)))
            v = _ap.learned_value(learned, 1.0, r_n, prior=PRIOR_TRADES, min_n=MIN_TRADES, ceiling=hi, floor=lo)
            out[etype] = {"mult": round(float(v["value"]), 4), "source": v["source"], "weight": v["weight"], "n": r_n, "avg_r": avg_r, "verdict": c.get("verdict")}
    except Exception as e:  # noqa: BLE001
        logger.debug("event weights: compute failed: %s", e)
        return {}
    return out


def update(rows: Iterable[Dict[str, Any]]) -> int:
    """Recompute from the closed-trade rows and replace the cache (an unreadable read keeps the old one). Returns how many types are in the cache. Never raises."""
    try:
        new = compute(rows)
        with _lock:
            _weights.clear()
            _weights.update(new)
            return len(_weights)
    except Exception as e:  # noqa: BLE001
        logger.debug("event weights: update failed (old weights kept): %s", e)
        return len(_weights)


def mult(etype: Any, polarity: Any) -> float:
    """The multiplier for a typed event: 1.0 for a negative / unknown event, an unknown type, a type under 30 trades, or when switched off. A cache read. Never raises."""
    try:
        if not enabled() or isinstance(polarity, bool) or polarity != 1:
            return 1.0
        w = _weights.get(str(etype or "").strip().lower())
        return float(w["mult"]) if w and w.get("source") != "constant" else 1.0
    except Exception:  # noqa: BLE001
        return 1.0


def apply(etype: Any, polarity: Any, strength: Any) -> Optional[float]:
    """The strength the entry side should read: min(1, strength x multiplier) for a positive event, the strength unchanged otherwise. Never raises."""
    try:
        m = mult(etype, polarity)
        return strength if m == 1.0 else round(max(0.0, min(1.0, float(strength) * m)), 4)
    except Exception:  # noqa: BLE001
        return strength


def snapshot() -> Dict[str, Any]:
    """For /setup/stats: whether it is on and what each positive type reads now. Never raises."""
    try:
        with _lock:
            return {"enabled": enabled(), "types": {k: dict(v) for k, v in sorted(_weights.items())}}
    except Exception:  # noqa: BLE001
        return {"enabled": False, "types": {}}
