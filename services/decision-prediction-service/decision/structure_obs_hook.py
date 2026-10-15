"""group374: builds the all-decision structure observation that the decision service posts to the training service (training/structure_obs.py).

Pure and stdlib only. `build_payload(symbol, response, now)` returns None when there is nothing worth logging (no structure block, structure
unavailable, no final decision) so a missing structure never becomes an invented row. `should_send(key)` is a bounded in-process dedupe so a
bulk scan does not post the same (symbol, day, decision) hundreds of times; the server upsert is idempotent anyway. Never raises."""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

_IST = timezone(timedelta(hours=5, minutes=30))
_SEEN: Dict[str, bool] = {}
_LOCK = threading.Lock()
_MAX_SEEN = 5000


def enabled() -> bool:
    return (os.getenv("STRUCTURE_OBS_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or isinstance(v, bool):
            return None
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def build_payload(symbol: str, response: Any, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    try:
        if not isinstance(response, dict) or not symbol:
            return None
        st = response.get("structure")
        final = response.get("decision")
        if not isinstance(st, dict) or not isinstance(final, str) or not final:
            return None
        f = st.get("features") if isinstance(st.get("features"), dict) else {}
        if (f.get("quality") or "unavailable") not in ("ok", "limited"):
            return None          # no usable structure -> no row (unknown is never logged as a signal)
        now = (now or datetime.now(_IST)).astimezone(_IST)
        sl = st.get("structural_levels") if isinstance(st.get("structural_levels"), dict) else {}
        return {
            "symbol": str(symbol).upper(), "obs_date": now.date().isoformat(), "asof_date": f.get("asof"),
            "engine_version": f.get("ver") or "", "params_hash": f.get("hash"),
            "policy_mode": st.get("mode") or "full",
            "price": _num(response.get("close")) or _num(f.get("price_used")) or _num(f.get("ms_price")), "atr": _num(f.get("atr")),
            "final_decision": final, "legacy_decision": st.get("original_decision") or final,
            "policy_applied": st.get("applied") is True,
            "rules": [r.get("id") for r in (st.get("rules") if isinstance(st.get("rules"), list) else []) if isinstance(r, dict) and r.get("id")],
            "combined_score": _num(response.get("combined_score")), "technical_score": _num(response.get("technical_score")),
            "tqs": _num(f.get("tqs")), "groups": _num(f.get("groups")),
            "features": {k: f.get(k) for k in ("quality", "trend", "bos_dir", "bos_bars", "bos_failed", "choch_dir", "choch_bars", "chase_atr",
                                               "stretch_atr", "room_atr", "rr", "stop", "target", "loc_score", "tqs", "groups", "atr", "asof", "ver")
                         if f.get(k) is not None},
            "levels": {"stop": _num(sl.get("stop")), "target": _num(sl.get("target")), "rr": _num(sl.get("rr"))} if sl else
                      {"stop": _num(f.get("stop")), "target": _num(f.get("target")), "rr": _num(f.get("rr"))},
        }
    except Exception:  # noqa: BLE001
        return None


def should_send(payload: Dict[str, Any]) -> bool:
    """True the first time this (symbol, day, mode, decision, applied) is seen in this process; bounded memory."""
    try:
        key = "|".join(str(payload.get(k)) for k in ("symbol", "obs_date", "engine_version", "policy_mode", "final_decision", "policy_applied"))
        with _LOCK:
            if key in _SEEN:
                return False
            if len(_SEEN) >= _MAX_SEEN:
                _SEEN.clear()
            _SEEN[key] = True
        return True
    except Exception:  # noqa: BLE001
        return False


def _reset_for_tests() -> None:
    with _LOCK:
        _SEEN.clear()
