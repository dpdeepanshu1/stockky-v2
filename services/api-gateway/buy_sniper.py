"""
Buy Sniper — select 1–4 high-conviction actionable buy setups from a scan result set.

Filters PREPARE TO BUY / BUY NOW (and high-conviction HOLD breakouts), then
attaches explicit entry window, buy range, target, stop-loss, and holding duration.

Used by:
  POST /api/scan/find-buys
  POST /scan/find-buys
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("buy-sniper")

# Default thresholds (override via kwargs if needed)
MIN_CONVICTION = 58
MIN_PRICE = 5.0
# Price gate — OFF by default (0 = no upper cap; full universe is eligible).
# Set MAX_PRICE in the environment for an explicit cap if you ever want one.
import os as _os
MAX_PRICE = float(_os.getenv("MAX_PRICE", "0") or 0)
# Display-only "value buy" tag threshold — never excludes a stock.
VALUE_BUY_THRESHOLD = float(_os.getenv("VALUE_BUY_THRESHOLD", "2000") or 2000)
DEFAULT_TARGET_COUNT = 4
EST_PROFIT_PCT = 6.5
STOP_LOSS_PCT = 3.2

# 2026-08-24: volatility-aware fallback target/stop.
#
# These are ONLY used when the decision-prediction-service (the trained
# model) didn't already return its own target/stop_loss for a row — see
# build_suggestion() below, which always prefers the model's numbers when
# present. This is a fallback-of-a-fallback, not a replacement for the
# model, and it is NOT a claim of a validated trading edge — it's a
# standard, well-established risk-management adjustment (ATR-scaled
# stop/target instead of one flat percentage for every stock).
#
# Why this matters right now: trailing-year NSE data (to 2026-08-14) shows
# Nifty 50/Sensex roughly flat-to-down (-1% to -3%) while Nifty Midcap 100
# was +12.88% and Smallcap 100 +12.49% — i.e. return AND realized
# volatility have diverged sharply between large caps and the small/mid-cap
# names that make up most of this app's ≤₹5000 universe. A flat 3.2%
# stop-loss that's appropriate for a large, low-beta stock is frequently
# too tight for a higher-ATR small-cap (stopped out on normal noise) and
# too loose for a genuinely low-volatility one (gives back more than the
# setup's actual risk). Scaling by the stock's own recent ATR% addresses
# that without inventing any new directional signal.
ATR_STOP_MULTIPLIER = 1.5   # stop ≈ 1.5x daily ATR%, a conventional default
ATR_TARGET_MULTIPLIER = 3.0  # ~2:1 reward:risk vs the ATR-based stop
MIN_STOP_PCT = 2.0    # floor: never tighter than 2% (avoid noise stop-outs)
MAX_STOP_PCT = 6.0    # ceiling: never looser than 6% on a ≤₹5000 setup
MIN_TARGET_PCT = 4.0
MAX_TARGET_PCT = 12.0


def _atr_pct(s: Dict[str, Any], price: float) -> Optional[float]:
    """ATR as a % of price, if the feed store had a usable atr/daily_atr
    field for this symbol. Returns None (caller falls back to the flat
    EST_PROFIT_PCT/STOP_LOSS_PCT constants) when no ATR is available —
    this never fabricates a volatility number that wasn't actually
    measured."""
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
            if 0.1 <= pct <= 25.0:  # sanity bounds — reject bad/stale feed data
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
    d = str(s.get("decision") or s.get("signal") or "").upper().strip()
    return d


def _conviction(s: Dict[str, Any]) -> float:
    for k in ("conviction", "combined_score", "conviction_score"):
        v = _num(s.get(k))
        if v > 0:
            return v
    # Derive from tech/fund if present
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


def _is_buy_candidate(s: Dict[str, Any], min_conviction: float = MIN_CONVICTION) -> bool:
    if not isinstance(s, dict):
        return False
    px = _price(s)
    if px < MIN_PRICE or (MAX_PRICE > 0 and px > MAX_PRICE):
        return False
    conv = _conviction(s)
    if conv < min_conviction:
        return False
    decision = _decision_label(s)
    change_pct = _num(s.get("change_pct"))

    if decision in ("BUY NOW", "BUY", "PREPARE TO BUY"):
        return True
    # Early breakout: strong combined + positive momentum even if label is HOLD
    if decision in ("HOLD", "WAIT", "") and conv >= 68 and change_pct >= 0.8:
        return True
    return False


def _action_for(s: Dict[str, Any]) -> str:
    decision = _decision_label(s)
    if decision in ("BUY NOW", "BUY"):
        return "BUY NOW"
    if decision == "PREPARE TO BUY":
        return "BUY ON 15M BREAKOUT"
    return "BUY ON CONFIRMATION"


def _rationale(s: Dict[str, Any], action: str, conv: float) -> str:
    tech = int(_num(s.get("technical_score")) or 0)
    fund = int(_num(s.get("fundamental_score")) or 0)
    sector = s.get("sector") or "sector"
    bits = [
        f"Conviction {int(conv)}/100",
        f"tech {tech}" if tech else None,
        f"fund {fund}" if fund else None,
        f"{sector}" if sector else None,
    ]
    core = ", ".join(b for b in bits if b)
    if action == "BUY NOW":
        return f"{core}. High-conviction setup — aligned momentum and fundamentals."
    if action == "BUY ON 15M BREAKOUT":
        return f"{core}. Prepare-to-buy: wait for 15m breakout above range with volume."
    return f"{core}. Confirmation required before entry."


def build_suggestion(s: Dict[str, Any], est_profit_pct: float = EST_PROFIT_PCT) -> Optional[Dict[str, Any]]:
    """Turn one scan row into a sniper card, or None if not actionable."""
    if not _is_buy_candidate(s):
        return None
    px = _price(s)
    if px <= 0:
        return None
    conv = _conviction(s)
    action = _action_for(s)
    tech = int(_num(s.get("technical_score")) or 70)
    fund = int(_num(s.get("fundamental_score")) or 70)
    # Prefer scanner target/stop when present and sensible — this is the
    # decision-prediction-service's own model output and always wins.
    target = _num(s.get("target") or s.get("target_price"))
    stop = _num(s.get("stop_loss"))
    atr_pct = _atr_pct(s, px)
    if target <= 0:
        if atr_pct is not None:
            eff_target_pct = max(MIN_TARGET_PCT, min(atr_pct * ATR_TARGET_MULTIPLIER, MAX_TARGET_PCT))
        else:
            eff_target_pct = est_profit_pct
        target = round(px * (1 + eff_target_pct / 100.0), 2)
    else:
        eff_target_pct = round((target / px - 1) * 100.0, 2) if px > 0 else est_profit_pct
    if stop <= 0:
        if atr_pct is not None:
            eff_stop_pct = max(MIN_STOP_PCT, min(atr_pct * ATR_STOP_MULTIPLIER, MAX_STOP_PCT))
        else:
            eff_stop_pct = STOP_LOSS_PCT
        stop = round(px * (1 - eff_stop_pct / 100.0), 2)

    buy_low = round(px * 0.995, 2)
    buy_high = round(px * 1.008, 2)
    profit_abs = round(px * (eff_target_pct / 100.0), 2)

    return {
        "symbol": str(s.get("symbol") or "").upper().replace(".NS", "").replace(".BO", "").strip(),
        "action": action,
        "buy_price_range": f"₹{buy_low} - ₹{buy_high}",
        "buy_price_low": buy_low,
        "buy_price_high": buy_high,
        "entry_time": "Next Trading Session (09:25 AM - 09:45 AM)",
        "entry_window": "09:25 AM - 09:45 AM IST",
        "target_price": round(target, 2),
        "stop_loss": round(stop, 2),
        "estimated_profit": f"+{eff_target_pct:.1f}% (₹{profit_abs}/share)",
        "estimated_profit_pct": eff_target_pct,
        "holding_duration": "2 to 5 Trading Days",
        "holding_period": s.get("holding_period") or "2-5 Days",
        "conviction_score": int(round(conv)),
        "technical_score": tech,
        "fundamental_score": fund,
        "change_pct": _num(s.get("change_pct")),
        "price": px,
        "sector": s.get("sector"),
        "rationale": _rationale(s, action, conv),
        "decision": _decision_label(s) or action,
        # Display-only — never excludes a stock, just badges cheap setups
        "value_buy": bool(0 < px <= VALUE_BUY_THRESHOLD),
    }


def filter_actionable_buy_suggestions(
    scanned_stocks: List[Dict[str, Any]],
    target_count: int = DEFAULT_TARGET_COUNT,
    min_conviction: float = MIN_CONVICTION,
) -> List[Dict[str, Any]]:
    """
    Select 1..target_count buy setups with clear entry/exit math.
    Sorted by conviction descending.
    """
    if not isinstance(scanned_stocks, list):
        return []
    target_count = max(1, min(int(target_count or DEFAULT_TARGET_COUNT), 10))

    candidates: List[Dict[str, Any]] = []
    seen = set()
    for s in scanned_stocks:
        if not isinstance(s, dict):
            continue
        # Skip pure meta / error rows
        if s.get("_meta") or str(s.get("decision") or "").upper() == "ERROR":
            continue
        if not _is_buy_candidate(s, min_conviction=min_conviction):
            continue
        card = build_suggestion(s)
        if not card or not card.get("symbol"):
            continue
        sym = card["symbol"]
        if sym in seen:
            continue
        seen.add(sym)
        candidates.append(card)

    candidates.sort(key=lambda x: (x.get("conviction_score") or 0), reverse=True)
    return candidates[:target_count]


def suggestions_from_scan_payload(payload: Dict[str, Any], target_count: int = DEFAULT_TARGET_COUNT) -> Dict[str, Any]:
    """
    Accept flexible FE payloads:
      { "stocks": [...] }
      { "all_results": [...] }
      { "results": [...] }
      { "recommendations": [...] }
    """
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

    # Coerce target_count from payload
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

    # Empty candidates = HTTP 200 success (modal shows "No setups meet criteria")
    suggestions = filter_actionable_buy_suggestions(stocks, target_count=tc, min_conviction=min_c)
    return {
        "ok": True,
        "count": len(suggestions),
        "suggestions": suggestions if isinstance(suggestions, list) else [],
        "scanned_input": len(stocks) if isinstance(stocks, list) else 0,
        "min_conviction": min_c,
        "target_count": tc,
        "message": None if suggestions else "No setups meet conviction / decision criteria",
    }
