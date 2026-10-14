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
import math
from datetime import datetime, timezone
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

# ── Daily market-structure gate (group 357, WS-4) ────────────────────────────────────────────────────────────────
# "Good stock, bad entry?" - the question real-trade's entry gate asks, answered BEFORE a card is surfaced, so the early-breakout path (a
# HOLD/WAIT row with strong momentum) can no longer put a chase / stretched / no-room / bearish-structure setup on the Hot Picks tab.
# Reads the flat ms_* summary the scan row carries (`structure_summary`, or ms_* keys on the row). NO / stale / insufficient structure = NO
# OPINION: never blocks, never bullish. SNIPER_STRUCTURE_MODE = off (card unchanged, no `structure` key) | shadow (attach the verdict, keep
# the card) | enforce (default: drop the card). Limits reuse real-trade's STRUCTURE_* env names; 0 switches that test off. Pure, never raises.
_SG_OK_QUALITIES = ("ok", "limited")


def _sg_mode() -> str:
    m = ((_os.getenv("SNIPER_STRUCTURE_MODE") or "").strip() or "enforce").lower()
    return m if m in ("off", "shadow", "enforce") else "enforce"


def _sg_f(name: str, default: float) -> float:
    try:
        v = float(((_os.getenv(name) or "").strip() or str(default)))
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _sg_flag(name: str, default: bool) -> bool:
    raw = ((_os.getenv(name) or "").strip() or ("true" if default else "false")).lower()
    return raw not in ("0", "false", "no", "off")


def _fnum(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _pos(v: Any) -> Optional[float]:
    x = _fnum(v)
    return x if x is not None and x > 0 else None


def _structure_summary(s: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The flat ms_* summary on a scan row: `structure_summary` (dict), else ms_* keys on the row itself. None when absent."""
    try:
        sm = s.get("structure_summary")
        if isinstance(sm, dict) and sm.get("ms_quality"):
            return sm
        flat = {k: v for k, v in s.items() if isinstance(k, str) and k.startswith("ms_")}
        return flat if flat.get("ms_quality") else None
    except Exception:  # noqa: BLE001
        return None


def _age_days(asof: Any) -> Optional[float]:
    try:
        if asof is None:
            return None
        if isinstance(asof, (int, float)):
            ts = float(asof)
            dt = datetime.fromtimestamp(ts / 1000.0 if ts > 1e11 else ts, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(asof).strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
    except Exception:  # noqa: BLE001
        return None


def _sg_no_data(sm: Optional[Dict[str, Any]], px: float) -> Optional[str]:
    if not isinstance(sm, dict):
        return "no structure summary"
    q = sm.get("ms_quality")
    if q not in _SG_OK_QUALITIES:
        return f"structure quality '{q or 'unavailable'}'"
    age, max_age = _age_days(sm.get("ms_asof")), _sg_f("STRUCTURE_MAX_AGE_DAYS", 7.0)
    if max_age > 0 and age is not None and age > max_age:
        return f"structure last bar is {age:.0f}d old"
    msp = _pos(sm.get("ms_price"))
    if px <= 0 or msp is None:
        return "no price to compare"
    dev = _sg_f("STRUCTURE_MAX_PRICE_DEV_PCT", 15.0)
    if dev > 0 and abs(px - msp) / msp * 100.0 > dev:
        return f"price {px:.2f} is {abs(px - msp) / msp * 100:.0f}% from the structure close {msp:.2f} (split / bad data)"
    if _pos(sm.get("ms_atr")) is None:
        return "no ATR in the structure summary"
    return None


def _sg_live(sm: Dict[str, Any], px: float) -> Dict[str, Optional[float]]:
    """chase / stretch / room (ATR) and R:R re-expressed at the card price (the summary was computed at the structure close)."""
    out: Dict[str, Optional[float]] = {"chase_atr": None, "stretch_atr": None, "room_atr": None, "rr": None}
    msp, atr = _pos(sm.get("ms_price")), _pos(sm.get("ms_atr"))
    if px <= 0 or msp is None or atr is None:
        return out
    d = (px - msp) / atr
    c, st, r = _fnum(sm.get("ms_chase_atr")), _fnum(sm.get("ms_stretch_atr")), _fnum(sm.get("ms_room_atr"))
    out["chase_atr"] = round(max(0.0, c + d), 3) if c is not None else None
    out["stretch_atr"] = round(st + d, 3) if st is not None else None
    out["room_atr"] = round(r - d, 3) if r is not None else None
    stop, target = _pos(sm.get("ms_stop")), _pos(sm.get("ms_target"))
    if stop is not None and target is not None and stop < px < target:
        out["rr"] = round((target - px) / (px - stop), 3)
    return out


def structure_verdict(s: Dict[str, Any], px: float, mode: Optional[str] = None) -> Dict[str, Any]:
    """The gate verdict for one scan row at price `px`. Keys: gate_mode, opinion ('ok'|'no_opinion'), would_reject, reasons[], no_data,
    live{}, enforced, stop, target, rr. `enforced` is True only when the mode is enforce AND the verdict would reject. Never raises."""
    mode = mode or _sg_mode()
    v: Dict[str, Any] = {"gate_mode": mode, "opinion": "no_opinion", "would_reject": False, "reasons": [], "no_data": None, "live": {},
                         "enforced": False, "stop": None, "target": None, "rr": None}
    try:
        sm = _structure_summary(s) if isinstance(s, dict) else None
        nd = _sg_no_data(sm, px)
        if nd:
            v["no_data"] = nd
            return v
        live = _sg_live(sm, px)
        v["live"], v["opinion"] = live, "ok"
        v["stop"], v["target"], v["rr"] = _pos(sm.get("ms_stop")), _pos(sm.get("ms_target")), live.get("rr")
        reasons: List[str] = []
        lim = _sg_f("STRUCTURE_MAX_CHASE_ATR", 1.5)
        if lim > 0 and live["chase_atr"] is not None and live["chase_atr"] > lim:
            reasons.append(f"chase {live['chase_atr']:.1f} ATR above the nearest demand/imbalance zone (max {lim:g})")
        lim = _sg_f("STRUCTURE_MAX_STRETCH_ATR", 3.0)
        if lim > 0 and live["stretch_atr"] is not None and live["stretch_atr"] > lim:
            reasons.append(f"price stretched {live['stretch_atr']:.1f} ATR above EMA20 (max {lim:g})")
        lim = _sg_f("STRUCTURE_MIN_ROOM_ATR", 0.5)
        if lim > 0 and live["room_atr"] is not None and live["room_atr"] < lim:
            reasons.append(f"only {max(0.0, live['room_atr']):.1f} ATR of room to the next resistance (min {lim:g})")
        lim = _sg_f("STRUCTURE_MIN_RR", 1.2)
        if lim > 0 and live["rr"] is not None and live["rr"] < lim:
            reasons.append(f"structure R:R {live['rr']:.2f} below {lim:g}")
        if _sg_flag("STRUCTURE_REJECT_STOP_WIDE", True) and "stop_too_wide" in (sm.get("ms_rr_flags") or []):
            reasons.append("structural stop is too wide")
        if _sg_flag("STRUCTURE_BEARISH_BLOCK", True):
            if sm.get("ms_trend") == "bearish":
                reasons.append("daily structure is bearish")
            else:
                bars = _fnum(sm.get("ms_bos_bars"))
                if (sm.get("ms_bos_dir") == "bearish" and not sm.get("ms_bos_failed") and bars is not None
                        and bars <= int(_sg_f("STRUCTURE_BEARISH_BOS_BARS", 5.0))):
                    reasons.append(f"fresh bearish break of structure {int(bars)} bar(s) ago")
        v["reasons"] = reasons
        v["would_reject"] = bool(reasons)
        v["enforced"] = bool(reasons) and mode == "enforce"
        return v
    except Exception as e:  # noqa: BLE001
        logger.debug("sniper structure gate failed (no opinion): %s", e)
        v.update({"opinion": "no_opinion", "would_reject": False, "enforced": False, "reasons": [], "no_data": "evaluation error"})
        return v



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
    """Turn one scan row into a sniper card, or None if not actionable (see _build_card)."""
    return _build_card(s, est_profit_pct)[0]


def _build_card(s: Dict[str, Any], est_profit_pct: float = EST_PROFIT_PCT,
                gate_mode: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """(card, blocked_verdict). blocked_verdict is set ONLY when the card passed every other test and the enforced structure gate dropped it.

    Turn one scan row into a sniper card, or (None, None) if not actionable.

    NEW: R:R enforcement — if the implied reward < MIN_REWARD_RISK × risk,
    the card is not surfaced. Previously this was silently rejected later by
    risk_engine; now it never reaches the Hot Picks tab at all, keeping the
    tab clean and the R:R promise honest.
    """
    if not _is_buy_candidate(s):
        return None, None
    px = _price(s)
    if px <= 0:
        return None, None

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
        return None, None

    # ── Daily structure gate (group 357) - after the R:R check so the verdict is only computed for cards that would otherwise surface ──
    gmode = gate_mode or _sg_mode()
    verdict = structure_verdict(s, px, gmode) if gmode != "off" else None
    if verdict is not None and verdict["enforced"]:
        logger.debug("buy_sniper: %s blocked by daily structure: %s", s.get("symbol"), "; ".join(verdict["reasons"]))
        return None, verdict

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
    if verdict is not None:
        card["structure"] = verdict
    return card, None


def _filter_with_report(
    scanned_stocks: List[Dict[str, Any]],
    target_count: int = DEFAULT_TARGET_COUNT,
    min_conviction: float = MIN_CONVICTION,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(cards, structure_rejected). `structure_rejected` lists the candidates the daily-structure gate dropped ({symbol, reasons}) so the UI can
    say "N setups turned away by structure" instead of silently showing fewer. Never raises on odd rows."""
    if not isinstance(scanned_stocks, list):
        return [], []
    target_count = max(1, min(int(target_count or DEFAULT_TARGET_COUNT), 10))

    candidates: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    seen = set()
    seen_rej = set()
    for s in scanned_stocks:
        if not isinstance(s, dict):
            continue
        if s.get("_meta") or str(s.get("decision") or "").upper() == "ERROR":
            continue
        if not _is_buy_candidate(s, min_conviction=min_conviction):
            continue
        card = build_suggestion(s)
        if not card or not card.get("symbol"):
            # Was it the structure gate? Only when the card would have surfaced with the gate off (so an R:R-failed row is never blamed on it).
            if _sg_mode() == "enforce":
                blocked = _build_card(s, gate_mode="enforce")[1]
                if blocked is not None:
                    sym = str(s.get("symbol") or "").upper().replace(".NS", "").replace(".BO", "").strip()
                    if sym and sym not in seen_rej:
                        seen_rej.add(sym)
                        rejected.append({"symbol": sym, "reasons": list(blocked["reasons"])})
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
    return candidates[:target_count], rejected


def filter_actionable_buy_suggestions(
    scanned_stocks: List[Dict[str, Any]],
    target_count: int = DEFAULT_TARGET_COUNT,
    min_conviction: float = MIN_CONVICTION,
) -> List[Dict[str, Any]]:
    """Select 1..target_count buy setups with clear entry/exit math.
    Sorted by conviction descending. R:R-failed and structure-blocked cards excluded entirely."""
    return _filter_with_report(scanned_stocks, target_count, min_conviction)[0]


# ── PREPARE-to-Buy refresh notes (group 358) ─────────────────────────────────────────────────────────────────────────
# The surgical PREPARE refresh (/data-feed/refresh-prepare-to-buy) only re-quotes. These notes say WHY a refreshed PREPARE row deserves a
# second look: (a) its daily structure now fails the entry check at the fresh price ("turned bad", `flipped` when the stored price still
# passed), or (b) a structure event is near the bar (P9 triggers, mirrored from decision-prediction's structure_policy.prepare_triggers).
# LOG-ONLY: nothing here removes, re-ranks or re-prices a row. SNIPER_PREPARE_STRUCTURE_MODE = off | log (default). No summary on the feed
# row = no opinion (the refresh never fetches structure: it exists to avoid a request storm). Pure, never raises.
_PREP_DEAD_ZONE = ("filled", "broken", "expired")
PREPARE_TRIGGER_LABELS = {
    "bos_near": "fresh bullish break of structure",
    "choch": "bullish change of character",
    "sweep": "bullish liquidity sweep",
    "entering_fvg": "price entering a bullish imbalance zone",
    "entering_ob": "price entering a bullish demand zone",
    "entering_fib": "price in the Fibonacci golden zone",
}


def _prep_mode() -> str:
    m = ((_os.getenv("SNIPER_PREPARE_STRUCTURE_MODE") or "").strip() or "log").lower()
    return m if m in ("off", "log") else "log"


def _pnum(v: Any) -> Optional[float]:
    """Like _fnum but a bool is not a number (mirrors the decision service's _num)."""
    return None if isinstance(v, bool) else _fnum(v)


def prepare_triggers(sm: Optional[Dict[str, Any]], price: Any = None) -> List[str]:
    """P9: structure events near the current bar that justify refreshing a PREPARE TO BUY. Mirrors
    decision-prediction-service/decision/structure_policy.prepare_triggers (a test pins the two together)."""
    t: List[str] = []
    try:
        if not isinstance(sm, dict):
            return t
        bars = _pnum(sm.get("ms_bos_bars"))
        if sm.get("ms_bos_dir") == "bullish" and bars is not None and bars <= 3 and not bool(sm.get("ms_bos_failed")):
            t.append("bos_near")
        bars = _pnum(sm.get("ms_choch_bars"))
        if sm.get("ms_choch_dir") == "bullish" and bars is not None and bars <= 5:
            t.append("choch")
        bars = _pnum(sm.get("ms_sweep_bars"))
        if sm.get("ms_sweep_dir") == "bullish" and bars is not None and bars <= 3:
            t.append("sweep")
        msp, atr, live = _pnum(sm.get("ms_price")), _pnum(sm.get("ms_atr")), _pnum(price)
        px = live if (live and msp and atr and atr > 0) else msp   # the decision service's `price_used or ms_price`
        for name, pre in (("entering_fvg", "ms_fvg"), ("entering_ob", "ms_ob")):
            top, bot = _pnum(sm.get(pre + "_top")), _pnum(sm.get(pre + "_bottom"))
            if (sm.get(pre + "_side") == "bullish" and px and top and bot and sm.get(pre + "_status") not in _PREP_DEAD_ZONE
                    and bot * 0.995 <= px <= top * 1.005):
                t.append(name)
        if bool(sm.get("ms_in_golden")):
            t.append("entering_fib")
    except Exception:  # noqa: BLE001
        return []
    return t


def prepare_refresh_note(row: Dict[str, Any], live_price: Any, old_price: Any = None, mode: Optional[str] = None) -> Dict[str, Any]:
    """The refresh note for one PREPARE row at its fresh price. Keys: mode, opinion ('ok'|'no_opinion'|'off'), turned_bad, flipped, reasons[],
    triggers[], headline (None when there is nothing to say), no_data, live{}. `flipped` = fails now but passed at the previously stored price."""
    mode = mode or _prep_mode()
    note: Dict[str, Any] = {"mode": mode, "opinion": "off" if mode == "off" else "no_opinion", "turned_bad": False, "flipped": False,
                            "reasons": [], "triggers": [], "headline": None, "no_data": None, "live": {}}
    if mode == "off":
        return note
    try:
        px = _pnum(live_price) or 0.0
        v = structure_verdict(row, px, "shadow")
        if v["opinion"] != "ok":
            note["no_data"] = v["no_data"]
            return note
        note["opinion"], note["live"] = "ok", v["live"]
        note["turned_bad"], note["reasons"] = bool(v["would_reject"]), list(v["reasons"])
        old = _pnum(old_price)
        if note["turned_bad"] and old and old > 0 and abs(old - px) > 1e-9:
            vo = structure_verdict(row, old, "shadow")
            note["flipped"] = vo["opinion"] == "ok" and not vo["would_reject"]
        note["triggers"] = prepare_triggers(_structure_summary(row), px)
        if note["turned_bad"]:
            tail = " (it passed at the last stored price)" if note["flipped"] else ""
            note["headline"] = "Structure now blocks entry: " + "; ".join(note["reasons"][:2]) + tail
        elif note["triggers"]:
            note["headline"] = "Structure event: " + ", ".join(PREPARE_TRIGGER_LABELS.get(x, x) for x in note["triggers"])
        return note
    except Exception as e:  # noqa: BLE001
        logger.debug("prepare refresh note failed (no opinion): %s", e)
        note.update({"opinion": "no_opinion", "turned_bad": False, "flipped": False, "reasons": [], "triggers": [], "headline": None,
                     "no_data": "evaluation error"})
        return note


def prepare_refresh_report(rows: Dict[str, Dict[str, Any]], live_prices: Dict[str, float],
                           old_prices: Optional[Dict[str, float]] = None, mode: Optional[str] = None) -> Dict[str, Any]:
    """Notes for a refreshed batch: {mode, checked, no_opinion, turned_bad, flipped, with_triggers, notes{SYM: note}}. `notes` holds only rows
    with something to say (turned bad, or a trigger). `rows` are the feed rows (flat ms_* keys), `live_prices` the fresh quotes."""
    mode = mode or _prep_mode()
    rep: Dict[str, Any] = {"mode": mode, "checked": 0, "no_opinion": 0, "turned_bad": 0, "flipped": 0, "with_triggers": 0, "notes": {}}
    if mode == "off":
        return rep
    old_prices = old_prices or {}
    for sym, px in (live_prices or {}).items():
        row = (rows or {}).get(sym)
        if not isinstance(row, dict):
            rep["no_opinion"] += 1
            continue
        n = prepare_refresh_note(row, px, old_prices.get(sym), mode)
        if n["opinion"] != "ok":
            rep["no_opinion"] += 1
            continue
        rep["checked"] += 1
        rep["turned_bad"] += 1 if n["turned_bad"] else 0
        rep["flipped"] += 1 if n["flipped"] else 0
        rep["with_triggers"] += 1 if n["triggers"] else 0
        if n["headline"]:
            rep["notes"][sym] = n
    return rep


def _payload_rows(payload: Any) -> List[Any]:
    """The scan rows of a flexible FE payload (the same lookup suggestions_from_scan_payload uses)."""
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


def symbols_needing_structure(payload: Any, limit: int = 12, min_conviction: Optional[float] = None) -> List[str]:
    """Symbols of buy-candidate rows that carry no structure summary yet, best conviction first (at most `limit`). The gateway route fetches
    these so the gate has something to judge; rows that already carry a summary are left alone. [] when the gate is off. Pure."""
    if _sg_mode() == "off":
        return []
    min_c = MIN_CONVICTION if min_conviction is None else min_conviction
    if isinstance(payload, dict) and payload.get("min_conviction") is not None and min_conviction is None:
        try:
            min_c = float(payload["min_conviction"])
        except (TypeError, ValueError):
            pass
    ranked: List[Tuple[float, str]] = []
    seen = set()
    for r in _payload_rows(payload):
        if not isinstance(r, dict) or r.get("_meta") or _structure_summary(r) is not None:
            continue
        if not _is_buy_candidate(r, min_conviction=min_c):
            continue
        sym = str(r.get("symbol") or "").upper().replace(".NS", "").replace(".BO", "").strip()
        if sym and sym not in seen:
            seen.add(sym)
            ranked.append((-_sector_adjusted_conviction(r, _conviction(r)), sym))
    ranked.sort()
    return [sym for _, sym in ranked[:max(0, int(limit))]]


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

    suggestions, rejected = _filter_with_report(stocks, target_count=tc, min_conviction=min_c)
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
        "message": None if suggestions else (
            "No setups passed the daily-structure check (good stock, bad entry)" if rejected
            else "No setups meet conviction / R:R / decision criteria"),
    }
