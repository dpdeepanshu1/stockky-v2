"""orders/setup_inputs.py - the facts S2, S3 and S1's extras need (E273 round 5, 31 Oct 2026). Position Stocks, LONG side.

setup_engine decides; this module only MEASURES. Every function is pure over the arguments it is given (bars, a board payload, a structure summary, a tape
verdict); `gather_long` is the one place that reads the live sources and every read is wrapped, so a source that is down gives a missing fact, and a missing
fact is never true. Nothing here places, sizes or refuses a trade.

Bars are the bar store's 1-minute tuples (ts, open, high, low, close, volume). A bar counts only once it has CLOSED (ts + 60 <= now).

What each fact means (all starting definitions, named constants, replaced by learned values once a setup cell has 30 trades):
  rs_pct        the board's side percentile / 100 (board rows rank by |move| / the stock's own average range, so it is strength in the stock's own units);
                a symbol on the board's bear side = 0.0, a symbol not on the board = None
  sector_third  the stock's sector among the named sectors with >= 3 stocks, ordered by average change today: top / mid / bottom third (fewer than 3 sectors = None)
  priced_in     the move since the previous close (in the event's direction) / (PRICED_IN_ATR_MULT x daily ATR %); above 1 = the news has already moved the price
  structure_ok  daily structure summary present, usable, trend not bearish and no fresh bearish break of structure
  candle        +1 hammer / bullish engulfing, -1 shooting star / bearish engulfing on the last CLOSED 1-minute bar
  vol_vs_norm   last closed bar volume / mean volume of today's earlier closed bars (the plan wants the same-minute norm; that history does not exist yet)
  pullback_hold a closed bar broke the opening range high earlier, a later closed bar dipped back to within PULLBACK_TOUCH_PCT of it and the last closed bar
                closes above it
  z_move        the largest move between two closes inside the last SPIKE_WINDOW_MIN bars, divided by (stdev of today's 1-minute returns x sqrt(bars it took)).
                Negative = a down-spike. Needs SPIKE_MIN_RETURNS returns
  kind          gap_down_reversal (pre-open gap <= -GAP_DOWN_MIN_PCT) | below_open_pullback (price under the day open) | panic_dump (otherwise)
  failure_candle  a hammer / bullish engulfing within 0.2 % of the spike's low
  tape_exhaustion the reliable tape shows selling exhausted or absorbed (sell_exhaustion, selling_absorbed, or the long imbalance check "for")
  vwap_stretch    price under VWAP by at least VWAP_STRETCH_ATR_FRAC x daily ATR % (0.5 % when ATR is unknown)
  climax_volume   the busiest bar around the spike's low is >= CLIMAX_VOL_MULT x the mean bar volume today
  wall_close      a high-volume node (support) sits within WALL_CLOSE_PCT under the price
  liquid          the symbol is inside the top BOARD_LIQUID_TOP_N by turnover
Nothing is guessed: a window that is too short, a zero deviation or an unreadable number returns None / False.
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


_liquid_cache: Dict[str, Any] = {"at": 0.0, "set": frozenset()}


def is_liquid(symbol: str, top_n: int, *, now: Optional[float] = None, ranked: Optional[Sequence[str]] = None) -> bool:
    """Inside the top `top_n` symbols by turnover (cached 60 s). `ranked` lets a test pass the ordered list."""
    try:
        t = time.monotonic() if now is None else now
        if ranked is not None:
            return str(symbol).upper() in {str(s).upper() for s in list(ranked)[:top_n]}
        if t - _liquid_cache["at"] > 60.0 or not _liquid_cache["set"]:
            from feed import bar_store
            syms = sorted(bar_store.symbols(), key=lambda s: bar_store.turnover(s), reverse=True)[:top_n]
            _liquid_cache.update(at=t, set=frozenset(str(s).upper() for s in syms))
        return str(symbol).upper() in _liquid_cache["set"]
    except Exception:  # noqa: BLE001
        return False


def gather_long(*, symbol: str, price: Any, now_ts: float, flow_json_orb: Any, quote: Any, gap_pct: Any, tape: Any, want_s2: bool, want_s3: bool,
                board_payload: Any = None, bars: Any = None, structure_summary: Any = None, prev_atr_pct: Any = None, range_end_ts: Optional[float] = None,
                liquid: Optional[bool] = None, top_n: int = 100, sector: Optional[str] = None) -> Dict[str, Any]:
    """Everything setup_engine needs beyond S1's old facts. Each source is read inside its own try, so one down source loses only the facts it feeds.
    The keyword arguments with a value skip the live read (that is how the tests run it)."""
    out: Dict[str, Any] = {}
    q = quote if isinstance(quote, dict) else {}
    pc = q.get("previous_close") if q.get("previous_close") is not None else q.get("prev_close")
    try:                                                         # S1 extras: sector third, priced_in
        payload = board_payload
        if payload is None:
            from screening import board
            payload = board.build(None, None, [])
        if sector is None:
            from feed import sector_map
            sector = sector_map.sector_of(symbol)
        st = sector_third(payload, sector)
        if not st:                                               # E272: before the board has 3 named sectors priced, the 08:30 to 09:20 plan's pre-open sector ranking stands in
            from screening import premarket_plan
            st = premarket_plan.sector_third(symbol, sector=sector)
        if st:
            out["sector_third"] = st
        board_payload = payload
    except Exception:  # noqa: BLE001
        pass
    try:
        atr = prev_atr_pct
        if atr is None:
            from screening import prev_day
            pdv = prev_day.peek(symbol)
            atr = getattr(pdv, "atr_pct", None) if pdv is not None else None
        try:
            import config as _cfg
            _mult = float(getattr(_cfg, "SETUP_PRICED_IN_ATR_MULT", PRICED_IN_ATR_MULT))
        except Exception:  # noqa: BLE001
            _mult = PRICED_IN_ATR_MULT
        pi = priced_in(price, pc, atr, +1, _mult)
        if pi is not None:
            out["event_priced_in"] = pi
        prev_atr_pct = atr
    except Exception:  # noqa: BLE001
        pass
    if not (want_s2 or want_s3):
        return out
    try:
        rows = closed_bars(bars if bars is not None else __import__("feed.bar_store", fromlist=["get_bars"]).get_bars(str(symbol).upper()), now_ts)
    except Exception:  # noqa: BLE001
        rows = []
    if range_end_ts is None:
        range_end_ts = range_end_for(now_ts)
    if want_s2:
        try:
            r = rs_pct(board_payload, symbol)
            if r is not None:
                out["rs_pct"] = r
            summ = structure_summary
            reason = None
            if summ is None:
                from orders import structure_gate
                summ = structure_gate.fetch_summary(symbol)
            from orders import structure_gate as _sg
            reason = _sg.no_data_reason(summ, price)
            out["structure_ok"] = structure_ok(summ, reason)
            orb = flow_json_orb if isinstance(flow_json_orb, dict) else {}
            out.update(s2_facts_long(rows, or_high=orb.get("range_high"), range_end_ts=range_end_ts, orb=orb, price=price))
            if gap_pct is not None:
                out["gap_pct"] = float(gap_pct)
        except Exception:  # noqa: BLE001
            pass
    if want_s3:
        try:
            lq = liquid if liquid is not None else is_liquid(symbol, top_n)
            d_open = q.get("open") if q.get("open") is not None else q.get("day_open")
            if _num(d_open) is None and rows and rows[0][0] - day_open_ts(now_ts) <= 180.0:
                d_open = rows[0][1]                                # the bar store saw the session from the open: its first bar stands in for the day open
            out.update(s3_facts_long(rows, price=price, vwap=q.get("avg_price"), atr_pct=prev_atr_pct, gap_pct=gap_pct, day_open=d_open, tape=tape, liquid=lq))
        except Exception:  # noqa: BLE001
            pass
    return out


# ---- E274 round 44: the SHORT mirrors of the S2 / S3 facts ----------------------------------------------------------------------------------------------------------------------
def pullback_hold_short(rows: Sequence[Tuple[float, ...]], or_low: Optional[float], range_end_ts: Optional[float]) -> bool:
    """Mirror of pullback_hold: a closed bar after the range closed below or_low, a later closed bar rallied back to it (high within PULLBACK_TOUCH_PCT) and the last closed bar closes below it."""
    try:
        if or_low is None or or_low <= 0 or range_end_ts is None:
            return False
        after = [r for r in rows if r[0] >= range_end_ts]
        brk = next((i for i, r in enumerate(after) if r[4] < or_low), None)
        if brk is None or brk >= len(after) - 1:
            return False
        touch = or_low * (1.0 - PULLBACK_TOUCH_PCT / 100.0)
        rallied = any(r[2] >= touch for r in after[brk + 1:])
        return bool(rallied and after[-1][4] < or_low)
    except Exception:  # noqa: BLE001
        return False


def structure_ok_short(summary: Any, usable_reason: Optional[str]) -> bool:
    """Mirror of structure_ok: usable summary, trend not bullish and no fresh unfailed bullish break of structure (within 5 bars)."""
    try:
        if usable_reason is not None or not isinstance(summary, dict):
            return False
        if summary.get("ms_trend") == "bullish":
            return False
        bars = _num(summary.get("ms_bos_bars"))
        if summary.get("ms_bos_dir") == "bullish" and not summary.get("ms_bos_failed") and (bars is None or bars <= 5):
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def tape_exhaustion_short(tape: Any) -> bool:
    """Buying exhausted or absorbed on the RELIABLE tape (buy_exhaustion, buying_absorbed, or the short's imbalance check \"for\")."""
    try:
        if not isinstance(tape, dict) or tape.get("reliable") is not True:
            return False
        checks = tape.get("checks") if isinstance(tape.get("checks"), dict) else {}
        return bool(tape.get("exhaustion") == "buy_exhaustion" or tape.get("absorption") == "buying_absorbed" or checks.get("imbalance") == "for")
    except Exception:  # noqa: BLE001
        return False


def wall_close_short(tape: Any, price: Any) -> bool:
    """A high-volume node (resistance, `hvn_above`) sits within WALL_CLOSE_PCT above the price."""
    try:
        p, w = _num(price), _num((tape or {}).get("hvn_above"))
        if not p or not w or w <= 0:
            return False
        gap = (w / p - 1.0) * 100.0
        return 0.0 <= gap <= WALL_CLOSE_PCT
    except Exception:  # noqa: BLE001
        return False


def s3_facts_short(rows: Sequence[Tuple[float, ...]], *, price: Any, vwap: Any, atr_pct: Any, tape: Any) -> Dict[str, Any]:
    """The S3 short facts after an UP-spike beyond the stock's own volatility; {} when the window is too short or there is no up-spike. The engine needs no `liquid` for a short."""
    sp = spike_z(rows, sign=1)
    if not sp:
        return {}
    out: Dict[str, Any] = {"z_move": sp["z"], "kind": "spike"}
    try:
        t = sp["peak_idx"]
        high = rows[t][2]
        recent_high = max(r[2] for r in rows[-3:])
        near = high > 0 and recent_high >= high * (1.0 - FAILURE_NEAR_LOW_PCT / 100.0)
        out["failure_candle"] = bool(candle_signal(rows) < 0 and near)                 # shooting star / bearish engulfing at the spike's high
        out["tape_exhaustion"] = tape_exhaustion_short(tape)
        p, vw, a = _num(price), _num(vwap), _num(atr_pct)
        thr = VWAP_STRETCH_ATR_FRAC * a if a and a > 0 else VWAP_STRETCH_FALLBACK_PCT
        out["vwap_stretch"] = bool(p and vw and vw > 0 and (p - vw) / vw * 100.0 >= thr)
        mean_v = sum(r[5] for r in rows) / len(rows)
        around = [r[5] for r in rows[max(0, t - 1):t + 2]]
        out["climax_volume"] = bool(mean_v > 0 and around and max(around) >= CLIMAX_VOL_MULT * mean_v)
        out["wall_close"] = wall_close_short(tape, price)
    except Exception:  # noqa: BLE001
        pass
    return out


def s2_facts_short(rows: Sequence[Tuple[float, ...]], *, or_low: Any, range_end_ts: Optional[float], orb: Any, price: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        orb = orb if isinstance(orb, dict) else {}
        ol, p = _num(or_low), _num(price)
        out["or_break"] = -1 if (orb.get("close_trigger") is True and ol and p and p <= ol) else 0
        out["pullback_hold"] = pullback_hold_short(rows, ol, range_end_ts)
        v = vol_vs_norm(rows)
        if v is not None:
            out["vol_vs_norm"] = v
        out["candle"] = candle_signal(rows)                                              # -1 = shooting star / bearish engulfing = the short's rejection candle
    except Exception:  # noqa: BLE001
        pass
    return out


def gather_short(*, symbol: str, price: Any, now_ts: float, orb: Any, quote: Any, gap_pct: Any, tape: Any, want_s2: bool, want_s3: bool, board_payload: Any = None,
                 bars: Any = None, structure_summary: Any = None, prev_atr_pct: Any = None, range_end_ts: Optional[float] = None) -> Dict[str, Any]:
    """The S2 / S3 facts of a SHORT (S4's extras come from setup_short.gather). Each source is read inside its own try; a source that is down loses only the facts it feeds.
    The keyword arguments with a value skip the live read (that is how the tests run it). `orb` is the SHORT opening-range record (range_low, close_trigger)."""
    out: Dict[str, Any] = {}
    if not (want_s2 or want_s3):
        return out
    q = quote if isinstance(quote, dict) else {}
    try:
        rows = closed_bars(bars if bars is not None else __import__("feed.bar_store", fromlist=["get_bars"]).get_bars(str(symbol).upper()), now_ts)
    except Exception:  # noqa: BLE001
        rows = []
    if range_end_ts is None:
        range_end_ts = range_end_for(now_ts)
    o = orb if isinstance(orb, dict) else {}
    if want_s2:
        try:
            payload = board_payload
            if payload is None:
                from screening import board
                payload = board.build(None, None, [])
            r = rs_pct(payload, symbol)
            if r is not None:
                out["rs_pct"] = r                                                        # a bear-side board row = 0.0 = the weakest of the day; not on the board = missing
            summ = structure_summary
            if summ is None:
                from orders import structure_gate
                summ = structure_gate.fetch_summary(symbol)
            from orders import structure_gate as _sg
            out["structure_ok"] = structure_ok_short(summ, _sg.no_data_reason(summ, price))
            out.update(s2_facts_short(rows, or_low=o.get("range_low"), range_end_ts=range_end_ts, orb=o, price=price))
            if gap_pct is not None:
                out["gap_pct"] = float(gap_pct)
        except Exception:  # noqa: BLE001
            pass
    if want_s3:
        try:
            atr = prev_atr_pct
            if atr is None:
                from screening import prev_day
                pdv = prev_day.peek(symbol)
                atr = getattr(pdv, "atr_pct", None) if pdv is not None else None
            out.update(s3_facts_short(rows, price=price, vwap=q.get("avg_price"), atr_pct=atr, tape=tape))
        except Exception:  # noqa: BLE001
            pass
    return out
