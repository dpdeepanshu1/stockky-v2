"""entry_engine/setup_inputs.py - the facts S2 and S3-long need in Real Trade (E276 round 68, 1 Nov 2026).

setup_engine decides; this module only MEASURES. The measuring functions are the same ones Position Stocks runs (orders/setup_inputs.py), so a fact means the same thing
in both services. `gather_long` here is the Real Trade version: it takes what entry.py already holds (closed 1-minute bars, the Market Board body, the daily structure
summary, the book imbalance, the flow tape) and reads nothing itself, so it is pure; the caller does the awaits. A source that is down is a missing fact, and a missing
fact is never true. Nothing here places, sizes or refuses a trade.

Real Trade differences from Position Stocks:
  tape_exhaustion  the reliable flow tape (flow_json["tape"]) when it says selling is exhausted / absorbed, or else the 5-level book leaning to the bid
                   (imbalance_5 above +BOOK_BUY_IMBALANCE). The book is a resting-order snapshot, weaker evidence than a trade tape (the same stand-in S1 uses).
  liquid           the stock's normal daily turnover (prev_day.avg_turnover, 20 sessions) is at least SETUP_S3_LIQUID_MIN_TURNOVER_CR crore Rs. Real Trade has no
                   all-market turnover ranking, so the top-N rule of Position Stocks becomes a turnover floor. Unknown turnover = not liquid.
  wall_close       only when the tape carries an hvn_below node; the flow stamp does not, so it is normally false here.
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

PRICED_IN_ATR_MULT = 2.0
PULLBACK_TOUCH_PCT = 0.10
SPIKE_WINDOW_MIN = 30
SPIKE_MIN_RETURNS = 20
GAP_DOWN_MIN_PCT = 1.0
FAILURE_NEAR_LOW_PCT = 0.2
VWAP_STRETCH_ATR_FRAC = 0.25
VWAP_STRETCH_FALLBACK_PCT = 0.5
CLIMAX_VOL_MULT = 2.0
WALL_CLOSE_PCT = 0.3
SECTOR_MIN_STOCKS = 3
SECTOR_MIN_SECTORS = 3


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def closed_bars(bars: Optional[Sequence[Any]], now_ts: float) -> List[Tuple[float, float, float, float, float, float]]:
    """Closed 1-minute bars, oldest first; an unreadable row is skipped, never fatal."""
    out: List[Tuple[float, float, float, float, float, float]] = []
    for b in bars or ():
        try:
            t, o, h, l, c, v = (_num(b[i]) for i in range(6))
        except (TypeError, IndexError, KeyError):
            continue
        if None in (t, o, h, l, c) or t + 60.0 > now_ts or h < l:
            continue
        out.append((t, o, h, l, c, v if v is not None and v >= 0 else 0.0))
    out.sort(key=lambda r: r[0])
    return out


def candle_signal(rows: Sequence[Tuple[float, ...]]) -> int:
    """+1 hammer or bullish engulfing, -1 shooting star or bearish engulfing, 0 nothing, on the last row."""
    try:
        if not rows:
            return 0
        _, o, h, l, c, _v = rows[-1]
        rng = h - l
        if rng <= 0:
            return 0
        body, lower, upper = abs(c - o), min(o, c) - l, h - max(o, c)
        if lower >= 2.0 * body and lower >= 0.5 * rng and (c - l) / rng >= 0.6:
            return 1
        if upper >= 2.0 * body and upper >= 0.5 * rng and (h - c) / rng >= 0.6:
            return -1
        if len(rows) >= 2:
            _, po, _ph, _pl, pc, _pv = rows[-2]
            if pc < po and c > o and o <= pc and c >= po:
                return 1
            if pc > po and c < o and o >= pc and c <= po:
                return -1
        return 0
    except Exception:  # noqa: BLE001
        return 0


def vol_vs_norm(rows: Sequence[Tuple[float, ...]], min_prior: int = 5) -> Optional[float]:
    try:
        if len(rows) < min_prior + 1:
            return None
        prior = [r[5] for r in rows[:-1] if r[5] > 0]
        if len(prior) < min_prior:
            return None
        avg = sum(prior) / len(prior)
        return round(rows[-1][5] / avg, 3) if avg > 0 else None
    except Exception:  # noqa: BLE001
        return None


def pullback_hold(rows: Sequence[Tuple[float, ...]], or_high: Optional[float], range_end_ts: Optional[float]) -> bool:
    """A closed bar after the range broke above or_high, a later closed bar dipped back to it (low within PULLBACK_TOUCH_PCT) and the last closed bar closes above it."""
    try:
        if or_high is None or or_high <= 0 or range_end_ts is None:
            return False
        after = [r for r in rows if r[0] >= range_end_ts]
        brk = next((i for i, r in enumerate(after) if r[4] > or_high), None)
        if brk is None or brk >= len(after) - 1:
            return False
        touch = or_high * (1.0 + PULLBACK_TOUCH_PCT / 100.0)
        dipped = any(r[3] <= touch for r in after[brk + 1:])
        return bool(dipped and after[-1][4] > or_high)
    except Exception:  # noqa: BLE001
        return False


def _stdev(xs: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / n
    return math.sqrt(var) if var > 0 else None


def spike_z(rows: Sequence[Tuple[float, ...]], window: int = SPIKE_WINDOW_MIN, min_returns: int = SPIKE_MIN_RETURNS, sign: int = 0) -> Optional[Dict[str, Any]]:
    """The biggest close-to-close move inside the last `window` closed bars in the stock's own volatility. {"z", "k", "trough_idx", "peak_idx", "sigma"} or None.
    z < 0: a down-spike (peak close, then a lower trough close); z > 0: an up-spike. The larger |z| of the two is returned; `sign` +1 / -1 (E274 round 44) looks at up-spikes / down-spikes only."""
    try:
        closes = [r[4] for r in rows]
        if len(closes) < min_returns + 1:
            return None
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i] > 0 and closes[i - 1] > 0]
        sigma = _stdev(rets) if len(rets) >= min_returns else None
        if not sigma:
            return None
        start = max(0, len(closes) - window)
        best: Optional[Dict[str, Any]] = None
        for i in range(start, len(closes) - 1):
            for j in range(i + 1, len(closes)):
                if closes[i] <= 0:
                    continue
                move = math.log(closes[j] / closes[i])
                k = j - i
                z = move / (sigma * math.sqrt(k))
                if sign and z * sign <= 0:
                    continue
                if best is None or abs(z) > abs(best["z"]):
                    best = {"z": round(z, 3), "k": k, "peak_idx": i if z < 0 else j, "trough_idx": j if z < 0 else i, "sigma": sigma}
        return best
    except Exception:  # noqa: BLE001
        return None


def priced_in(price: Any, prev_close: Any, atr_pct: Any, sign: int = 1, mult: float = PRICED_IN_ATR_MULT) -> Optional[float]:
    p, pc, a = _num(price), _num(prev_close), _num(atr_pct)
    if not p or not pc or pc <= 0 or not a or a <= 0 or mult <= 0:
        return None
    move = (p - pc) / pc * 100.0 * sign
    return round(max(0.0, move) / (mult * a), 4)


def sector_third(payload: Any, sector: Optional[str]) -> Optional[str]:
    """top / mid / bottom third of the named sectors by average change today; None when it cannot be said."""
    try:
        if not isinstance(payload, dict) or not sector or str(sector) == "Other":
            return None
        rows = [s for s in (payload.get("sectors") or []) if isinstance(s, dict) and s.get("name") != "Other"
                and (_num(s.get("count")) or 0) >= SECTOR_MIN_STOCKS and _num(s.get("avg_change_pct")) is not None]
        if len(rows) < SECTOR_MIN_SECTORS:
            return None
        rows.sort(key=lambda s: -float(s["avg_change_pct"]))
        idx = next((i for i, s in enumerate(rows) if s.get("name") == sector), None)
        if idx is None:
            return None
        n = len(rows)
        return "top" if idx < n / 3.0 else ("bottom" if idx >= 2.0 * n / 3.0 else "mid")
    except Exception:  # noqa: BLE001
        return None


def rs_pct(payload: Any, symbol: str) -> Optional[float]:
    try:
        if not isinstance(payload, dict):
            return None
        sym = str(symbol or "").upper()
        for r in payload.get("bullish") or ():
            if isinstance(r, dict) and str(r.get("symbol", "")).upper() == sym:
                p = _num(r.get("percentile"))
                return None if p is None else max(0.0, min(1.0, p / 100.0))
        for r in payload.get("bearish") or ():
            if isinstance(r, dict) and str(r.get("symbol", "")).upper() == sym:
                return 0.0
        return None
    except Exception:  # noqa: BLE001
        return None


def structure_ok(summary: Any, usable_reason: Optional[str]) -> bool:
    """usable_reason = structure_gate.no_data_reason(summary, price) (None = usable). Bearish trend or a fresh unfailed bearish BOS = False."""
    try:
        if usable_reason is not None or not isinstance(summary, dict):
            return False
        if summary.get("ms_trend") == "bearish":
            return False
        bars = _num(summary.get("ms_bos_bars"))
        if summary.get("ms_bos_dir") == "bearish" and not summary.get("ms_bos_failed") and (bars is None or bars <= 5):
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def tape_exhaustion_long(tape: Any) -> bool:
    try:
        if not isinstance(tape, dict) or tape.get("reliable") is not True:
            return False
        checks = tape.get("checks") if isinstance(tape.get("checks"), dict) else {}
        return bool(tape.get("exhaustion") == "sell_exhaustion" or tape.get("absorption") == "selling_absorbed" or checks.get("imbalance") == "for")
    except Exception:  # noqa: BLE001
        return False


def wall_close_long(tape: Any, price: Any) -> bool:
    try:
        p, w = _num(price), _num((tape or {}).get("hvn_below"))
        if not p or not w or w <= 0:
            return False
        gap = (p / w - 1.0) * 100.0
        return 0.0 <= gap <= WALL_CLOSE_PCT
    except Exception:  # noqa: BLE001
        return False


def kind_long(gap_pct: Any, price: Any, day_open: Any) -> str:
    g, p, o = _num(gap_pct), _num(price), _num(day_open)
    if g is not None and g <= -GAP_DOWN_MIN_PCT:
        return "gap_down_reversal"
    if p is not None and o is not None and p < o:
        return "below_open_pullback"
    return "panic_dump"


def s3_facts_long(rows: Sequence[Tuple[float, ...]], *, price: Any, vwap: Any, atr_pct: Any, gap_pct: Any, day_open: Any, tape: Any, liquid: bool) -> Dict[str, Any]:
    """The S3 long facts from closed bars; {} when the window is too short."""
    sp = spike_z(rows)
    if not sp:
        return {}
    out: Dict[str, Any] = {"z_move": sp["z"], "kind": kind_long(gap_pct, price, day_open), "liquid": bool(liquid)}
    try:
        t = sp["trough_idx"]
        low = rows[t][3]
        recent_low = min(r[3] for r in rows[-3:])
        near = low > 0 and recent_low <= low * (1.0 + FAILURE_NEAR_LOW_PCT / 100.0)
        out["failure_candle"] = bool(candle_signal(rows) > 0 and near)
        out["tape_exhaustion"] = tape_exhaustion_long(tape)
        p, vw, a = _num(price), _num(vwap), _num(atr_pct)
        thr = VWAP_STRETCH_ATR_FRAC * a if a and a > 0 else VWAP_STRETCH_FALLBACK_PCT
        out["vwap_stretch"] = bool(p and vw and vw > 0 and (vw - p) / vw * 100.0 >= thr)
        mean_v = sum(r[5] for r in rows) / len(rows)
        around = [r[5] for r in rows[max(0, t - 1):t + 2]]
        out["climax_volume"] = bool(mean_v > 0 and around and max(around) >= CLIMAX_VOL_MULT * mean_v)
        out["wall_close"] = wall_close_long(tape, price)
    except Exception:  # noqa: BLE001
        pass
    return out


def s2_facts_long(rows: Sequence[Tuple[float, ...]], *, or_high: Any, range_end_ts: Optional[float], orb: Any, price: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        orb = orb if isinstance(orb, dict) else {}
        oh, p = _num(or_high), _num(price)
        out["or_break"] = 1 if (orb.get("close_trigger") is True and oh and p and p >= oh) else 0
        out["pullback_hold"] = pullback_hold(rows, oh, range_end_ts)
        v = vol_vs_norm(rows)
        if v is not None:
            out["vol_vs_norm"] = v
        out["candle"] = candle_signal(rows)
    except Exception:  # noqa: BLE001
        pass
    return out


def day_open_ts(now_ts: float) -> float:
    """09:15 IST of the day that contains now_ts, as an epoch."""
    return float(int((now_ts + 19800.0) // 86400) * 86400 - 19800 + 9 * 3600 + 15 * 60)


def range_end_for(now_ts: float) -> float:
    """The opening-range end (ORB_RANGE_END_IST, default 09:20) of today as an epoch."""
    hh, mm = 9, 20
    try:
        import config
        parts = str(getattr(config, "ORB_RANGE_END_IST", "09:20")).split(":")
        hh, mm = int(parts[0]), int(parts[1])
    except Exception:  # noqa: BLE001
        pass
    return float(int((now_ts + 19800.0) // 86400) * 86400 - 19800 + hh * 3600 + mm * 60)


BOOK_BUY_IMBALANCE = 0.15       # same stand-in threshold setup_live uses for S1's tape_buying
_CRORE = 1.0e7


def day_open_ts(now_ts: float) -> float:
    """09:15 IST of the day that contains now_ts, as an epoch."""
    return float(int((now_ts + 19800.0) // 86400) * 86400 - 19800 + 9 * 3600 + 15 * 60)


def liquid_by_turnover(avg_turnover: Any, min_cr: Any) -> bool:
    """Normal daily turnover (Rs) at least `min_cr` crore. Unknown = False."""
    t, m = _num(avg_turnover), _num(min_cr)
    return bool(t is not None and m is not None and m > 0 and t >= m * _CRORE)


def tape_exhaustion_rt(tape: Any, imbalance_5: Any) -> bool:
    """Selling exhausted / absorbed on the RELIABLE flow tape, or else a bid-heavy 5-level book."""
    if tape_exhaustion_long(tape):
        return True
    imb = _num(imbalance_5)
    return bool(imb is not None and imb > BOOK_BUY_IMBALANCE)


def gather_long(*, symbol: str, price: Any, now_ts: float, bars: Any, board_payload: Any, structure_summary: Any, structure_reason: Optional[str],
                prev_close: Any, atr_pct: Any, gap_pct: Any, day_open: Any, vwap: Any, orb: Any, tape: Any, imbalance_5: Any, avg_turnover: Any,
                sector: Optional[str] = None, sector_fallback: Optional[str] = None, liquid_min_cr: Any = None,
                want_s2: bool = True, want_s3: bool = True, range_end_ts: Optional[float] = None) -> Dict[str, Any]:
    """Everything setup_engine needs for S2 / S3-long (and S1's sector third) beyond what setup_live already derives. Pure, never raises.
    `orb` is orb_bars.range_trigger(bars) (or_high, bar_close, bar_closed_after_range); `structure_reason` is structure_gate.no_data_reason(summary, price)
    (None = usable); `sector_fallback` is a ready-made top / mid / bottom for when the board has too few named sectors. Each fact sits in its own try."""
    out: Dict[str, Any] = {}
    try:
        st = sector_third(board_payload, sector) or (sector_fallback if sector_fallback in ("top", "mid", "bottom") else None)
        if st:
            out["sector_third"] = st
    except Exception:  # noqa: BLE001
        pass
    try:
        pi = priced_in(price, prev_close, atr_pct, +1)
        if pi is not None:
            out["event_priced_in"] = pi
    except Exception:  # noqa: BLE001
        pass
    if not (want_s2 or want_s3):
        return out
    try:
        rows = closed_bars(bars, now_ts)
    except Exception:  # noqa: BLE001
        rows = []
    if range_end_ts is None:
        range_end_ts = range_end_for(now_ts)
    o = orb if isinstance(orb, dict) else {}
    if want_s2:
        try:
            r = rs_pct(board_payload, symbol)
            if r is not None:
                out["rs_pct"] = r
            out["structure_ok"] = structure_ok(structure_summary, structure_reason)
            oh = _num(o.get("or_high"))
            bc = _num(o.get("bar_close"))
            # a closed bar after the range closed above the range high AND the live price is still at or above it (the PS rule, from bars instead of the ORB record)
            brk = bool(oh and bc and bc > oh and o.get("bar_closed_after_range") is True and (_num(price) or 0.0) >= oh)
            out["or_break"] = 1 if brk else 0
            out["pullback_hold"] = pullback_hold(rows, oh, range_end_ts)
            v = vol_vs_norm(rows)
            if v is not None:
                out["vol_vs_norm"] = v
            out["candle"] = candle_signal(rows)
            g = _num(gap_pct)
            if g is not None:
                out["gap_pct"] = g
        except Exception:  # noqa: BLE001
            pass
    if want_s3:
        try:
            lq = liquid_by_turnover(avg_turnover, liquid_min_cr)
            d_open = day_open
            if _num(d_open) is None and rows and rows[0][0] - day_open_ts(now_ts) <= 180.0:
                d_open = rows[0][1]                       # the bars cover the session from the open: the first bar stands in for the day open
            f3 = s3_facts_long(rows, price=price, vwap=vwap, atr_pct=atr_pct, gap_pct=gap_pct, day_open=d_open, tape=tape, liquid=lq)
            if f3:
                f3["tape_exhaustion"] = tape_exhaustion_rt(tape, imbalance_5)
            out.update(f3)
        except Exception:  # noqa: BLE001
            pass
    return out
