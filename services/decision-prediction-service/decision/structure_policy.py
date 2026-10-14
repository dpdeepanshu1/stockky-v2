"""group353 (WS-5): daily market-structure policy for the decision service. Pure, stdlib only, never raises.

Reads the flat `ms_*` summary (technical /analyze -> gateway payload) and answers ONE question for a BUY NOW candidate: is this a good
ENTRY, not just a good stock? It can only ever make a decision MORE conservative (BUY NOW -> PREPARE TO BUY / WAIT); it never upgrades.

ALWAYS LIVE (group370: the record-only "shadow" and "caps" modes were removed). The policy applies its downgrades and, when sane, replaces
the legacy stop/target with the structural ones. STRUCTURE_POLICY_MODE=off is the only switch (emergency kill switch); any other value = live.
Every threshold below is still a HYPOTHESIS until the backtest (scripts/structure_backtest.py) calibrates it - skips are silent, so run it.

BUY NOW needs ALL of: >= groups_min (3) independent confirming groups, entry-location score >= loc_min (55), and a bullish BOS that has not
failed and is <= bos_max_bars (10) bars old. A missing value counts as NOT met (unknown is never bullish) once a usable summary exists.
PREPARE TO BUY (group370): the arithmetic is recomputed at the live price and a PREPARE whose hard rules (chase / stretch / R:R / bearish)
now fail goes to WAIT; a PREPARE that passes EVERY BUY NOW requirement AND has a P9 trigger (fresh BOS / CHOCH / sweep / zone / fib) is
upgraded to BUY NOW - only when the caller says the upgrade is allowed (good data quality, not already owned).

Unknown is never bullish: quality unavailable / insufficient / stale, or a summary missing, means "no opinion" (decision untouched)."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

MODES = ("off", "full")
BUY_NOW, PREPARE, WAIT = "BUY NOW", "PREPARE TO BUY", "WAIT"
_SEVERITY = {PREPARE: 1, WAIT: 2}
_HARD_RULES = ("P2_CHASE", "P3_STRETCH", "P4_RR", "P5_TREND")   # rules whose verdict is WAIT; re-checked on a PREPARE

DEFAULTS: Dict[str, float] = {
    "chase_atr_max": 1.5,        # P2 price already this many ATR beyond the structure reference
    "stretch_atr_max": 3.0,      # P3 price this many ATR above EMA20
    "rr_min": 1.2,               # P4 structure R:R floor
    "room_atr_min": 0.8,         # P6 less than this much room to the next resistance
    "tqs_min": 40.0,             # kept for env compatibility; no longer used (P7 is the independent-group count)
    "groups_min": 3.0,           # P7 BUY NOW needs at least this many independent confirming groups
    "loc_min": 55.0,             # P7 BUY NOW needs an entry-location score of at least this
    "bos_max_bars": 10.0,        # P8 BUY NOW needs a non-failed bullish BOS at most this many bars old
    "choch_reversal_bars": 10.0,  # P5 a bullish CHOCH this recent excuses a bearish trend
    "max_price_drift_pct": 15.0,  # live price vs structure close: beyond this the summary is too old to use
    "stop_ratio_lo": 0.5,        # full mode: structural stop distance vs legacy stop distance sanity band
    "stop_ratio_hi": 1.6,
}


def policy_mode() -> str:
    """full (always live) | off (kill switch). A stale "shadow" / "caps" in an old .env is live (full), never record-only."""
    m = (os.getenv("STRUCTURE_POLICY_MODE") or "full").strip().lower()
    return "off" if m == "off" else "full"


def prepare_upgrade_enabled() -> bool:
    """P9 upgrade kill switch (STRUCTURE_PREPARE_UPGRADE=0/false/off). On by default."""
    return (os.getenv("STRUCTURE_PREPARE_UPGRADE") or "1").strip().lower() not in ("0", "false", "off", "no")


def _params() -> Dict[str, float]:
    p = dict(DEFAULTS)
    raw = (os.getenv("STRUCTURE_POLICY_PARAMS_JSON") or "").strip()
    if raw:
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                for k, v in d.items():
                    if k in p:
                        try:
                            p[k] = float(v)
                        except (TypeError, ValueError):
                            pass
        except Exception:  # noqa: BLE001
            pass
    return p


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or isinstance(v, bool):
            return None
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def build_structure_features(summary: Optional[Dict[str, Any]], price: Optional[float] = None) -> Dict[str, Any]:
    """Flat features, shifted to the live `price` when it differs from the structure close (chase / stretch / room / R:R)."""
    s = summary if isinstance(summary, dict) else {}
    f: Dict[str, Any] = {
        "quality": s.get("ms_quality") or "unavailable", "trend": s.get("ms_trend"), "strength": _num(s.get("ms_strength")),
        "bos_dir": s.get("ms_bos_dir"), "bos_bars": _num(s.get("ms_bos_bars")), "bos_failed": bool(s.get("ms_bos_failed")),
        "choch_dir": s.get("ms_choch_dir"), "choch_bars": _num(s.get("ms_choch_bars")),
        "sweep_dir": s.get("ms_sweep_dir"), "sweep_bars": _num(s.get("ms_sweep_bars")),
        "fvg_side": s.get("ms_fvg_side"), "fvg_status": s.get("ms_fvg_status"), "fvg_top": _num(s.get("ms_fvg_top")), "fvg_bottom": _num(s.get("ms_fvg_bottom")),
        "ob_side": s.get("ms_ob_side"), "ob_status": s.get("ms_ob_status"), "ob_top": _num(s.get("ms_ob_top")), "ob_bottom": _num(s.get("ms_ob_bottom")),
        "in_golden": bool(s.get("ms_in_golden")), "loc_score": _num(s.get("ms_loc_score")),
        "chase_atr": _num(s.get("ms_chase_atr")), "stretch_atr": _num(s.get("ms_stretch_atr")), "room_atr": _num(s.get("ms_room_atr")),
        "rr": _num(s.get("ms_rr")), "stop": _num(s.get("ms_stop")), "target": _num(s.get("ms_target")),
        "rr_flags": list(s.get("ms_rr_flags") or []), "atr": _num(s.get("ms_atr")), "ms_price": _num(s.get("ms_price")),
        "tqs": _num(s.get("ms_tqs")), "groups": _num(s.get("ms_groups")), "asof": s.get("ms_asof"), "ver": s.get("ms_ver"),
        "price_used": None, "drift_pct": None, "live_adjusted": False,
    }
    px, msp, atr = _num(price), f["ms_price"], f["atr"]
    if px and msp and atr and atr > 0:
        f["price_used"], f["drift_pct"] = px, round((px - msp) / msp * 100, 2)
        d = (px - msp) / atr
        if abs(d) > 1e-9:
            f["live_adjusted"] = True
            for k, sign in (("chase_atr", 1), ("stretch_atr", 1), ("room_atr", -1)):
                if f[k] is not None:
                    f[k] = round(f[k] + sign * d, 3)
        if f["stop"] is not None and f["target"] is not None and px > f["stop"]:
            risk, reward = px - f["stop"], f["target"] - px
            f["rr"] = round(reward / risk, 2) if risk > 0 and reward > 0 else None
            if reward <= 0 and "no_target" not in f["rr_flags"]:
                f["rr_flags"] = f["rr_flags"] + ["no_target"]
    return f


def _no_opinion(f: Dict[str, Any], p: Dict[str, float]) -> Optional[str]:
    if f["quality"] not in ("ok", "limited"):
        return f"structure {f['quality']}"
    if f["ms_price"] is None or f["atr"] is None:
        return "structure summary incomplete"
    if f["drift_pct"] is not None and abs(f["drift_pct"]) > p["max_price_drift_pct"]:
        return f"live price {f['drift_pct']:+.1f}% from structure close (too old)"
    return None


def _rules(f: Dict[str, Any], p: Dict[str, float]) -> List[Dict[str, Any]]:
    fired: List[Dict[str, Any]] = []

    def add(rid: str, to: str, msg: str) -> None:
        fired.append({"id": rid, "to": to, "reason": msg})

    if f["chase_atr"] is not None and f["chase_atr"] > p["chase_atr_max"]:
        add("P2_CHASE", WAIT, f"chasing: price {f['chase_atr']:.1f} ATR beyond the structure reference (max {p['chase_atr_max']:g})")
    if f["stretch_atr"] is not None and f["stretch_atr"] > p["stretch_atr_max"]:
        add("P3_STRETCH", WAIT, f"stretched: {f['stretch_atr']:.1f} ATR above EMA20 (max {p['stretch_atr_max']:g})")
    flags = set(f["rr_flags"])
    if "no_target" in flags:
        add("P4_RR", WAIT, "no structural target above price")
    elif "stop_too_wide" in flags:
        add("P4_RR", WAIT, "structural stop too far below price")
    elif f["rr"] is not None and f["rr"] < p["rr_min"]:
        add("P4_RR", WAIT, f"structure R:R {f['rr']:.2f} below {p['rr_min']:g}")
    if f["trend"] == "bearish":
        reversal = f["choch_dir"] == "bullish" and f["choch_bars"] is not None and f["choch_bars"] <= p["choch_reversal_bars"]
        if not reversal:
            add("P5_TREND", WAIT, "daily structure is bearish (lower highs / lower lows) with no recent bullish change of character")
    if f["room_atr"] is not None and f["room_atr"] < p["room_atr_min"]:
        add("P6_ROOM", PREPARE, f"only {max(f['room_atr'], 0):.1f} ATR of room to the next resistance")
    if f["groups"] is None or f["groups"] < p["groups_min"]:
        got = "unknown" if f["groups"] is None else f"{int(f['groups'])}"
        add("P7_QUALITY", PREPARE, f"thin confirmation: {got} independent group(s) positive, BUY NOW needs {int(p['groups_min'])}")
    if f["loc_score"] is None or f["loc_score"] < p["loc_min"]:
        got = "unknown" if f["loc_score"] is None else f"{int(f['loc_score'])}"
        add("P7_LOCATION", PREPARE, f"entry location score {got} is below {int(p['loc_min'])}")
    bars = f["bos_bars"]
    if f["bos_dir"] == "bullish" and f["bos_failed"]:
        add("P8_FAILED_BOS", PREPARE, "the last bullish break of structure failed (closed back inside)")
    elif f["bos_dir"] != "bullish" or bars is None:
        add("P8_NO_BOS", PREPARE, "no bullish break of structure to confirm the entry")
    elif bars > p["bos_max_bars"]:
        add("P8_OLD_BOS", PREPARE, f"the bullish break of structure is {int(bars)} bars old (max {int(p['bos_max_bars'])})")
    return fired


def prepare_triggers(f: Dict[str, Any]) -> List[str]:
    """P9: structure events near the current bar. On a PREPARE TO BUY, one of these plus a clean BUY NOW check upgrades it (apply_policy)."""
    t: List[str] = []
    if f.get("bos_dir") == "bullish" and f.get("bos_bars") is not None and f["bos_bars"] <= 3 and not f.get("bos_failed"):
        t.append("bos_near")
    if f.get("choch_dir") == "bullish" and f.get("choch_bars") is not None and f["choch_bars"] <= 5:
        t.append("choch")
    if f.get("sweep_dir") == "bullish" and f.get("sweep_bars") is not None and f["sweep_bars"] <= 3:
        t.append("sweep")
    px = f.get("price_used") or f.get("ms_price")
    for name, side, top, bot, st in (("entering_fvg", f.get("fvg_side"), f.get("fvg_top"), f.get("fvg_bottom"), f.get("fvg_status")),
                                     ("entering_ob", f.get("ob_side"), f.get("ob_top"), f.get("ob_bottom"), f.get("ob_status"))):
        if side == "bullish" and px and top and bot and st not in ("filled", "broken", "expired") and bot * 0.995 <= px <= top * 1.005:
            t.append(name)
    if f.get("in_golden"):
        t.append("entering_fib")
    return t


def _wait_zone(f: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Where a pullback entry would be structurally sound: the nearest live bullish demand / imbalance zone below price."""
    px = f.get("price_used") or f.get("ms_price")
    best = None
    for name, side, top, bot, st in (("demand zone", f.get("ob_side"), f.get("ob_top"), f.get("ob_bottom"), f.get("ob_status")),
                                     ("imbalance zone", f.get("fvg_side"), f.get("fvg_top"), f.get("fvg_bottom"), f.get("fvg_status"))):
        if side == "bullish" and top and bot and px and top < px and st not in ("filled", "broken", "expired"):
            if best is None or top > best["high"]:
                best = {"low": round(bot, 2), "high": round(top, 2), "kind": name}
    return best


def _structural_levels(f: Dict[str, Any], p: Dict[str, float], price: Any, legacy: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """The structural stop / target as replacement levels - only when sane (stop < price < target, stop distance within the band of the legacy one)."""
    sl, tg, px = f["stop"], f["target"], _num(price)
    lstop = _num(legacy.get("stop_loss"))
    if sl is not None and tg is not None and px and sl < px < tg:
        if lstop is not None and lstop < px:
            ratio = (px - sl) / (px - lstop)
            if not (p["stop_ratio_lo"] <= ratio <= p["stop_ratio_hi"]):
                return None
        return {"stop_loss": round(sl, 2), "target": round(tg, 2)}
    return None


def apply_policy(decision: str, summary: Optional[Dict[str, Any]], price: Optional[float], legacy: Optional[Dict[str, Any]] = None,
                 explain: Optional[Dict[str, Any]] = None, mode: Optional[str] = None, allow_upgrade: bool = True) -> Dict[str, Any]:
    """Returns {'decision': final, 'structure': block-for-the-response, 'levels': optional replacement levels}. Never raises.
    `mode` is "off" or anything else (= live). `allow_upgrade=False` stops a P9 PREPARE -> BUY NOW upgrade (the caller knows the data quality)."""
    mode = "off" if mode == "off" else ("full" if mode else policy_mode())
    legacy = legacy or {}
    out_struct: Dict[str, Any] = {"mode": mode, "applied": False, "original_decision": decision, "policy_decision": decision,
                                  "rules": [], "no_opinion": None, "legacy_levels": legacy, "structural_levels": None, "wait_for_entry": None,
                                  "prepare_triggers": [], "features": None, "explain": explain if isinstance(explain, dict) else None}
    res = {"decision": decision, "structure": out_struct, "levels": None}
    try:
        if mode == "off":
            out_struct["no_opinion"] = "policy off"
            return res
        f = build_structure_features(summary, price)
        out_struct["features"] = f
        p = _params()
        if decision == PREPARE:
            out_struct["prepare_triggers"] = prepare_triggers(f)
        why = _no_opinion(f, p)
        if why:
            out_struct["no_opinion"] = why
            return res
        if f["stop"] is not None and f["target"] is not None:
            out_struct["structural_levels"] = {"stop": f["stop"], "target": f["target"], "rr": f["rr"]}
        if decision not in (BUY_NOW, PREPARE):
            return res       # WAIT / DO NOT BUY / HOLD are never touched
        fired = _rules(f, p)
        if decision == PREPARE:
            # group370: the PREPARE arithmetic (chase / stretch / R:R / trend at the LIVE price) is ACTIVE, not informational.
            hard = [r for r in fired if r["id"] in _HARD_RULES]
            if hard:
                out_struct["rules"] = hard
                out_struct["policy_decision"] = WAIT
                out_struct["wait_for_entry"] = _wait_zone(f)
                out_struct["applied"] = True
                res["decision"] = WAIT
                return res
            trig = out_struct["prepare_triggers"]
            if not fired and trig and allow_upgrade and prepare_upgrade_enabled():
                up = {"id": "P9_UPGRADE", "to": BUY_NOW, "reason": "structure confirms the entry now (" + ", ".join(trig) + "); every BUY NOW requirement is met"}
                out_struct["rules"] = [up]
                out_struct["policy_decision"] = BUY_NOW
                out_struct["applied"] = True
                res["decision"] = BUY_NOW
                res["levels"] = _structural_levels(f, p, price, legacy)
            return res
        out_struct["rules"] = fired
        if not fired:
            res["levels"] = _structural_levels(f, p, price, legacy)
            return res
        final = max((r["to"] for r in fired), key=lambda t: _SEVERITY[t])
        out_struct["policy_decision"] = final
        if final == WAIT:
            out_struct["wait_for_entry"] = _wait_zone(f)
        out_struct["applied"] = True
        res["decision"] = final
        res["levels"] = _structural_levels(f, p, price, legacy)
        return res
    except Exception as e:  # noqa: BLE001 - the policy must never break a decision
        out_struct["no_opinion"] = f"policy error: {type(e).__name__}"
        return {"decision": decision, "structure": out_struct, "levels": None}


def summary_line(struct: Optional[Dict[str, Any]]) -> Optional[str]:
    """One mobile-friendly line: 'Trend up - BOS 2d - RR 1.9 - chase 0.3 ATR'."""
    f = (struct or {}).get("features")
    if not isinstance(f, dict) or f.get("quality") not in ("ok", "limited"):
        return None
    arrow = {"bullish": "up", "bearish": "down"}.get(f.get("trend") or "", "range")
    bits = [f"Trend {arrow}"]
    if f.get("bos_dir") and f.get("bos_bars") is not None:
        bits.append(f"BOS {'up' if f['bos_dir'] == 'bullish' else 'down'} {int(f['bos_bars'])}d")
    if f.get("rr") is not None:
        bits.append(f"RR {f['rr']:.1f}")
    if f.get("chase_atr") is not None:
        bits.append(f"chase {f['chase_atr']:.1f} ATR")
    return " · ".join(bits)
