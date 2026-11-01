"""E273 round 18 (Real Trade copy of position-stocks orders/setup_stamp.py, same shape): the setup stamp - which rule took a trade, written into flow_json["setup"] (no database column).

The stamp holds the setup id, its trigger, the confirmations met, the soft penalties and the size multiplier. Real Trade has no setup walk yet, so every
entry is stamped LEGACY_SHORTLIST with its trigger (the shortlist label), the confirmations that were true and the size penalties; that is the baseline the
new setups will be compared with. `confirmations_from` reads them from flow_json. Pure: no I/O, never raises.
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
        st["event"] = ev                # E285 round 115: the typed event the entry saw (type, polarity, strength, age), the same record Position Stocks keeps; record only
    return st


def event_part(event: Any) -> Optional[Dict[str, Any]]:
    """{"type", "pol", "strength", "age_min"} from a typed event (Real Trade's intraday_news.positive_event dict {polarity, type, strength, age_min}, or a dict / object already in
    Position Stocks' shape), or None when there is no readable type or the polarity is not +1 / -1. Same output as Position Stocks' setup_stamp.event_part. Never raises."""
    try:
        if event is None:
            return None
        def get(k: str, d: Any = None) -> Any:
            if isinstance(event, dict):
                keys = ("pol", "polarity", "event_polarity") if k == "pol" else (k, "event_" + k)
                return next((event[x] for x in keys if x in event), d)
            return getattr(event, "event_polarity" if k == "pol" else "event_" + k, d)
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


def _num(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError):
        return None


def confirmations_from(flow_json: Any, tick: Any = None) -> List[str]:
    """The confirmations that were TRUE for this entry, from what Real Trade already recorded: the opening-range verdict (`orb_break`; Real Trade has no trade-by-trade tape),
    the book (`book_bid_heavy`: 5-level imbalance above 0.15) and the price above VWAP (`above_vwap`). A missing fact is simply
    not listed. Never raises."""
    out: List[str] = []
    try:
        d = json.loads(flow_json) if isinstance(flow_json, str) and flow_json else (flow_json if isinstance(flow_json, dict) else {})
        d = d if isinstance(d, dict) else {}
        orb = d.get("orb")
        if isinstance(orb, dict) and orb.get("would_enter") is True:
            out.append("orb_break")
        imb = _num(d.get("imbalance_5"))
        if imb is not None and imb > 0.15:
            out.append("book_bid_heavy")
        price, vwap = _num(getattr(tick, "price", None)), _num(getattr(tick, "vwap", None))
        if price and vwap and price > vwap:
            out.append("above_vwap")
    except Exception:  # noqa: BLE001
        pass
    return out


def public(flow_json: Any) -> Optional[Dict[str, Any]]:
    """E285 round 104: the stamp in the shape the screens read (same keys as position-stocks `_row_setup`): id, trigger, confirmations, penalties, size_mult, slot, side.
    None when the text carries no stamp (a trade opened before entries were stamped, or a broker import) or cannot be read. Pure, never raises."""
    try:
        st = read(flow_json)
        if not st:
            return None
        conf, pen = st.get("confirmations"), st.get("penalties")
        return {"id": st.get("id"), "trigger": st.get("trigger") or "", "confirmations": [str(c) for c in conf][:10] if isinstance(conf, list) else [],
                "penalties": dict(pen) if isinstance(pen, dict) else {}, "size_mult": st.get("size_mult"), "slot": st.get("slot") or "later",
                "side": st.get("side") or "LONG"}
    except Exception:  # noqa: BLE001
        return None
