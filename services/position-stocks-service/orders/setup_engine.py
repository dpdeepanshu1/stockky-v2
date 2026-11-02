"""E273: setup_engine - one trigger gives the reason to enter, or there is no trade (stdlib only, no I/O, no config; byte-identical in position-stocks and real-trade).

classify(facts) walks the fixed priority S1_CATALYST_UP, S4_CATALYST_DOWN, S2_RS_BREAKOUT, S3_EXHAUSTION and returns the first setup whose TRIGGER is true and
of whose three CONFIRMATIONS at least two are true. Candles and tape are only confirmations, never triggers. No setup = {"id": None, "skip": "NO_SETUP"}.
If S1 and S2 both fire for a long, S1 is the reason and S2 is added to `extra` (more conviction). The result's `stamp` is what orders/setup_stamp.py writes
into flow_json["setup"].

facts (every key optional; a missing fact is "not true", never a guess):
  side "LONG"|"SHORT", service "PS"|"RT" (RT is long only; S4 is an exit trigger there, never an entry), now (datetime, IST clock), allowed (set of setup ids; None = all)
  event_polarity +1/-1/0 (0 or missing = unknown, never triggers), event_priced_in (price move / expected move; above 1 = already priced: S1 / S4 refused)
  price, preopen_price, or_high, or_low, bar_close (last COMPLETED 1-minute bar close), bar_closed_after_range (bool: that bar closed after the opening range ended)
  above_vwap (bool), tape (+1 buying / -1 selling / 0 or None unknown), sector_third "top"|"mid"|"bottom"
  S2: rs_pct (0..1, 1 = strongest of the day; weakest side uses 1 - rs_pct), gap_pct, structure_ok, or_break (+1 closed above range / -1 below), pullback_hold, vol_vs_norm, candle (+1 bullish / -1 bearish / 0)
  S3: z_move (signed move in the stock's own volatility), failure_candle, tape_exhaustion, vwap_stretch, climax_volume, wall_close, liquid, kind "spike"|"panic_dump"|"gap_down_reversal"|"below_open_pullback"
  gap_pctile (0..1: the gap's rank among the stock's own gaps; above GAP_HALF_PCTILE halves the size), penalties {name: z-units}, promoted (set of "ID|slot" cells that earned full size)
The numbers below are STARTING values (the plan: each setup learns its own live); the learned value replaces them through adaptive_params once a cell has 30 trades.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

S1, S4, S2, S3 = "S1_CATALYST_UP", "S4_CATALYST_DOWN", "S2_RS_BREAKOUT", "S3_EXHAUSTION"
PRIORITY = (S1, S4, S2, S3)
START_MIN = {S1: 9 * 60 + 21, S4: 9 * 60 + 21, S2: 9 * 60 + 30, S3: 9 * 60 + 45}   # first closed bar after the 09:15-09:20 range is 09:21
MIN_CONFIRMATIONS = 2
RS_TOP = 0.9                  # top decile of the day
Z_SPIKE = 2.0                 # S3: move beyond this many own-volatility units
GAP_HALF_PCTILE = 0.95        # a gap above the 95th percentile of its own gaps halves the size
PENALTY_SKIP = 4.0            # total soft penalty (z-units) that ends in a skip; below it the size shrinks
SIZE_FLOOR = 0.25


def _t(v: Any) -> bool:
    return v is True or (isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0)


def _minute(now: Any) -> Optional[int]:
    try:
        return now.hour * 60 + now.minute if isinstance(now, datetime) else None
    except Exception:  # noqa: BLE001
        return None


def slot(now: Any) -> str:
    m = _minute(now)
    if m is None or m >= 10 * 60 + 30:
        return "later"
    return "0920" if m < 9 * 60 + 30 else ("0930" if m < 10 * 60 else "1000")


def _count(conf: Dict[str, bool]) -> List[str]:
    return [k for k, v in conf.items() if v]


def _s1(f: Dict[str, Any], sign: int) -> Optional[Dict[str, Any]]:
    """S1 (sign +1, long) / S4 (sign -1, short): a typed event of the matching polarity, price holds the right side of the pre-open price, and a completed bar
    closes beyond the opening range."""
    pol = f.get("event_polarity")
    if not isinstance(pol, (int, float)) or isinstance(pol, bool) or pol * sign <= 0:
        return None
    pi = f.get("event_priced_in")
    if isinstance(pi, (int, float)) and not isinstance(pi, bool) and pi > 1.0:
        return None                                   # already priced in: only S3 may take it
    price, pre, close = f.get("price"), f.get("preopen_price"), f.get("bar_close")
    edge = f.get("or_high") if sign > 0 else f.get("or_low")
    try:
        holds = (float(price) - float(pre)) * sign >= 0
        broke = (float(close) - float(edge)) * sign > 0 and bool(f.get("bar_closed_after_range", True))
    except (TypeError, ValueError):
        return None
    if not (holds and broke):
        return None
    tape = f.get("tape")
    third = f.get("sector_third")
    vw = f.get("above_vwap")
    conf = {"tape_buying" if sign > 0 else "tape_selling": isinstance(tape, (int, float)) and tape * sign > 0,
            "above_vwap" if sign > 0 else "below_vwap": (vw is True) if sign > 0 else (vw is False),
            "sector_top_third" if sign > 0 else "sector_bottom_third": third == ("top" if sign > 0 else "bottom")}
    if sign < 0 and "board_bear" in f:
        conf["board_bearish"] = f.get("board_bear") is True          # E274 round 42: the Market Board ranks the short bearish with enough conviction (fact present only when the switch is on)
    return {"trigger": ("positive event, price holds above the pre-open price, closed bar above the opening-range high" if sign > 0 else
                        "negative event, price below the pre-open price, closed bar below the opening-range low"), "conf": conf}


def _s2(f: Dict[str, Any], sign: int) -> Optional[Dict[str, Any]]:
    """S2: never a breakout alone. Strongest (or weakest) stock of the day, a gap that is not against the side, daily structure agrees, and a closed-bar break
    of the opening range or a pullback hold."""
    try:
        rs = float(f.get("rs_pct"))
        gap = float(f.get("gap_pct"))                 # E273 round 5: an unknown gap is not "no gap" - S2 needs it known
    except (TypeError, ValueError):
        return None
    strong = (rs if sign > 0 else 1.0 - rs) >= RS_TOP
    gap_ok = gap * sign >= 0
    brk = isinstance(f.get("or_break"), (int, float)) and f["or_break"] * sign > 0
    if not (strong and gap_ok and f.get("structure_ok") is True and (brk or f.get("pullback_hold") is True)):
        return None
    tape, cd = f.get("tape"), f.get("candle")
    conf = {"volume_above_norm": isinstance(f.get("vol_vs_norm"), (int, float)) and f["vol_vs_norm"] > 1.0,
            "rejection_candle": isinstance(cd, (int, float)) and cd * sign > 0,
            "tape_with_side": isinstance(tape, (int, float)) and tape * sign > 0}
    return {"trigger": "top-decile relative " + ("strength" if sign > 0 else "weakness") + ", gap not against, structure agrees, closed-bar range break or pullback hold", "conf": conf}


def _s3(f: Dict[str, Any], sign: int) -> Optional[Dict[str, Any]]:
    """S3: sign -1 = short after an up-spike, sign +1 = long after a panic dump (liquid names) / gap-down reversal / below-open pullback. Needs the move beyond
    the stock's own volatility AND a failure candle AND tape exhaustion or absorption."""
    try:
        z = float(f.get("z_move"))
    except (TypeError, ValueError):
        return None
    kind = f.get("kind")
    if sign > 0:
        if kind not in ("panic_dump", "gap_down_reversal", "below_open_pullback") or f.get("liquid") is not True:
            return None
        if z > -Z_SPIKE:
            return None
    elif z < Z_SPIKE:
        return None
    if not (f.get("failure_candle") is True and f.get("tape_exhaustion") is True):
        return None
    conf = {"vwap_stretch": f.get("vwap_stretch") is True, "climax_volume": f.get("climax_volume") is True, "wall_close": f.get("wall_close") is True}
    return {"trigger": ("down-move beyond own volatility, failure candle, tape exhaustion (" if sign > 0 else "up-move beyond own volatility, failure candle, tape exhaustion (") + str(kind or "spike") + ")", "conf": conf}


def _size(setup_id: str, sl: str, f: Dict[str, Any], pen_total: float) -> float:
    promoted = f.get("promoted") or ()
    if f"{setup_id}|{sl}" in promoted:
        base = 1.0
    elif sl == "0920":
        base = 0.25                                   # the 09:20 slot starts at quarter size
    else:
        base = 0.5                                    # S2 half size until its stats prove it; S3 and the later slots start at probation
    g = f.get("gap_pctile")
    if isinstance(g, (int, float)) and not isinstance(g, bool) and g > GAP_HALF_PCTILE:
        base *= 0.5
    if pen_total > 0:
        base = max(SIZE_FLOOR, base / (1.0 + pen_total))      # soft penalties shrink the size (never below the floor); a large total skips instead
    return round(base, 4)


def classify(facts: Dict[str, Any]) -> Dict[str, Any]:
    """The winning setup, or {"id": None, "skip": "NO_SETUP" | "PENALTY_TOO_HIGH", ...}. Never raises."""
    try:
        f = dict(facts or {})
        side = str(f.get("side") or "LONG").upper()
        rt = str(f.get("service") or "PS").upper() == "RT"
        if rt and side != "LONG":
            return {"id": None, "skip": "NO_SETUP", "why": "real-trade is long only"}
        allowed = f.get("allowed")
        m = _minute(f.get("now"))
        sl = slot(f.get("now"))
        found: List[Dict[str, Any]] = []
        for sid in PRIORITY:
            if allowed is not None and sid not in allowed:
                continue
            if m is None or m < START_MIN[sid]:
                continue
            if sid == S1 and side == "LONG":
                r = _s1(f, +1)
            elif sid == S4 and side == "SHORT" and not rt:
                r = _s1(f, -1)
            elif sid == S2:
                r = _s2(f, +1 if side == "LONG" else -1)
            elif sid == S3:
                r = _s3(f, +1 if side == "LONG" else -1)
            else:
                r = None
            if r is None:
                continue
            met = _count(r["conf"])
            if len(met) >= MIN_CONFIRMATIONS:
                found.append({"id": sid, "trigger": r["trigger"], "met": met})
        if not found:
            return {"id": None, "skip": "NO_SETUP"}
        win, extra = found[0], [x["id"] for x in found[1:]]
        pens = {str(k): float(v) for k, v in (f.get("penalties") or {}).items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        pen_total = sum(pens.values())
        skip_at = PENALTY_SKIP
        ps = f.get("penalty_skip")                                    # optional: a caller may raise (never lower) the skip total; absent = PENALTY_SKIP
        if isinstance(ps, (int, float)) and not isinstance(ps, bool) and ps == ps and ps > PENALTY_SKIP:
            skip_at = float(ps)
        if pen_total >= skip_at:
            return {"id": None, "skip": "PENALTY_TOO_HIGH", "penalty_total": round(pen_total, 4)}
        mult = _size(win["id"], sl, f, pen_total)
        conf = win["met"] + extra                                     # an extra setup that fired is stored as a confirmation
        return {"id": win["id"], "trigger": win["trigger"], "confirmations": conf, "extra": extra, "size_mult": mult, "slot": sl, "side": side,
                "penalty_total": round(pen_total, 4),
                "stamp": {"id": win["id"], "trigger": win["trigger"][:200], "confirmations": conf[:10], "penalties": {k: round(v, 4) for k, v in pens.items()},
                          "size_mult": mult, "slot": sl, "side": side}}
    except Exception:  # noqa: BLE001
        return {"id": None, "skip": "NO_SETUP", "why": "error"}
