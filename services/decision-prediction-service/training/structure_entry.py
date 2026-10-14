"""group359: the daily market-structure snapshot taken when a paper trade is ENTERED. Pure, stdlib only, never raises.

The decision service answers every /decide with a `structure` record (policy mode, rules fired, structural levels, a few features). The
Trades tab ran without it, so a closed trade could not be compared with what the structure said when it was opened. The frontend now sends
that record with the pick; `sanitize_entry` keeps only an allow-listed, size-capped copy of it and `attach` stores it inside the prediction
snapshot's existing `feature_snapshot` JSON under `structure_entry` (no schema change). It is written ONCE: a later re-click or refresh never
rewrites it, because it documents the moment of entry. `read_entry` is what GET /api/trades returns as `structure`.

Unknown is never bullish: a missing / unusable record gives None (the tab says "no structure snapshot"), never an invented value."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

KEY = "structure_entry"
VERSION = 1
MAX_RULES = 8
MAX_TEXT = 200
MAX_BYTES = 4000

_MODES = ("shadow", "caps", "full")           # shadow/caps only for rows stored BEFORE group370 (policy is always full now); "off" = never looked: not stored
_FEATURE_NUM = ("bos_bars", "choch_bars", "chase_atr", "stretch_atr", "room_atr", "rr", "stop", "target", "tqs", "groups", "ms_price", "atr")
_FEATURE_STR = ("quality", "trend", "bos_dir", "choch_dir", "asof", "ver")


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or isinstance(v, bool):
            return None
        f = float(v)
        return round(f, 4) if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _txt(v: Any, n: int = MAX_TEXT) -> Optional[str]:
    return v.strip()[:n] if isinstance(v, str) and v.strip() else None


def sanitize_entry(raw: Any) -> Optional[Dict[str, Any]]:
    """Compact copy of a decision-service `structure` record, or None when there is nothing usable (not a dict, mode off / unknown)."""
    try:
        if not isinstance(raw, dict):
            return None
        mode = raw.get("mode")
        if mode not in _MODES:
            return None
        rules = []
        for r in (raw.get("rules") if isinstance(raw.get("rules"), list) else [])[:MAX_RULES]:
            if isinstance(r, dict) and _txt(r.get("reason")):
                rules.append({"id": _txt(r.get("id"), 24), "to": _txt(r.get("to"), 24), "reason": _txt(r.get("reason"))})
        f = raw.get("features") if isinstance(raw.get("features"), dict) else {}
        feats: Dict[str, Any] = {k: _num(f.get(k)) for k in _FEATURE_NUM if _num(f.get(k)) is not None}
        feats.update({k: _txt(f.get(k), 40) for k in _FEATURE_STR if _txt(f.get(k), 40)})
        if isinstance(f.get("bos_failed"), bool):
            feats["bos_failed"] = f["bos_failed"]
        sl = raw.get("structural_levels") if isinstance(raw.get("structural_levels"), dict) else None
        out: Dict[str, Any] = {
            "v": VERSION, "mode": mode, "applied": raw.get("applied") is True,
            "original_decision": _txt(raw.get("original_decision"), 24), "policy_decision": _txt(raw.get("policy_decision"), 24),
            "rules": rules, "no_opinion": _txt(raw.get("no_opinion")),
            "levels": ({"stop": _num(sl.get("stop")), "target": _num(sl.get("target")), "rr": _num(sl.get("rr"))}
                       if sl and _num(sl.get("stop")) is not None else None),
            "features": feats, "summary_line": _txt(raw.get("summary_line")),
        }
        if len(json.dumps(out, default=str)) > MAX_BYTES:
            out["features"] = {k: v for k, v in feats.items() if k in ("quality", "trend", "rr", "chase_atr")}
            out["rules"] = rules[:3]
        return out
    except Exception:  # noqa: BLE001
        return None


def attach(feature_snapshot: Any, raw: Any) -> Optional[Dict[str, Any]]:
    """The feature_snapshot to store: a COPY of the given dict (or a new one) carrying `structure_entry`. None means "leave the stored value
    alone" (nothing usable to add, or an entry is already there: entry-time data is written once)."""
    try:
        base = feature_snapshot if isinstance(feature_snapshot, dict) else ({} if feature_snapshot in (None, "") else None)
        if base is None or KEY in base and read_entry(base) is not None:
            return None
        entry = sanitize_entry(raw if raw is not None else base.get("structure"))
        if entry is None:
            return None
        merged = dict(base)
        merged[KEY] = entry
        return merged
    except Exception:  # noqa: BLE001
        return None


def read_entry(feature_snapshot: Any) -> Optional[Dict[str, Any]]:
    """The stored entry snapshot (what GET /api/trades returns as `structure`), or None. Accepts a dict or a JSON string."""
    try:
        fs = json.loads(feature_snapshot) if isinstance(feature_snapshot, str) else feature_snapshot
        e = fs.get(KEY) if isinstance(fs, dict) else None
        return e if isinstance(e, dict) and e.get("mode") in _MODES else None
    except Exception:  # noqa: BLE001
        return None
