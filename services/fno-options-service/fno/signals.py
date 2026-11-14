"""fno/signals.py - direction (CE or PE) and contract choice. Pure: bars and a chain in, a decision with its reasons out. No clock, no network.

DIRECTION = a score in [-100, +100] (positive = buy a CALL, negative = buy a PUT):
    daily structure     the shared market-structure engine (stockky_ms.analyze) on the INDEX candles: trend, fresh BOS / CHOCH      up to +/-50
    daily trend         price vs EMA20, EMA20 vs EMA50                                                                            up to +/-25
    intraday            price vs session VWAP, opening-range break, EMA9 vs EMA21 on the intraday bars                            up to +/-25
    chain               PCR, OI walls, OI added near ATM (analytics.chain_score)                                                  up to +/-30 (added last)
The sum is clamped to [-100, 100]. |score| >= min_score gives a direction; otherwise FLAT. A missing input adds nothing: absent data is never good news.

CONTRACT = among the strikes around ATM on the chosen side, the one whose |delta| is closest to the target (0.52 by default) after the liquidity filters
(two-sided quote, spread, premium, volume, open interest). Delta comes from Dhan; if it is missing it is computed from the IV with Black-Scholes.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import analytics, bs
from .models import Chain, ContractRef, Side
from .params import Params

UP, DOWN, FLAT = "UP", "DOWN", "FLAT"


# ── candles ────────────────────────────────────────────────────────────────────────────────────────────────
def _c(x: Any, key: str, idx: int) -> Optional[float]:
    try:
        v = x[key] if isinstance(x, dict) else x[idx]
        f = float(v)
        return f if f == f else None
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def to_ms_bars(candles: Sequence[Any]) -> List[List[float]]:
    """[{"t","o","h","l","c","v"}] (or [t,o,h,l,c,v] lists) -> the [t,o,h,l,c,v] rows the market-structure engine reads. Bad rows are dropped."""
    out: List[List[float]] = []
    for x in candles or []:
        t, o, h, l, c = (_c(x, "t", 0), _c(x, "o", 1), _c(x, "h", 2), _c(x, "l", 3), _c(x, "c", 4))
        if None in (t, o, h, l, c):
            continue
        v = _c(x, "v", 5) or 0.0
        out.append([t, o, h, l, c, v])
    out.sort(key=lambda r: r[0])
    return out


def ema(values: Sequence[float], n: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (n + 1.0)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(out[-1] + k * (float(v) - out[-1]))
    return out


def _ms_block(bars: List[List[float]]) -> Optional[Dict[str, Any]]:
    try:
        from stockky_ms.engine import analyze
        blk = analyze(bars)
    except Exception:  # noqa: BLE001 - the engine is optional here: without it the other inputs still work
        return None
    return blk if isinstance(blk, dict) and blk.get("quality") in ("ok", "limited") else None


# ── direction ──────────────────────────────────────────────────────────────────────────────────────────────
def daily_score(daily: Sequence[Any]) -> Tuple[float, List[str], Optional[float]]:
    """(score in [-75, 75], reasons, daily ATR) from daily index candles."""
    bars = to_ms_bars(daily)
    score = 0.0
    why: List[str] = []
    atr: Optional[float] = None
    if len(bars) < 30:
        return 0.0, ["too few daily candles"], None
    blk = _ms_block(bars)
    if blk:
        atr = blk.get("atr") if isinstance(blk.get("atr"), (int, float)) else (blk.get("atr") or {}).get("value") if isinstance(blk.get("atr"), dict) else None
        trend = blk.get("trend")
        if trend == "bullish":
            score += 25; why.append("daily structure bullish")
        elif trend == "bearish":
            score -= 25; why.append("daily structure bearish")
        for name, w in (("bos", 15.0), ("choch", 10.0)):
            ev = blk.get(name) or {}
            if ev.get("direction") and ev.get("bars_since") is not None and ev["bars_since"] <= 5 and not ev.get("failed"):
                sgn = 1 if ev["direction"] in ("bullish", "up", 1) else -1
                score += sgn * w
                why.append(f"fresh {name.upper()} {'up' if sgn > 0 else 'down'} ({ev['bars_since']} bars ago)")
    closes = [b[4] for b in bars]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    if closes[-1] > e20[-1]:
        score += 10; why.append("close above EMA20")
    else:
        score -= 10; why.append("close below EMA20")
    if e20[-1] > e50[-1]:
        score += 15; why.append("EMA20 above EMA50")
    else:
        score -= 15; why.append("EMA20 below EMA50")
    if atr is None:                                  # simple 14-day ATR when the engine gave none
        trs = [max(b[2] - b[3], abs(b[2] - bars[i][4]), abs(b[3] - bars[i][4])) for i, b in enumerate(bars[1:])]
        atr = sum(trs[-14:]) / len(trs[-14:]) if trs else None
    return max(-75.0, min(75.0, score)), why, atr


def intraday_score(intraday: Sequence[Any]) -> Tuple[float, List[str]]:
    """(score in [-25, 25], reasons) from today's intraday index candles (5 or 15 minute)."""
    bars = to_ms_bars(intraday)
    if bars:        # round 315: today's session only (the latest bar's IST date): a feed that also carries an earlier day must not blend two days into one VWAP / opening range
        ist = timezone(timedelta(hours=5, minutes=30))
        last_day = datetime.fromtimestamp(bars[-1][0], ist).date()
        bars = [b for b in bars if datetime.fromtimestamp(b[0], ist).date() == last_day]
    if len(bars) < 6:
        return 0.0, ["too few intraday candles"]
    score = 0.0
    why: List[str] = []
    px = bars[-1][4]
    vol = sum(b[5] for b in bars)
    vwap = (sum(((b[2] + b[3] + b[4]) / 3.0) * b[5] for b in bars) / vol) if vol > 0 else sum((b[2] + b[3] + b[4]) / 3.0 for b in bars) / len(bars)
    if px > vwap:
        score += 10; why.append("price above session VWAP")
    else:
        score -= 10; why.append("price below session VWAP")
    first = bars[:3]
    orh, orl = max(b[2] for b in first), min(b[3] for b in first)
    if px > orh:
        score += 8; why.append("above opening range high")
    elif px < orl:
        score -= 8; why.append("below opening range low")
    closes = [b[4] for b in bars]
    e9, e21 = ema(closes, 9), ema(closes, 21)
    if e9[-1] > e21[-1]:
        score += 7; why.append("EMA9 above EMA21 (intraday)")
    else:
        score -= 7; why.append("EMA9 below EMA21 (intraday)")
    return max(-25.0, min(25.0, score)), why


def combine_bias(d: float, dwhy: List[str], atr: Optional[float], i: float, iwhy: List[str], c: float, cwhy: List[str], p: Params) -> Dict[str, Any]:
    """The CORE direction from its three already-computed parts (daily, intraday, chain). index_bias and fno/strategies.py both end here, so they can never disagree."""
    total = max(-100.0, min(100.0, d + i + c))
    raw = UP if total >= p.min_score else (DOWN if total <= -p.min_score else FLAT)
    direction, reasons = raw, dwhy + iwhy + cwhy
    if raw != FLAT and p.core_intraday_agree and i * (1 if raw == UP else -1) < 0:
        direction = FLAT        # round 495 (E515): daily + chain alone must not buy a CALL while the intraday score says DOWN (or a PUT while it says UP). i == 0 (no vote / neutral) does not block: core_min_intraday_bars covers the missing case
        reasons = reasons + [f"intraday score {i:+.0f} is against the {'UP' if raw == UP else 'DOWN'} lean ({total:+.0f}): no CORE entry"]
    return {"direction": direction, "raw_direction": raw, "score": round(total, 1), "parts": {"daily": round(d, 1), "intraday": round(i, 1), "chain": round(c, 1)},
            "reasons": reasons, "atr": atr}


def index_bias(daily: Sequence[Any], intraday: Sequence[Any] = (), summary: Optional[Dict[str, Any]] = None, p: Optional[Params] = None) -> Dict[str, Any]:
    p = p or Params()
    d, dwhy, atr = daily_score(daily)
    i, iwhy = intraday_score(intraday) if intraday else (0.0, [])
    c, cwhy = analytics.chain_score(summary) if summary else (0.0, [])
    return combine_bias(d, dwhy, atr, i, iwhy, c, cwhy, p)


# ── contract choice ───────────────────────────────────────────────────────────────────────────────────────
def _delta_of(side: Side, spot: float, strike: float, opt_type: str, expiry: date, now: datetime, r: float) -> Optional[float]:
    if side.delta is not None:
        return abs(side.delta)
    if side.iv and side.iv > 0:
        t = bs.year_fraction(now, expiry)
        if t > 0:
            return abs(bs.greeks(spot, strike, t, r, side.iv / 100.0, opt_type)["delta"])
    return None


def pick_contract(chain: Chain, direction: str, now: datetime, p: Optional[Params] = None, lot_size: Optional[int] = None,
                  security_lookup: Optional[Any] = None) -> Dict[str, Any]:
    """Choose the CE (direction UP) or PE (DOWN) to buy. Returns {"ok", "contract", "premium", "delta", "spread_pct", "reasons", "considered"}.
    `security_lookup(underlying, expiry, strike, opt_type)` may return an object with security_id / lot_size / segment / tick_size (the scrip master)."""
    p = p or Params()
    opt = "CE" if direction == UP else ("PE" if direction == DOWN else "")
    res: Dict[str, Any] = {"ok": False, "contract": None, "premium": None, "delta": None, "spread_pct": None, "reasons": [], "considered": []}
    if not opt:
        res["reasons"].append("no direction")
        return res
    i = chain.atm_index()
    if i is None or chain.spot is None:
        res["reasons"].append("chain has no spot / strikes")
        return res
    try:
        expiry = date.fromisoformat(chain.expiry)
    except ValueError:
        res["reasons"].append("bad expiry")
        return res
    lo, hi = max(0, i - p.strikes_each_side), min(len(chain.rows), i + p.strikes_each_side + 1)
    cands: List[Tuple[float, Dict[str, Any]]] = []
    for row in chain.rows[lo:hi]:
        s = row.side(opt)
        info: Dict[str, Any] = {"strike": row.strike, "reject": None}
        if s is None or s.ltp is None:
            info["reject"] = "no price"
        elif s.bid is None or s.ask is None or s.bid <= 0 or s.ask < s.bid:
            info["reject"] = "no two-sided quote"
        elif s.spread_pct is not None and s.spread_pct > p.max_spread_pct:
            info["reject"] = f"spread {s.spread_pct:.1f}% > {p.max_spread_pct:g}%"
        elif s.ask < p.min_premium:
            info["reject"] = f"premium {s.ask:g} < {p.min_premium:g}"
        elif s.volume is None:                                          # round 299: absent data is never good news. Dhan's chain always carries volume and OI, so a missing one is a broken payload, not a liquid strike
            info["reject"] = "no volume data"
        elif s.volume < p.min_volume:
            info["reject"] = f"volume {s.volume:g} < {p.min_volume:g}"
        elif s.oi is None:
            info["reject"] = "no open-interest data"
        elif s.oi < p.min_oi:
            info["reject"] = f"OI {s.oi:g} < {p.min_oi:g}"
        else:
            d = _delta_of(s, chain.spot, row.strike, opt, expiry, now, p.risk_free_rate)
            info["delta"] = d
            if d is not None and not (p.delta_min <= d <= p.delta_max):
                info["reject"] = f"|delta| {d:.2f} outside {p.delta_min:g}-{p.delta_max:g}"
            else:
                miss = abs(d - p.target_delta) if d is not None else abs(row.strike - chain.spot) / max(chain.step() or 1.0, 1.0) * 0.1
                cands.append((miss + (s.spread_pct or 0.0) * 0.01, {"row": row, "side": s, "delta": d}))
        res["considered"].append(info)
    if not cands:
        res["reasons"].append("no strike passed the liquidity / delta filters")
        return res
    cands.sort(key=lambda x: x[0])
    best = cands[0][1]
    row, side = best["row"], best["side"]
    ref = security_lookup(chain.underlying, chain.expiry, row.strike, opt) if security_lookup else None
    lot = int(getattr(ref, "lot_size", 0) or lot_size or 0)
    if lot <= 0:
        res["reasons"].append("lot size unknown")
        return res
    sid = getattr(ref, "security_id", None) or side.security_id
    res.update(ok=True, premium=side.ask, delta=best["delta"], spread_pct=side.spread_pct,
               contract=ContractRef(underlying=chain.underlying, expiry=chain.expiry, strike=row.strike, opt_type=opt, lot_size=lot, security_id=sid,
                                    segment=getattr(ref, "segment", "") or ("BSE_FNO" if chain.underlying == "SENSEX" else "NSE_FNO"),
                                    tick_size=float(getattr(ref, "tick_size", 0.05) or 0.05)))
    return res
