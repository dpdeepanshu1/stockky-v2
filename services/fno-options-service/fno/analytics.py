"""fno/analytics.py - what an option chain says: PCR, max pain, OI walls, OI build-up, ATM IV, the ATM straddle (expected move), IV rank. Pure, no I/O.

Reading guide (the usual market convention; a signal input, never a promise):
  PCR (put OI / call OI) high  -> many puts written (support below) -> mildly bullish;  low -> mildly bearish.
  call wall = strike with the most call OI at or above spot  -> resistance;  put wall = most put OI at or below spot -> support.
  OI build-up per contract: price up + OI up = long build-up (+2), price up + OI down = short covering (+1), price down + OI down = long unwinding (-1),
  price down + OI up = short build-up (-2). Price change is measured against the previous close, OI against previous_oi (Dhan fields).
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import Chain, Side, StrikeRow


def _sum(vals) -> float:
    return float(sum(v for v in vals if v is not None))


def pcr(chain: Chain) -> Dict[str, Optional[float]]:
    call_oi = _sum(r.ce.oi for r in chain.rows if r.ce)
    put_oi = _sum(r.pe.oi for r in chain.rows if r.pe)
    call_v = _sum(r.ce.volume for r in chain.rows if r.ce)
    put_v = _sum(r.pe.volume for r in chain.rows if r.pe)
    return {"pcr_oi": round(put_oi / call_oi, 3) if call_oi > 0 else None, "pcr_volume": round(put_v / call_v, 3) if call_v > 0 else None,
            "call_oi": call_oi, "put_oi": put_oi}


def max_pain(chain: Chain) -> Optional[float]:
    """The settlement level at which option writers pay out the least (sum over strikes of OI x intrinsic value). None without OI."""
    rows = [r for r in chain.rows if (r.ce and r.ce.oi) or (r.pe and r.pe.oi)]
    if len(rows) < 2:
        return None
    best_k, best_pay = None, None
    for cand in rows:
        k = cand.strike
        pay = 0.0
        for r in rows:
            if r.ce and r.ce.oi:
                pay += r.ce.oi * max(0.0, k - r.strike)
            if r.pe and r.pe.oi:
                pay += r.pe.oi * max(0.0, r.strike - k)
        if best_pay is None or pay < best_pay:
            best_k, best_pay = k, pay
    return best_k


def oi_walls(chain: Chain, top: int = 3) -> Dict[str, Any]:
    """Top call-OI strikes at/above spot (resistance) and top put-OI strikes at/below spot (support)."""
    spot = chain.spot
    if spot is None:
        return {"resistance": [], "support": [], "call_wall": None, "put_wall": None}
    calls = sorted(((r.strike, r.ce.oi) for r in chain.rows if r.ce and r.ce.oi and r.strike >= spot), key=lambda x: -x[1])[:top]
    puts = sorted(((r.strike, r.pe.oi) for r in chain.rows if r.pe and r.pe.oi and r.strike <= spot), key=lambda x: -x[1])[:top]
    return {"resistance": [{"strike": k, "oi": v} for k, v in calls], "support": [{"strike": k, "oi": v} for k, v in puts],
            "call_wall": calls[0][0] if calls else None, "put_wall": puts[0][0] if puts else None}


def buildup(side: Optional[Side]) -> Optional[int]:
    """+2 long build-up, +1 short covering, -1 long unwinding, -2 short build-up, 0 flat, None unknown."""
    if side is None or side.ltp is None or side.prev_close in (None, 0) or side.oi is None or side.prev_oi in (None, 0):
        return None
    px = side.ltp - side.prev_close
    oi = side.oi - side.prev_oi
    if px == 0 or oi == 0:
        return 0
    if px > 0:
        return 2 if oi > 0 else 1
    return -1 if oi < 0 else -2


def atm_block(chain: Chain) -> Dict[str, Any]:
    i = chain.atm_index()
    if i is None:
        return {"atm_strike": None, "atm_iv": None, "straddle": None, "expected_move_pct": None, "ce_ltp": None, "pe_ltp": None}
    row = chain.rows[i]
    ivs = [s.iv for s in (row.ce, row.pe) if s and s.iv and s.iv > 0]
    ce = row.ce.ltp if row.ce else None
    pe = row.pe.ltp if row.pe else None
    straddle = (ce + pe) if (ce is not None and pe is not None) else None
    spot = chain.spot
    em = (straddle / spot * 100.0) if (straddle is not None and spot) else None
    return {"atm_strike": row.strike, "atm_iv": round(sum(ivs) / len(ivs), 2) if ivs else None, "straddle": round(straddle, 2) if straddle is not None else None,
            "expected_move_pct": round(em, 3) if em is not None else None, "ce_ltp": ce, "pe_ltp": pe}


def near_atm_flow(chain: Chain, each_side: int = 3) -> Dict[str, Any]:
    """OI added in calls vs puts within `each_side` strikes of ATM, and the build-up codes of the ATM pair."""
    i = chain.atm_index()
    if i is None:
        return {"call_oi_change": None, "put_oi_change": None, "flow": 0.0, "atm_ce_buildup": None, "atm_pe_buildup": None}
    lo, hi = max(0, i - each_side), min(len(chain.rows), i + each_side + 1)
    near = chain.rows[lo:hi]
    dc = _sum((r.ce.oi - r.ce.prev_oi) for r in near if r.ce and r.ce.oi is not None and r.ce.prev_oi is not None)
    dp = _sum((r.pe.oi - r.pe.prev_oi) for r in near if r.pe and r.pe.oi is not None and r.pe.prev_oi is not None)
    tot = abs(dc) + abs(dp)
    # more PUT OI added than CALL OI added = writers backing the market = bullish: flow in [-1, 1], + bullish
    flow = (dp - dc) / tot if tot > 0 else 0.0
    row = chain.rows[i]
    return {"call_oi_change": dc, "put_oi_change": dp, "flow": round(flow, 3), "atm_ce_buildup": buildup(row.ce), "atm_pe_buildup": buildup(row.pe)}


def iv_rank(current: Optional[float], history: Sequence[float]) -> Optional[Dict[str, float]]:
    """IV rank (position inside the min-max range) and percentile (share of history below) from a list of past ATM IVs; None when there is too little."""
    h = [x for x in history if x is not None and x > 0]
    if current is None or len(h) < 20:
        return None
    lo, hi = min(h), max(h)
    rank = 50.0 if hi == lo else (current - lo) / (hi - lo) * 100.0
    pct = sum(1 for x in h if x < current) / len(h) * 100.0
    return {"iv_rank": round(max(0.0, min(100.0, rank)), 1), "iv_percentile": round(pct, 1)}


def chain_summary(chain: Chain, each_side: int = 3, iv_history: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    atm = atm_block(chain)
    out: Dict[str, Any] = {"underlying": chain.underlying, "expiry": chain.expiry, "spot": chain.spot, "step": chain.step(), "strikes": len(chain.rows)}
    out.update(pcr(chain))
    out["max_pain"] = max_pain(chain)
    out.update(oi_walls(chain))
    out.update(atm)
    out.update(near_atm_flow(chain, each_side))
    if iv_history is not None:
        out["iv_rank"] = iv_rank(atm.get("atm_iv"), iv_history)
    return out


def chain_score(summary: Dict[str, Any]) -> Tuple[float, List[str]]:
    """Chain confirmation score in [-30, +30] (+ bullish) and the reasons. Missing inputs add nothing (never read as neutral-good)."""
    score = 0.0
    why: List[str] = []
    p = summary.get("pcr_oi")
    if p is not None:
        if p >= 1.2:
            score += 10; why.append(f"PCR {p} high (puts written)")
        elif p >= 1.0:
            score += 4; why.append(f"PCR {p} slightly bullish")
        elif p <= 0.7:
            score -= 10; why.append(f"PCR {p} low (calls written)")
        elif p < 1.0:
            score -= 4; why.append(f"PCR {p} slightly bearish")
    spot = summary.get("spot")
    cw, pw = summary.get("call_wall"), summary.get("put_wall")
    if spot:
        if cw is not None and 0 <= (cw - spot) / spot * 100.0 <= 0.25:
            score -= 10; why.append(f"call wall {cw:g} right above spot")
        if pw is not None and 0 <= (spot - pw) / spot * 100.0 <= 0.25:
            score += 10; why.append(f"put wall {pw:g} right below spot")
    flow = summary.get("flow") or 0.0
    if abs(flow) >= 0.25:
        score += 10.0 * (1 if flow > 0 else -1)
        why.append("put OI added near ATM" if flow > 0 else "call OI added near ATM")
    return max(-30.0, min(30.0, score)), why
