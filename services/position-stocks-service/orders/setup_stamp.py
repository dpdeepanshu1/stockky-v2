"""E272: the setup stamp - which rule took a trade, written into flow_json["setup"] (no database column).

The stamp holds the setup id, its trigger, the confirmations met, the soft penalties and the size multiplier. Until setup_engine exists (E273) every
entry is stamped LEGACY_SHORTLIST, which gives setup_stats a baseline to compare the new setups against. Pure: no I/O, never raises.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

LEGACY_ID = "LEGACY_SHORTLIST"
SLOTS = ("0920", "0930", "1000", "later")


def time_slot(now: Optional[datetime]) -> str:
    """09:20 / 09:30 / 10:00 / later, by the IST clock time of `now` (a naive datetime is read as IST). Unknown = later."""
    try:
        if now is None:
            return "later"
        m = now.hour * 60 + now.minute
        if m < 9 * 60 + 30:
            return "0920"
        if m < 10 * 60:
            return "0930"
        if m < 10 * 60 + 30:
            return "1000"
        return "later"
    except Exception:  # noqa: BLE001
        return "later"


def build(setup_id: str = LEGACY_ID, trigger: str = "", confirmations: Optional[List[str]] = None, penalties: Optional[Dict[str, float]] = None,
          size_mult: Optional[float] = None, now: Optional[datetime] = None, side: str = "LONG", event: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        pen = {str(k): round(float(v), 4) for k, v in (penalties or {}).items()}
    except (TypeError, ValueError):
        pen = {}
    try:
        sm = None if size_mult is None else round(float(size_mult), 4)
    except (TypeError, ValueError):
        sm = None
    st = {"id": str(setup_id or LEGACY_ID), "trigger": str(trigger or "")[:200], "confirmations": [str(c) for c in (confirmations or [])][:10],
          "penalties": pen, "size_mult": sm, "slot": time_slot(now), "side": str(side or "LONG").upper()}
    ev = event_part(event)
    if ev:
        st["event"] = ev                # E285 round 106: the typed event the entry saw (type, polarity, strength, age) - event_stats learns per type from it
    return st


def event_part(event: Any) -> Optional[Dict[str, Any]]:
    """{"type", "pol", "strength", "age_min"} from a quality signal's typed event (a dict with those keys, or an object with event_type / event_polarity / event_strength /
    event_age_min), or None when there is no readable type or the polarity is not +1 / -1. Never raises."""
    try:
        if event is None:
            return None
        get = event.get if isinstance(event, dict) else (lambda k, d=None, _e=event: getattr(_e, "event_" + k if k != "pol" else "event_polarity", d))
        et = str(get("type") or "").strip().lower()[:24]
        pol = get("pol")
        pol = int(pol) if pol in (1, -1) else 0
        if not et or not pol:
            return None
        out: Dict[str, Any] = {"type": et, "pol": pol}
        for k in ("strength", "age_min"):
            try:
                v = float(get(k))
                if v == v and abs(v) != float("inf"):
                    out[k] = round(v, 3)
            except (TypeError, ValueError):
                pass
        return out
    except Exception:  # noqa: BLE001
        return None


def attach(flow_json: Any, stamp: Dict[str, Any]) -> Any:
    """flow_json text with `stamp` under "setup"; the input unchanged on any problem. A stamp already present is kept (first reason wins)."""
    try:
        base = json.loads(flow_json) if isinstance(flow_json, str) and flow_json else (dict(flow_json) if isinstance(flow_json, dict) else {})
        if not isinstance(base, dict):
            base = {}
        if isinstance(base.get("setup"), dict) and base["setup"].get("id"):
            return flow_json
        base["setup"] = stamp
        return json.dumps(base, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        return flow_json


def read(flow_json: Any) -> Optional[Dict[str, Any]]:
    try:
        d = json.loads(flow_json) if isinstance(flow_json, str) and flow_json else flow_json
        s = d.get("setup") if isinstance(d, dict) else None
        return s if isinstance(s, dict) and s.get("id") else None
    except Exception:  # noqa: BLE001
        return None


def restamp(flow_json: Any, penalties: Optional[Dict[str, float]], size_mult: Optional[float]) -> Any:
    """E273 round 32: flow_json text with the stamp's penalties and size multiplier replaced (penalties that arrived after the setup engine sized the entry). The input
    unchanged when there is no stamp or on any problem. Never raises."""
    try:
        base = json.loads(flow_json) if isinstance(flow_json, str) and flow_json else (dict(flow_json) if isinstance(flow_json, dict) else None)
        if not isinstance(base, dict) or not isinstance(base.get("setup"), dict):
            return flow_json
        st = dict(base["setup"])
        st["penalties"] = {str(k): round(float(v), 4) for k, v in (penalties or {}).items()}
        if size_mult is not None:
            st["size_mult"] = round(float(size_mult), 4)
        base["setup"] = st
        return json.dumps(base, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        return flow_json
