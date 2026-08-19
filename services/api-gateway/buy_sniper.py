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
# Universal ≤ ₹5000 gate (align with instant_scanner / data_feed / bhavcopy)
MAX_PRICE = 5000.0
DEFAULT_TARGET_COUNT = 4
EST_PROFIT_PCT = 6.5
STOP_LOSS_PCT = 3.2


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
    if px < MIN_PRICE or px > MAX_PRICE:
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
    # Prefer scanner target/stop when present and sensible
    target = _num(s.get("target") or s.get("target_price"))
    stop = _num(s.get("stop_loss"))
    if target <= 0:
        target = round(px * (1 + est_profit_pct / 100.0), 2)
    if stop <= 0:
        stop = round(px * (1 - STOP_LOSS_PCT / 100.0), 2)

    buy_low = round(px * 0.995, 2)
    buy_high = round(px * 1.008, 2)
    profit_abs = round(px * (est_profit_pct / 100.0), 2)

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
        "estimated_profit": f"+{est_profit_pct}% (₹{profit_abs}/share)",
        "estimated_profit_pct": est_profit_pct,
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
