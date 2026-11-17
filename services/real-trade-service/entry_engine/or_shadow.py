"""entry_engine/or_shadow.py - round 599: SHADOW ONLY "OR" entry rules. Records what two looser rules WOULD have done; never changes an entry.

Why: every Real Trade entry gate is an AND, so a name that one gate dislikes is refused whatever else is true. On 17 Nov: 2518 WAITs, 0 orders, and the one name that matched S1
(HDFCBANK, twice) was stopped by `soft_stack`. Loosening needs evidence first, so these rules only log and stamp until 2-3 sessions show whether the names they would have
let in won. Nothing here places, sizes or blocks an order. Pure, stdlib only, never raises.

  rule A  at a `soft_stack` refusal: a setup (S1 / S2 / S3) matched, so the stack of soft penalties would be OR-ed away and the entry go in at the floor size.
  rule B  at a `no_setup_window` refusal: no setup matched, but a fresh typed POSITIVE event (strength >= min_strength, not priced in) AND price above VWAP AND the sector in its
          top third are all true: S1's event + two of its confirmations without the opening-range-break trigger.

OR_SHADOW_MODE: "log" (default) records; "off" does nothing. There is deliberately no "live" mode: it is added only after the evidence says so.
The record is one INFO line `OR_SHADOW[<mode>/<symbol>] rule A|B ...` and a list under decision.flow_json["or_shadow"] (the decision row is stored anyway).
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

KEY = "or_shadow"


def _num(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError):
        return None


def enabled(mode: Any) -> bool:
    return str(mode or "log").strip().lower() != "off"


def rule_a(stack_reason: Any, setup_res: Any) -> Optional[Dict[str, Any]]:
    """Hit when the soft-stack refusal fired AND a setup matched. None otherwise."""
    try:
        if not stack_reason or not isinstance(setup_res, dict) or not setup_res.get("id"):
            return None
        return {"rule": "A", "would": "allow_at_floor_size", "setup": str(setup_res.get("id")), "refused": str(stack_reason)[:160]}
    except Exception:  # noqa: BLE001
        return None


def rule_b(window_reason: Any, event: Any, above_vwap: Any, sector_third: Any, priced_in: Any = None, min_strength: float = 0.5) -> Optional[Dict[str, Any]]:
    """Hit when the no-setup-window refusal fired AND a fresh typed positive event (strength >= min_strength, not priced in) AND above VWAP AND sector top third. None otherwise.
    A missing fact is not true (same as the setup engine)."""
    try:
        if not window_reason or not isinstance(event, dict) or event.get("polarity") != 1:
            return None
        st = _num(event.get("strength"))
        if st is None or st < float(min_strength):
            return None
        pi = _num(priced_in)
        if pi is not None and pi > 1.0:
            return None                                   # already priced in: the setup engine refuses S1 for it too
        if above_vwap is not True or sector_third != "top":
            return None
        return {"rule": "B", "would": "allow_plain_entry", "event": str(event.get("type") or "news")[:40], "strength": round(st, 3),
                "age_min": event.get("age_min"), "refused": str(window_reason)[:160]}
    except Exception:  # noqa: BLE001
        return None


def stamp(flow_json: Any, hit: Optional[Dict[str, Any]]) -> Any:
    """Append `hit` to flow_json["or_shadow"] (a list). Returns the input unchanged when there is nothing to add or the text is not a JSON object."""
    try:
        if not isinstance(hit, dict):
            return flow_json
        if isinstance(flow_json, str) and flow_json.strip():
            d = json.loads(flow_json)
        elif isinstance(flow_json, dict):
            d = dict(flow_json)
        else:
            d = {}
        if not isinstance(d, dict):
            return flow_json
        lst = d.get(KEY)
        lst = list(lst) if isinstance(lst, list) else []
        lst.append(hit)
        d[KEY] = lst[-4:]
        return json.dumps(d, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        return flow_json
