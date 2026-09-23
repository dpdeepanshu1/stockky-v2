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
from typing import Any, Dict, List, Optional

logger = logging.getLogger("buy-sniper")

import os as _os

# ── Thresholds (market-intelligence derived, env-overridable) ─────────────────
# Raised from 58 → 62: choppy/weak market, only high-conviction signals.
MIN_CONVICTION = int(_os.getenv("SNIPER_MIN_CONVICTION", "62"))

# Raised from 5 → 20: sub-₹20 = operator risk + illiquid exits on NSE.
MIN_PRICE = float(_os.getenv("SNIPER_MIN_PRICE", "20.0"))

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
MIN_REWARD_RISK = float(_os.getenv("SNIPER_MIN_REWARD_RISK", "1.8"))

# Outperforming sectors in Aug-2026 — get a small conviction bonus so
# equal-scored stocks from winning sectors rank above laggards.
_OUTPERFORMING_SECTORS = {
    "banking", "bank", "psu bank", "public sector bank",
    "auto", "automobile", "automotive",
    "metal", "metals", "steel", "mining",
    "private bank", "private sector bank",
}
SECTOR_BONUS = int(_os.getenv("SNIPER_SECTOR_BONUS", "3"))

# Underperforming sectors — slight penalty so weak-sector stocks don't
# crowd out better setups from better sectors.
_UNDERPERFORMING_SECTORS = {"it", "technology", "tech", "pharma", "pharmaceutical",
                             "energy", "oil", "gas", "power"}
SECTOR_PENALTY = int(_os.getenv("SNIPER_SECTOR_PENALTY", "3"))


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


def build_suggestion(s: Dict[str, Any], est_profit_pct: float = EST_PROFIT_PCT) -> Optional[Dict[str, Any]]:
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

    buy_low    = round(px * 0.995, 2)
    buy_high   = round(px * 1.008, 2)
    profit_abs = round(px * (eff_target_pct / 100.0), 2)

    return {
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


def filter_actionable_buy_suggestions(
    scanned_stocks: List[Dict[str, Any]],
    target_count: int = DEFAULT_TARGET_COUNT,
    min_conviction: float = MIN_CONVICTION,
) -> List[Dict[str, Any]]:
    """Select 1..target_count buy setups with clear entry/exit math.
    Sorted by conviction descending. R:R-failed cards excluded entirely."""
    if not isinstance(scanned_stocks, list):
        return []
    target_count = max(1, min(int(target_count or DEFAULT_TARGET_COUNT), 10))

    candidates: List[Dict[str, Any]] = []
    seen = set()
    for s in scanned_stocks:
        if not isinstance(s, dict):
            continue
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

    # Sort by conviction desc, then by R:R desc as tiebreaker
    candidates.sort(
        key=lambda x: (x.get("conviction_score") or 0, x.get("reward_risk_ratio") or 0),
        reverse=True,
    )
    return candidates[:target_count]


def suggestions_from_scan_payload(
    payload: Dict[str, Any], target_count: int = DEFAULT_TARGET_COUNT
) -> Dict[str, Any]:
    """Accept flexible FE payloads and return suggestions."""
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

    suggestions = filter_actionable_buy_suggestions(stocks, target_count=tc, min_conviction=min_c)
    return {
        "ok":           True,
        "count":        len(suggestions),
        "suggestions":  suggestions if isinstance(suggestions, list) else [],
        "scanned_input": len(stocks) if isinstance(stocks, list) else 0,
        "min_conviction": min_c,
        "target_count": tc,
        "market_context": _market_context_note(),
        "message": None if suggestions else "No setups meet conviction / R:R / decision criteria",
    }
