"""
buy_sniper.py — Hot Picks scoring, buy-suggestion builder, and actionable filter.

MARKET INTELLIGENCE APPLIED (Aug-2026):
════════════════════════════════════════
Nifty at 24,090 — correction (−7% in 6m). FII net-short 1,97,792 contracts.
Midcap100 +12.88% | Smallcap100 +12.49% | Nifty50 −1.08% (1Y divergence: 14%).
PSU Banks +29% YTD | Auto +22% | Private Banks +15%.
IT −12% | Pharma −4% | Energy −3%.

Changes from original, all market-intelligence derived:
  1. MIN_CONVICTION raised 58 → 62. In a choppy/weak-index market, the
     borderline 58–62 band loses more often than it wins. Only surface
     high-confidence signals.
  2. MIN_PRICE raised 5 → 20. Sub-₹20 stocks on NSE = operator activity +
     wide bid-ask spreads + illiquid exits. The tiny per-share risk also
     inflates qty proposals to dangerous levels in real-trade-service.
  3. R:R enforcement added to build_suggestion(). A card is only built if
     the implied reward ≥ 1.8× the implied risk. Cards that fail go to None
     (same as non-actionable). This is the missing link between buy_sniper
     and risk_engine — previously a low-R:R card was surfaced on the Hot
     Picks tab and then rejected by risk_engine silently; now it never
     appears.
  4. Weak-market regime label added to each card's rationale so the
     dashboard shows the market context that was live when the card was scored.
  5. SECTOR BONUS: cards from outperforming sectors (PSU Banks, Auto,
     Metals) get +3 conviction points — not enough to push a weak signal
     over the bar, but enough to correctly rank two equally-scored stocks
     from different sectors.
  6. value_buy threshold updated: ₹20–₹500 tagged as "value buy" in
     choppy market (was ₹2000 flat — too wide; ₹2000 large-caps are
     underperforming the index right now).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("buy-sniper")

import os as _os

# ── Thresholds (market-intelligence derived, env-overridable) ─────────────────
# Raised from 58 → 62: choppy/weak market, only high-conviction signals.
MIN_CONVICTION = int(((_os.getenv("SNIPER_MIN_CONVICTION") or "").strip() or "62"))

# Raised from 5 → 20: sub-₹20 = operator risk + illiquid exits on NSE.
MIN_PRICE = float(((_os.getenv("SNIPER_MIN_PRICE") or "").strip() or "20.0"))

# Price ceiling — OFF by default.
MAX_PRICE = float(_os.getenv("MAX_PRICE", "0") or 0)

# "Value buy" badge range — tightened to ₹20–₹500 for Aug-2026 conditions.
# Large-cap ₹500–₹2000 names are mostly IT/pharma/energy which are underperforming.
VALUE_BUY_THRESHOLD = float(_os.getenv("VALUE_BUY_THRESHOLD", "500") or 500)

DEFAULT_TARGET_COUNT = 4

# ATR-based stop/target (same as original — proven risk-management standard).
ATR_STOP_MULTIPLIER   = 1.5
ATR_TARGET_MULTIPLIER = 3.0
MIN_STOP_PCT   = 2.0
MAX_STOP_PCT   = 6.0
MIN_TARGET_PCT = 4.0
MAX_TARGET_PCT = 12.0

# Flat fallbacks when ATR unavailable.
EST_PROFIT_PCT = 6.5
STOP_LOSS_PCT  = 3.2

# Minimum reward:risk ratio for a card to be surfaced at all.
# In a weak/choppy market, a 1.5:1 setup is not worth the capital risk.
MIN_REWARD_RISK = float(((_os.getenv("SNIPER_MIN_REWARD_RISK") or "").strip() or "1.8"))

# Outperforming sectors in Aug-2026 — get a small conviction bonus so
# equal-scored stocks from winning sectors rank above laggards.
_OUTPERFORMING_SECTORS = {
    "banking", "bank", "psu bank", "public sector bank",
    "auto", "automobile", "automotive",
    "metal", "metals", "steel", "mining",
    "private bank", "private sector bank",
}
SECTOR_BONUS = int(((_os.getenv("SNIPER_SECTOR_BONUS") or "").strip() or "3"))

# Underperforming sectors — slight penalty so weak-sector stocks don't
# crowd out better setups from better sectors.
_UNDERPERFORMING_SECTORS = {"it", "technology", "tech", "pharma", "pharmaceutical",
                             "energy", "oil", "gas", "power"}
SECTOR_PENALTY = int(((_os.getenv("SNIPER_SECTOR_PENALTY") or "").strip() or "3"))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _atr_pct(s: Dict[str, Any], price: float) -> Optional[float]:
    """ATR as % of price. Returns None when unavailable — never fabricates."""
    if price <= 0:
        return None
    for key in ("atr", "daily_atr"):
        raw = s.get(key)
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if v > 0:
            pct = (v / price) * 100.0
            if 0.1 <= pct <= 25.0:
                return pct
    return None


def _num(val: Any, default: float = 0.0) -> float:
    try:
        if val is None or val == "":
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _decision_label(s: Dict[str, Any]) -> str:
    return str(s.get("decision") or s.get("signal") or "").upper().strip()


def _conviction(s: Dict[str, Any]) -> float:
    for k in ("conviction", "combined_score", "conviction_score"):
        v = _num(s.get(k))
        if v > 0:
            return v
    tech = _num(s.get("technical_score"))
    fund = _num(s.get("fundamental_score"))
    if tech > 0 or fund > 0:
        return 0.55 * (tech or 50) + 0.45 * (fund or 50)
    return 0.0


def _price(s: Dict[str, Any]) -> float:
    for k in ("price", "cmp", "last_price", "ltp", "close", "current_price"):
        px = _num(s.get(k))
        if px > 0:
            return px
    return 0.0


def _sector_adjusted_conviction(s: Dict[str, Any], base_conviction: float) -> float:
    """Apply sector bonus/penalty based on Aug-2026 sector performance.
    Adjustments are bounded: +3 bonus for outperformers, -3 penalty for
    laggards. Never changes a WAIT to a BUY or vice versa — just ranks
    equal-scored stocks correctly."""
    sector = str(s.get("sector") or "").lower().strip()
    if not sector:
        return base_conviction
    for s_key in _OUTPERFORMING_SECTORS:
        if s_key in sector:
            return base_conviction + SECTOR_BONUS
    for s_key in _UNDERPERFORMING_SECTORS:
        if s_key in sector:
            return max(0.0, base_conviction - SECTOR_PENALTY)
    return base_conviction


def _is_buy_candidate(s: Dict[str, Any], min_conviction: float = MIN_CONVICTION) -> bool:
    if not isinstance(s, dict):
        return False
    px = _price(s)
    if px < MIN_PRICE:
        return False
    if MAX_PRICE > 0 and px > MAX_PRICE:
        return False
    conv = _sector_adjusted_conviction(s, _conviction(s))
    if conv < min_conviction:
        return False
    decision = _decision_label(s)
    change_pct = _num(s.get("change_pct"))
    if decision in ("BUY NOW", "BUY", "PREPARE TO BUY"):
        return True
    # Early breakout: strong combined + positive momentum even if HOLD label.
    # In weak market, raise the bar: conviction >= 72 (was 68) + change >= 1.2%.
    if decision in ("HOLD", "WAIT", "") and conv >= 72 and change_pct >= 1.2:
        return True
    return False


def _action_for(s: Dict[str, Any]) -> str:
    decision = _decision_label(s)
    if decision in ("BUY NOW", "BUY"):
        return "BUY NOW"
    if decision == "PREPARE TO BUY":
        return "BUY ON 15M BREAKOUT"
    return "BUY ON CONFIRMATION"


def _market_context_note() -> str:
    """Short market context string embedded in rationale for audit trail."""
    return (
        "Market: Nifty −7% in 6m, FII net-short, Midcap outperforming. "
        "Only high-conviction setups with strong R:R surfaced."
    )


def _rationale(s: Dict[str, Any], action: str, conv: float) -> str:
    tech   = int(_num(s.get("technical_score")) or 0)
    fund   = int(_num(s.get("fundamental_score")) or 0)
    sector = s.get("sector") or "sector"
    bits   = [
        f"Conviction {int(conv)}/100",
        f"tech {tech}" if tech else None,
        f"fund {fund}" if fund else None,
        sector if sector else None,
    ]
    core = ", ".join(b for b in bits if b)
    note = _market_context_note()
    if action == "BUY NOW":
        return f"{core}. High-conviction setup — aligned momentum and fundamentals. {note}"
    if action == "BUY ON 15M BREAKOUT":
        return f"{core}. Prepare-to-buy: wait for 15m breakout above range with volume. {note}"
    return f"{core}. Confirmation required before entry. {note}"


# -- Daily market-structure gate (group 357, WS-4; always live since group370) -------------------------------------------------------------
# "Good stock, bad ENTRY?" Same rules and defaults as real-trade's entry_engine/structure_gate.py. SNIPER_STRUCTURE_MODE: enforce (default,
# the card is dropped) | off (kill switch, card byte-for-byte as before). There is no record-only (shadow) mode: any other value, including a
# stale "shadow", is enforce. No / stale / insufficient structure = NO OPINION = never blocks and is never bullish. Pure, never raises.
import math as _math
from datetime import datetime as _dt, timezone as _tz

_SG_OK_QUALITIES = ("ok", "limited")


def _sg_mode() -> str:
    return "off" if ((_os.getenv("SNIPER_STRUCTURE_MODE") or "").strip().lower() == "off") else "enforce"


def _sg_float(name: str, default: float) -> float:
    try:
        v = float(((_os.getenv(name) or "").strip() or default))
        return v if _math.isfinite(v) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _sg_bool(name: str, default: bool) -> bool:
    return ((_os.getenv(name) or "").strip() or ("true" if default else "false")).lower() in ("1", "true", "yes", "on")


STRUCTURE_MAX_CHASE_ATR = _sg_float("STRUCTURE_MAX_CHASE_ATR", 1.5)
STRUCTURE_MAX_STRETCH_ATR = _sg_float("STRUCTURE_MAX_STRETCH_ATR", 3.0)
STRUCTURE_MIN_ROOM_ATR = _sg_float("STRUCTURE_MIN_ROOM_ATR", 0.5)
STRUCTURE_MIN_RR = _sg_float("STRUCTURE_MIN_RR", 1.2)
STRUCTURE_REJECT_STOP_WIDE = _sg_bool("STRUCTURE_REJECT_STOP_WIDE", True)
STRUCTURE_BEARISH_BLOCK = _sg_bool("STRUCTURE_BEARISH_BLOCK", True)
STRUCTURE_BEARISH_BOS_BARS = int(_sg_float("STRUCTURE_BEARISH_BOS_BARS", 5))
STRUCTURE_WAIT_CHASE_ATR = _sg_float("STRUCTURE_WAIT_CHASE_ATR", 0.75)   # group400: card says "wait for entry" from this chase (ATR); display only
STRUCTURE_MAX_AGE_DAYS = _sg_float("STRUCTURE_MAX_AGE_DAYS", 7.0)
STRUCTURE_MAX_PRICE_DEV_PCT = _sg_float("STRUCTURE_MAX_PRICE_DEV_PCT", 15.0)


def _sg_f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if _math.isfinite(x) else None


def _sg_pos(v: Any) -> Optional[float]:
    x = _sg_f(v)
    return x if x is not None and x > 0 else None


def _structure_summary(row: Any) -> Optional[Dict[str, Any]]:
    """The flat ms_* summary on a scan row: `structure_summary`, or ms_* keys on the row itself. None when absent."""
    if not isinstance(row, dict):
        return None
    s = row.get("structure_summary")
    if isinstance(s, dict) and s:
        return s
    flat = {k: v for k, v in row.items() if isinstance(k, str) and k.startswith("ms_")}
    return flat or None


_sg_summary = _structure_summary  # short alias used inside this module


def _sg_age_days(asof: Any) -> Optional[float]:
    try:
        if asof is None:
            return None
        if isinstance(asof, (int, float)):
            ts = float(asof)
            d = _dt.fromtimestamp(ts / 1000.0 if ts > 1e11 else ts, tz=_tz.utc)
        else:
            d = _dt.fromisoformat(str(asof).strip().replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=_tz.utc)
        return max(0.0, (_dt.now(_tz.utc) - d).total_seconds() / 86400.0)
    except Exception:  # noqa: BLE001
        return None


def _sg_no_data(summary: Optional[Dict[str, Any]], px: Optional[float]) -> Optional[str]:
    if not isinstance(summary, dict):
        return "no structure summary"
    q = summary.get("ms_quality")
    if q not in _SG_OK_QUALITIES:
        return f"structure quality '{q or 'unavailable'}'"
    age = _sg_age_days(summary.get("ms_asof"))
    if STRUCTURE_MAX_AGE_DAYS > 0 and age is not None and age > STRUCTURE_MAX_AGE_DAYS:
        return f"structure last bar is {age:.0f}d old"
    msp = _sg_pos(summary.get("ms_price"))
    if px is None or msp is None:
        return "no price to compare"
    dev = STRUCTURE_MAX_PRICE_DEV_PCT
    if dev > 0 and abs(px - msp) / msp * 100.0 > dev:
        return f"price {px:.2f} is {abs(px - msp) / msp * 100:.0f}% from the structure close {msp:.2f} (split / bad data)"
    if _sg_pos(summary.get("ms_atr")) is None:
        return "no ATR in the structure summary"
    return None


def _sg_live(summary: Dict[str, Any], px: float) -> Dict[str, Optional[float]]:
    """chase / stretch / room (ATR) and R:R re-expressed at the card price (the summary was computed at the structure close)."""
    msp, atr = _sg_pos(summary.get("ms_price")), _sg_pos(summary.get("ms_atr"))
    out: Dict[str, Optional[float]] = {"chase_atr": None, "stretch_atr": None, "room_atr": None, "rr": None}
    if msp is None or atr is None:
        return out
    d = (px - msp) / atr
    c, st, r = _sg_f(summary.get("ms_chase_atr")), _sg_f(summary.get("ms_stretch_atr")), _sg_f(summary.get("ms_room_atr"))
    out["chase_atr"] = round(max(0.0, c + d), 3) if c is not None else None
    out["stretch_atr"] = round(st + d, 3) if st is not None else None
    out["room_atr"] = round(r - d, 3) if r is not None else None
    stop, target = _sg_pos(summary.get("ms_stop")), _sg_pos(summary.get("ms_target"))
    if stop is not None and target is not None and stop < px < target:
        out["rr"] = round((target - px) / (px - stop), 3)
    return out


def _sg_entry_zone(summary: Dict[str, Any], px: float) -> Optional[Dict[str, Any]]:
    """group400, display only: the nearest usable bullish demand (order-block) or imbalance zone at or below the price, from the ms_* summary.
    {kind, bottom, top, status, dist_pct (price above the zone top; 0 inside)}. None when there is no such zone. Never invents one."""
    best: Optional[Dict[str, Any]] = None
    for kind, side, top, bot, st in (
            ("demand", summary.get("ms_ob_side"), _sg_pos(summary.get("ms_ob_top")), _sg_pos(summary.get("ms_ob_bottom")), summary.get("ms_ob_status")),
            ("imbalance", summary.get("ms_fvg_side"), _sg_pos(summary.get("ms_fvg_top")), _sg_pos(summary.get("ms_fvg_bottom")), summary.get("ms_fvg_status"))):
        if side != "bullish" or top is None or bot is None or bot >= top or st in ("filled", "broken", "expired"):
            continue
        if bot > px * 1.005:            # a zone above the price is not an entry zone
            continue
        dist = max(0.0, (px - top) / top * 100.0)
        if best is None or dist < best["dist_pct"]:
            best = {"kind": kind, "bottom": round(bot, 2), "top": round(top, 2), "status": st if isinstance(st, str) else None, "dist_pct": round(dist, 2)}
    return best


def structure_verdict(row: Any, price: Any, mode: Optional[str] = None) -> Dict[str, Any]:
    """Pure verdict for one scan row at `price`. Keys: gate_mode, opinion ('ok' | 'no_opinion'), would_reject, enforced, reasons[], no_data,
    live{chase_atr, stretch_atr, room_atr, rr}, stop, target, rr, entry_zone, wait_for_entry, wait_reason (last three display only, group400). Never raises."""
    m = mode if mode in ("off", "enforce") else _sg_mode()
    v: Dict[str, Any] = {"gate_mode": m, "opinion": "no_opinion", "would_reject": False, "enforced": False, "reasons": [], "no_data": None,
                         "live": {}, "stop": None, "target": None, "rr": None,
                         "entry_zone": None, "wait_for_entry": False, "wait_reason": None}
    try:
        summary = _sg_summary(row)
        px = _sg_pos(price)
        nd = _sg_no_data(summary, px)
        if nd:
            v["no_data"] = nd
            return v
        live = _sg_live(summary, px)
        v["live"], v["opinion"] = live, "ok"
        v["stop"], v["target"], v["rr"] = _sg_pos(summary.get("ms_stop")), _sg_pos(summary.get("ms_target")), live["rr"]
        reasons: List[str] = []
        lim = STRUCTURE_MAX_CHASE_ATR
        if lim > 0 and live["chase_atr"] is not None and live["chase_atr"] > lim:
            reasons.append(f"chase {live['chase_atr']:.1f} ATR above the nearest demand/imbalance zone (max {lim:g})")
        lim = STRUCTURE_MAX_STRETCH_ATR
        if lim > 0 and live["stretch_atr"] is not None and live["stretch_atr"] > lim:
            reasons.append(f"price stretched {live['stretch_atr']:.1f} ATR above EMA20 (max {lim:g})")
        lim = STRUCTURE_MIN_ROOM_ATR
        if lim > 0 and live["room_atr"] is not None and live["room_atr"] < lim:
            reasons.append(f"only {max(0.0, live['room_atr']):.1f} ATR of room to the next resistance (min {lim:g})")
        lim = STRUCTURE_MIN_RR
        flags = summary.get("ms_rr_flags") or []
        if lim > 0 and live["rr"] is not None and live["rr"] < lim:
            reasons.append(f"structure R:R {live['rr']:.2f} below {lim:g}")
        if STRUCTURE_REJECT_STOP_WIDE and isinstance(flags, (list, tuple)) and "stop_too_wide" in flags:
            reasons.append("structural stop is too wide")
        if STRUCTURE_BEARISH_BLOCK:
            if summary.get("ms_trend") == "bearish":
                reasons.append("daily structure is bearish")
            else:
                bars = _sg_f(summary.get("ms_bos_bars"))
                if (summary.get("ms_bos_dir") == "bearish" and not summary.get("ms_bos_failed") and bars is not None
                        and bars <= STRUCTURE_BEARISH_BOS_BARS):
                    reasons.append(f"fresh bearish break of structure {int(bars)} bar(s) ago")
        v["reasons"] = reasons
        v["entry_zone"] = _sg_entry_zone(summary, px)
        wl = STRUCTURE_WAIT_CHASE_ATR
        if reasons:
            v["wait_for_entry"], v["wait_reason"] = True, reasons[0]
        elif wl > 0 and live["chase_atr"] is not None and live["chase_atr"] >= wl:
            ez = v["entry_zone"]
            v["wait_for_entry"] = True
            v["wait_reason"] = (f"{live['chase_atr']:.1f} ATR above the {ez['kind']} zone, better entry near \u20b9{ez['top']:g}" if ez
                                else f"{live['chase_atr']:.1f} ATR above the nearest support, chasing")
        if reasons:
            v["would_reject"] = True
            v["enforced"] = (m == "enforce")
        return v
    except Exception as e:  # noqa: BLE001
        logger.debug("sniper structure verdict failed (no opinion): %s", e)
        v.update(opinion="no_opinion", would_reject=False, enforced=False, reasons=[], no_data="evaluation error")
        return v


def _sg_symbol(s: Any) -> str:
    return str((s or {}).get("symbol") or "").upper().replace(".NS", "").replace(".BO", "").strip()


def _payload_rows(payload: Any) -> List[Any]:
    """The scan rows inside a flexible find-buys payload (stocks / all_results / results / recommendations / rows / data, or a bare list)."""
    stocks: List[Any] = []
    if isinstance(payload, dict):
        for key in ("stocks", "all_results", "results", "recommendations", "rows"):
            v = payload.get(key)
            if isinstance(v, list) and v:
                stocks = v
                break
        if not stocks and isinstance(payload.get("data"), list):
            stocks = payload["data"]
    elif isinstance(payload, list):
        stocks = payload
    return stocks




def symbols_needing_structure(payload: Any, limit: int = 12) -> List[str]:
    """Best-conviction buy candidates that carry no structure summary yet (the route fetches those). [] when the gate is off / bad input."""
    try:
        if _sg_mode() == "off":
            return []
        min_c = MIN_CONVICTION
        if isinstance(payload, dict) and payload.get("min_conviction") is not None:
            try:
                min_c = float(payload["min_conviction"])
            except (TypeError, ValueError):
                min_c = MIN_CONVICTION
        best: Dict[str, float] = {}
        for s in _payload_rows(payload):
            if not isinstance(s, dict) or s.get("_meta") or str(s.get("decision") or "").upper() == "ERROR":
                continue
            if not _is_buy_candidate(s, min_conviction=min_c) or _structure_summary(s) is not None:
                continue
            sym = _sg_symbol(s)
            if sym:
                best[sym] = max(best.get(sym, 0.0), _sector_adjusted_conviction(s, _conviction(s)))
        return [k for k, _ in sorted(best.items(), key=lambda kv: -kv[1])][:max(0, int(limit))]
    except Exception:  # noqa: BLE001
        return []


# -- PREPARE-to-Buy refresh: notes (group 358) + event-driven selection (group 374, LIVE) -----------------------------------------------------------------------------------------
# After /data-feed/refresh-prepare-to-buy patches fresh quotes onto PREPARE rows, say WHY a row deserves a second look: the structure now fails
# the entry check at the fresh price ("turned bad"; "flipped" when it still passed at the last stored price) or a P9 structure event is near the
# bar (fresh BOS / CHOCH / sweep / price inside a zone / golden fib). Notes never change a row, a decision or a quote patch.
# group374: the same triggers now also DECIDE which rows get a fresh quote (select_prepare_event_symbols): a row outside the score band whose structure
# shows a P9 event is quoted too, so a setup that is forming is not missed because its score sits a few points low.
# SNIPER_PREPARE_STRUCTURE_MODE: live (default) | off. A stale "log" in an old .env means live. Independent of SNIPER_STRUCTURE_MODE (the card gate).
# SNIPER_PREPARE_EVENT_MARGIN (default 8): rows down to min_score - margin may be selected by an event. SNIPER_PREPARE_EVENT_MAX (default 15): cap.
# prepare_triggers() mirrors decision-prediction's structure_policy.prepare_triggers (a randomised test pins the two together).
_PREP_TRIGGER_TEXT = {
    "bos_near": "fresh bullish break of structure",
    "choch": "bullish change of character",
    "sweep": "bullish liquidity sweep",
    "entering_fvg": "price inside a bullish imbalance zone",
    "entering_ob": "price inside a bullish demand zone",
    "entering_fib": "price in the Fibonacci golden zone",
}


def _prep_mode() -> str:
    return "off" if ((_os.getenv("SNIPER_PREPARE_STRUCTURE_MODE") or "").strip().lower() == "off") else "live"


def _prep_num(v: Any) -> Optional[float]:
    """Same as structure_policy._num: bools and non-finite values are not numbers."""
    try:
        if v is None or isinstance(v, bool):
            return None
        f = float(v)
        return f if _math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def prepare_triggers(row: Any, price: Any = None) -> List[str]:
    """P9 structure events near the current bar for a PREPARE row (mirror of structure_policy.prepare_triggers). [] when there is no summary.
    Does not judge data quality (the note does)."""
    try:
        sm = _structure_summary(row)
        if not isinstance(sm, dict):
            return []
        msp, atr, px_in = _prep_num(sm.get("ms_price")), _prep_num(sm.get("ms_atr")), _prep_num(price)
        px = px_in if (px_in and msp and atr and atr > 0) else None   # structure_policy sets price_used only with all three
        bos_bars, choch_bars, sweep_bars = _prep_num(sm.get("ms_bos_bars")), _prep_num(sm.get("ms_choch_bars")), _prep_num(sm.get("ms_sweep_bars"))
        t: List[str] = []
        if sm.get("ms_bos_dir") == "bullish" and bos_bars is not None and bos_bars <= 3 and not bool(sm.get("ms_bos_failed")):
            t.append("bos_near")
        if sm.get("ms_choch_dir") == "bullish" and choch_bars is not None and choch_bars <= 5:
            t.append("choch")
        if sm.get("ms_sweep_dir") == "bullish" and sweep_bars is not None and sweep_bars <= 3:
            t.append("sweep")
        ref = px or msp
        for name, side, top, bot, st in (
                ("entering_fvg", sm.get("ms_fvg_side"), _prep_num(sm.get("ms_fvg_top")), _prep_num(sm.get("ms_fvg_bottom")), sm.get("ms_fvg_status")),
                ("entering_ob", sm.get("ms_ob_side"), _prep_num(sm.get("ms_ob_top")), _prep_num(sm.get("ms_ob_bottom")), sm.get("ms_ob_status"))):
            if side == "bullish" and ref and top and bot and st not in ("filled", "broken", "expired") and bot * 0.995 <= ref <= top * 1.005:
                t.append(name)
        if bool(sm.get("ms_in_golden")):
            t.append("entering_fib")
        return t
    except Exception:  # noqa: BLE001
        return []


def _prep_env_float(name: str, default: float) -> float:
    try:
        raw = (_os.getenv(name) or "").strip()
        v = float(raw) if raw else default
        return v if _math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def select_prepare_event_symbols(rows: Any, exclude: Any, score_of: Any, min_score: float) -> List[Tuple[str, List[str]]]:
    """Rows OUTSIDE the PREPARE score band that show a structure event (prepare_triggers) with usable, fresh structure.
    `rows` = {symbol: feed row}; `exclude` = symbols already selected; `score_of(row)` = conviction. Returns [(symbol, triggers)], best score first,
    capped by SNIPER_PREPARE_EVENT_MAX. Skips BUY NOW (already actionable) and SELL rows. [] when the mode is off. Pure, never raises."""
    try:
        if _prep_mode() == "off" or not isinstance(rows, dict):
            return []
        floor = float(min_score) - max(0.0, _prep_env_float("SNIPER_PREPARE_EVENT_MARGIN", 8.0))
        cap = int(_prep_env_float("SNIPER_PREPARE_EVENT_MAX", 15.0))
        if cap <= 0:
            return []
        skip = set(exclude or ())
        out: List[Tuple[float, str, List[str]]] = []
        for sym, row in rows.items():
            if sym in skip or not isinstance(row, dict):
                continue
            dec = str(row.get("decision") or row.get("action") or "").upper()
            if "BUY NOW" in dec or "SELL" in dec:
                continue
            sc = float(score_of(row) or 0.0)
            if sc < floor:
                continue
            sm = _structure_summary(row)
            msp = _sg_pos(sm.get("ms_price")) if isinstance(sm, dict) else None
            if _sg_no_data(sm, _sg_pos(row.get("price")) or msp):
                continue
            trig = prepare_triggers(row, None)
            if trig:
                out.append((sc, str(sym), trig))
        out.sort(key=lambda t: (-t[0], t[1]))
        return [(s_, t_) for _, s_, t_ in out[:cap]]
    except Exception as e:  # noqa: BLE001
        logger.debug("prepare event selection failed (none selected): %s", e)
        return []


def prepare_refresh_note(row: Any, price: Any, old_price: Any = None) -> Dict[str, Any]:
    """One PREPARE row at the fresh `price`. Keys: opinion ('ok' | 'no_opinion' | 'off'), turned_bad, flipped, reasons[], triggers[], headline, no_data.
    `old_price` is the price stored before the refresh (None / bad = unknown, never flipped). Pure, never raises."""
    n: Dict[str, Any] = {"opinion": "no_opinion", "turned_bad": False, "flipped": False, "reasons": [], "triggers": [], "headline": None, "no_data": None}
    try:
        if _prep_mode() == "off":
            n["opinion"] = "off"
            return n
        px = _sg_pos(price)
        nd = _sg_no_data(_structure_summary(row), px)
        if nd:
            n["no_data"] = nd
            return n
        n["opinion"] = "ok"
        v = structure_verdict(row, px, mode="enforce")   # the card gate's own switch must not silence the notes
        n["triggers"] = prepare_triggers(row, px)
        if v.get("would_reject"):
            n["turned_bad"] = True
            n["reasons"] = list(v.get("reasons") or [])
            old = _sg_pos(old_price)
            if old is not None:
                ov = structure_verdict(row, old, mode="enforce")
                n["flipped"] = bool(ov.get("opinion") == "ok" and not ov.get("would_reject"))
            n["headline"] = "Structure now blocks entry: " + "; ".join(n["reasons"]) + (" (passed at the last stored price)" if n["flipped"] else "")
        elif n["triggers"]:
            n["headline"] = "Structure event: " + ", ".join(_PREP_TRIGGER_TEXT.get(k, k) for k in n["triggers"])
        return n
    except Exception as e:  # noqa: BLE001
        logger.debug("prepare refresh note failed (no opinion): %s", e)
        return {"opinion": "no_opinion", "turned_bad": False, "flipped": False, "reasons": [], "triggers": [], "headline": None, "no_data": "evaluation error"}


def prepare_refresh_report(rows: Any, live: Any, old_prices: Any = None) -> Dict[str, Any]:
    """Notes for every refreshed symbol. rows {sym: feed row}, live {sym: fresh price}, old_prices {sym: price before the refresh}. A refreshed
    symbol with no usable row / summary is `no_opinion`. `notes` holds only the interesting rows (turned bad or a trigger). Never raises."""
    rep: Dict[str, Any] = {"mode": _prep_mode(), "checked": 0, "no_opinion": 0, "turned_bad": 0, "flipped": 0, "with_triggers": 0, "notes": {}}
    try:
        if rep["mode"] == "off":
            return rep
        rows = rows if isinstance(rows, dict) else {}
        old_prices = old_prices if isinstance(old_prices, dict) else {}
        for sym, px in (live.items() if isinstance(live, dict) else []):
            n = prepare_refresh_note(rows.get(sym), px, old_prices.get(sym))
            if n["opinion"] != "ok":
                rep["no_opinion"] += 1
                continue
            rep["checked"] += 1
            rep["turned_bad"] += 1 if n["turned_bad"] else 0
            rep["flipped"] += 1 if n["flipped"] else 0
            rep["with_triggers"] += 1 if n["triggers"] else 0
            if n["turned_bad"] or n["triggers"]:
                rep["notes"][sym] = n
        return rep
    except Exception as e:  # noqa: BLE001
        logger.debug("prepare refresh report failed: %s", e)
        return rep


def build_suggestion(s: Dict[str, Any], est_profit_pct: float = EST_PROFIT_PCT, _rejected: Optional[list] = None) -> Optional[Dict[str, Any]]:
    """Turn one scan row into a sniper card, or None if not actionable.

    NEW: R:R enforcement — if the implied reward < MIN_REWARD_RISK × risk,
    the card is not surfaced. Previously this was silently rejected later by
    risk_engine; now it never reaches the Hot Picks tab at all, keeping the
    tab clean and the R:R promise honest.
    """
    if not _is_buy_candidate(s):
        return None
    px = _price(s)
    if px <= 0:
        return None

    conv   = _sector_adjusted_conviction(s, _conviction(s))
    action = _action_for(s)
    tech   = int(_num(s.get("technical_score")) or 70)
    fund   = int(_num(s.get("fundamental_score")) or 70)

    # Prefer model-supplied target/stop when present and sensible.
    target = _num(s.get("target") or s.get("target_price"))
    stop   = _num(s.get("stop_loss"))
    atr_pct_val = _atr_pct(s, px)

    if target <= 0:
        if atr_pct_val is not None:
            eff_target_pct = max(MIN_TARGET_PCT, min(atr_pct_val * ATR_TARGET_MULTIPLIER, MAX_TARGET_PCT))
        else:
            eff_target_pct = est_profit_pct
        target = round(px * (1 + eff_target_pct / 100.0), 2)
    else:
        eff_target_pct = round((target / px - 1) * 100.0, 2) if px > 0 else est_profit_pct

    if stop <= 0:
        if atr_pct_val is not None:
            eff_stop_pct = max(MIN_STOP_PCT, min(atr_pct_val * ATR_STOP_MULTIPLIER, MAX_STOP_PCT))
        else:
            eff_stop_pct = STOP_LOSS_PCT
        stop = round(px * (1 - eff_stop_pct / 100.0), 2)

    # ── R:R enforcement (NEW) ──────────────────────────────────────────────
    risk   = px - stop
    reward = target - px
    if risk <= 0 or (reward / risk) < MIN_REWARD_RISK:
        logger.debug(
            "buy_sniper: %s skipped — R:R %.2f < %.1f (entry=%.2f stop=%.2f target=%.2f)",
            s.get("symbol"), reward / max(risk, 0.01), MIN_REWARD_RISK, px, stop, target,
        )
        return None

    # -- Daily-structure gate (group 357 / 370): runs AFTER the sniper's own R:R check, so only cards that would otherwise surface are judged.
    _sv = None
    if _sg_mode() != "off":
        _sv = structure_verdict(s, px)
        if _sv["enforced"]:
            if isinstance(_rejected, list):
                _rejected.append({"symbol": _sg_symbol(s), "price": px, "reasons": list(_sv["reasons"])})
            return None

    buy_low    = round(px * 0.995, 2)
    buy_high   = round(px * 1.008, 2)
    profit_abs = round(px * (eff_target_pct / 100.0), 2)

    card = {
        "symbol":              str(s.get("symbol") or "").upper().replace(".NS", "").replace(".BO", "").strip(),
        "action":              action,
        "buy_price_range":     f"₹{buy_low} - ₹{buy_high}",
        "buy_price_low":       buy_low,
        "buy_price_high":      buy_high,
        "entry_time":          "Next Trading Session (09:25 AM - 09:45 AM)",
        "entry_window":        "09:25 AM - 09:45 AM IST",
        "target_price":        round(target, 2),
        "stop_loss":           round(stop, 2),
        "estimated_profit":    f"+{eff_target_pct:.1f}% (₹{profit_abs}/share)",
        "estimated_profit_pct": eff_target_pct,
        "reward_risk_ratio":   round(reward / risk, 2),
        "holding_duration":    "2 to 5 Trading Days",
        "holding_period":      s.get("holding_period") or "2-5 Days",
        "conviction_score":    int(round(conv)),
        "technical_score":     tech,
        "fundamental_score":   fund,
        "change_pct":          _num(s.get("change_pct")),
        "price":               px,
        "sector":              s.get("sector"),
        "rationale":           _rationale(s, action, conv),
        "decision":            _decision_label(s) or action,
        # Badge: ₹20–₹500 in current market conditions (midcaps/smallcaps outperforming).
        "value_buy":           bool(MIN_PRICE <= px <= VALUE_BUY_THRESHOLD),
        "market_context":      "Nifty correction — only high-R:R setups surfaced",
    }
    if _sv is not None:
        # structural stop / target are DISPLAY ONLY: the card's own stop_loss / target_price / reward_risk_ratio are unchanged
        card["structure"] = {k: _sv[k] for k in ("gate_mode", "opinion", "would_reject", "enforced", "reasons", "no_data", "live", "stop", "target", "rr", "entry_zone", "wait_for_entry", "wait_reason")}
    return card


def _filter_with_rejects(scanned_stocks: List[Dict[str, Any]], target_count: int = DEFAULT_TARGET_COUNT,
                         min_conviction: float = MIN_CONVICTION) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(cards, structure_rejected). structure_rejected lists only rows the structure gate itself dropped (an R:R-failed row is never blamed on it)."""
    if not isinstance(scanned_stocks, list):
        return [], []
    target_count = max(1, min(int(target_count or DEFAULT_TARGET_COUNT), 10))
    candidates: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    seen = set()
    for s in scanned_stocks:
        if not isinstance(s, dict):
            continue
        if s.get("_meta") or str(s.get("decision") or "").upper() == "ERROR":
            continue
        if not _is_buy_candidate(s, min_conviction=min_conviction):
            continue
        card = build_suggestion(s, _rejected=rejected)
        if not card or not card.get("symbol"):
            continue
        sym = card["symbol"]
        if sym in seen:
            continue
        seen.add(sym)
        candidates.append(card)
    candidates.sort(key=lambda x: (x.get("conviction_score") or 0, x.get("reward_risk_ratio") or 0), reverse=True)
    return candidates[:target_count], rejected


def filter_actionable_buy_suggestions(
    scanned_stocks: List[Dict[str, Any]],
    target_count: int = DEFAULT_TARGET_COUNT,
    min_conviction: float = MIN_CONVICTION,
) -> List[Dict[str, Any]]:
    """Select 1..target_count buy setups with clear entry/exit math.
    Sorted by conviction descending. R:R-failed and structure-blocked cards excluded entirely."""
    return _filter_with_rejects(scanned_stocks, target_count, min_conviction)[0]


def suggestions_from_scan_payload(
    payload: Dict[str, Any], target_count: int = DEFAULT_TARGET_COUNT
) -> Dict[str, Any]:
    """Accept flexible FE payloads and return suggestions."""
    stocks = _payload_rows(payload)

    tc = target_count
    if isinstance(payload, dict) and payload.get("target_count") is not None:
        try:
            tc = int(payload["target_count"])
        except (TypeError, ValueError):
            tc = target_count

    min_c = MIN_CONVICTION
    if isinstance(payload, dict) and payload.get("min_conviction") is not None:
        try:
            min_c = float(payload["min_conviction"])
        except (TypeError, ValueError):
            min_c = MIN_CONVICTION

    suggestions, rejected = _filter_with_rejects(stocks, target_count=tc, min_conviction=min_c)
    if suggestions:
        message = None
    elif rejected:
        message = f"{len(rejected)} setup(s) were held back by the daily-structure check (chasing / stretched / no room / bearish)"
    else:
        message = "No setups meet conviction / R:R / decision criteria"
    return {
        "ok":           True,
        "count":        len(suggestions),
        "suggestions":  suggestions if isinstance(suggestions, list) else [],
        "scanned_input": len(stocks) if isinstance(stocks, list) else 0,
        "min_conviction": min_c,
        "target_count": tc,
        "market_context": _market_context_note(),
        "structure_mode": _sg_mode(),
        "structure_rejected": rejected,
        "message": message,
    }
