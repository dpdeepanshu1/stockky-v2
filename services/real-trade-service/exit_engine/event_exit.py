"""exit_engine/event_exit.py - a NEGATIVE typed event on a stock we HOLD (E274 round 45; plan: "S4 is the exit trigger for a held stock").

Real Trade is long only, so S4 (negative event) is never an entry here; it is the reason to get out of, or protect, a position that is already open. Every exit cycle each
open position is checked against the typed event feed (watchlist_engine.intraday_news.negative_event: today's market-hours snapshot, or the after-hours / pre-open veto row
for a position opened before today). This module only DECIDES; the exit loop applies it (exit_engine/exit.py, step 3c). Pure, never raises.

  * only a FRESH event that is news to the position counts and only when its announce-time strength is at least EVENT_EXIT_MIN_STRENGTH. Unknown polarity never counts.
  * never on the opening print: before EVENT_EXIT_START_IST (09:20) the verdict is hold.
  * STRONG (strength >= EVENT_EXIT_STRONG_STRENGTH) -> exit, whatever the P&L; with mode `tighten` it tightens instead.
  * WEAK -> tighten: stop to just under the last price (ltp - EVENT_EXIT_TIGHT_ATR x ATR, 0.5 % of the price when the ATR is unknown), rounded DOWN to the tick, at least one
    tick under the price. Ratchet only: a stop is never lowered.

EVENT_EXIT_MODE: off | tighten | exit (default exit).
"""
from __future__ import annotations

import math
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, Optional

import config

MODES = ("off", "tighten", "exit")
_stats: Dict[str, Any] = {"evaluated": 0, "no_event": 0, "too_early": 0, "hold": 0, "tightened": 0, "exited": 0, "errors": 0}
_ring: Deque[Dict[str, Any]] = deque(maxlen=60)


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _cfg(name: str, default: float) -> float:
    v = _f(getattr(config, name, default))
    return default if v is None else v


def mode() -> str:
    m = str(getattr(config, "EVENT_EXIT_MODE", "exit") or "exit").strip().lower()
    return m if m in MODES else "exit"


def _minutes(hhmm: Any, default: int) -> int:
    try:
        h, m = str(hhmm).split(":")
        return int(h) * 60 + int(m)
    except Exception:  # noqa: BLE001
        return default


def evaluate(event: Any, *, entry_price: Any = None, ltp: Any, current_stop: Any, atr: Any, now_ist: Any, tick: float = 0.05, mode_: Optional[str] = None) -> Dict[str, Any]:
    """{"action": "hold"|"tighten"|"exit", "new_stop", "reasons", "strength", "type"}. `event` = {"polarity": -1, "type", "strength", ...} or None."""
    out: Dict[str, Any] = {"action": "hold", "new_stop": None, "reasons": [], "strength": None, "type": None}
    try:
        _stats["evaluated"] += 1
        m = mode_ or mode()
        if m == "off":
            out["reasons"].append("mode off")
            return out
        if not isinstance(event, dict) or event.get("polarity") != -1:
            _stats["no_event"] += 1
            out["reasons"].append("no negative event")
            return out
        s = _f(event.get("strength")) or 0.0
        out["strength"], out["type"] = round(s, 3), str(event.get("type") or "news")
        if s < _cfg("EVENT_EXIT_MIN_STRENGTH", 0.40):
            _stats["hold"] += 1
            out["reasons"].append(f"strength {s:.2f} below the minimum")
            return out
        minute = now_ist.hour * 60 + now_ist.minute if isinstance(now_ist, datetime) else None
        if minute is None or minute < _minutes(getattr(config, "EVENT_EXIT_START_IST", "09:20"), 9 * 60 + 20):
            _stats["too_early"] += 1
            out["reasons"].append("before the opening print has settled")
            return out
        px, stop, tk = _f(ltp), _f(current_stop), (_f(tick) or 0.05)
        if not px or px <= 0:
            out["reasons"].append("no price")
            return out
        strong = s >= _cfg("EVENT_EXIT_STRONG_STRENGTH", 0.60)
        if strong and m == "exit":
            _stats["exited"] += 1
            out["action"] = "exit"
            out["reasons"].append(f"negative {out['type']} event, strength {s:.2f} (strong): sell")
            return out
        a = _f(atr)
        dist = _cfg("EVENT_EXIT_TIGHT_ATR", 0.25) * a if a and a > 0 else 0.005 * px
        cand = math.floor((px - dist) / tk + 1e-9) * tk
        cand = round(min(cand, px - tk), 4)
        if cand <= 0 or (stop is not None and cand <= stop + 1e-9):
            _stats["hold"] += 1
            out["reasons"].append("the stop is already as tight")
            return out
        _stats["tightened"] += 1
        out["action"], out["new_stop"] = "tighten", cand
        out["reasons"].append(f"negative {out['type']} event, strength {s:.2f} ({'strong, tighten mode' if strong else 'weak'}): stop to just under the price")
        return out
    except Exception:  # noqa: BLE001
        _stats["errors"] += 1
        return {"action": "hold", "new_stop": None, "reasons": ["error"], "strength": None, "type": None}


def limit_price_for(ltp: Any, event: Any, tick: float = 0.05) -> Optional[float]:
    """Marketable LIMIT sell price for an exit caused by AFTER-HOURS news (ltp minus EVENT_EXIT_LIMIT_BUFFER_PCT, rounded DOWN to the tick), else None (= MARKET). Never raises."""
    try:
        if not getattr(config, "EVENT_EXIT_LIMIT_ENABLED", True) or not isinstance(event, dict) or event.get("source") != "afterhours":
            return None
        px, tk = _f(ltp), (_f(tick) or 0.05)
        if not px or px <= 0:
            return None
        p = math.floor(px * (1.0 - _cfg("EVENT_EXIT_LIMIT_BUFFER_PCT", 0.5) / 100.0) / tk + 1e-9) * tk
        return round(p, 4) if p > 0 else None
    except Exception:  # noqa: BLE001
        return None


def record(symbol: str, verdict: Dict[str, Any], applied: bool, ltp: Any) -> None:
    try:
        _ring.append({"symbol": symbol, "action": verdict.get("action"), "new_stop": verdict.get("new_stop"), "strength": verdict.get("strength"),
                      "type": verdict.get("type"), "applied": bool(applied), "ltp": ltp, "reasons": list(verdict.get("reasons") or [])[:3]})
    except Exception:  # noqa: BLE001
        pass


def stats() -> Dict[str, Any]:
    return {"mode": mode(), **dict(_stats), "recent": list(_ring)[-20:]}


def reset() -> None:
    _ring.clear()
    for k in _stats:
        _stats[k] = 0
