"""momentum_health.py - pure decision: is an open trade's momentum used up, and what should the exit side do about it (E275, 31 Oct 2026).

Byte-identical in real-trade-service/exit_engine/ and position-stocks-service/orders/ (scripts/check_shared_copies.py, IDENTICAL family). Stdlib only, no I/O,
no config: the caller passes every number, so the same maths runs in both services and in the tests.

One sign s (+1 long, -1 short) mirrors every rule; "better" means s * price is higher. Five signals, each True / False:
  1 exhaustion      the last closed bar made a new best price for the trade while the tape delta is AGAINST it (a reliable tape only)
  2 no_new_best     no new best price for `no_new_best_min` minutes (the setup's median time-to-peak when known, else the caller's fallback) and age >= that
  3 climax_bar      the last closed bar's volume >= `climax_vol_mult` x the mean of the bars before it AND its wick on the profit side >= `climax_wick_frac` of its range
  4 at_wall         price within one tick of the next HVN wall on the profit side, or the gain has reached `typical_gain_frac` of the setup's typical maximum gain
  5 lost_level      `fading` (the caller's exit_adaptive.is_fading: wrong side of VWAP, or lower highs / higher lows) or price back through the entry trigger level
Signals 2 to 5 need the trade to have been in profit (best price beyond entry): a trade that never worked is the stop's and the stagnation rule's business.

Actions by the number of signals on (`n`):
  n >= book_n (3)       BOOK  only when the trade is `min_book_profit_pct` in profit now. `scaleout` False -> the whole quantity; `scaleout` True and quantity >= 4 -> half
                        (a partial exit on 1 to 3 shares is not meaningful, so those always exit fully). Not in profit enough -> falls to TIGHTEN.
  n >= tighten_n (2)    TIGHTEN  the stop sits max(`gap_min_pct`, `gap_range_mult` x the stock's own average 1-minute range) behind the live price, rounded toward
                        the price, never loosened, at least two ticks better, never past the target; the target is cut to one tick before the wall (never raised)
  n == 1                HOLD, but `trail_gap_pct` carries the trail distance recomputed from the stock's current 1-minute range
  n == 0                HOLD
Missing or bad data is "signal off", never an exit. It can only make an exit sooner, never later, and never raises.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence

SIGNALS = ("exhaustion", "no_new_best", "climax_bar", "at_wall", "lost_level")


def _num(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def _floor(x: float, tick: float) -> float:
    return round(math.floor(x / tick + 1e-9) * tick, 4)


def _ceil(x: float, tick: float) -> float:
    return round(math.ceil(x / tick - 1e-9) * tick, 4)


def _toward_price(level: float, s: int, tick: float) -> float:
    """A LONG's stop lands at or BELOW the live price, a SHORT's at or ABOVE it."""
    return _floor(level, tick) if s > 0 else _ceil(level, tick)


def _clean_bars(bars: Optional[Sequence]) -> List[tuple]:
    """(ts, open, high, low, close, volume) rows with finite numbers and high >= low, oldest first. Anything else is dropped."""
    out: List[tuple] = []
    for b in bars or ():
        try:
            ts, o, h, l, c, v = (float(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5]))
        except (TypeError, ValueError, IndexError):
            continue
        if not all(math.isfinite(x) for x in (ts, o, h, l, c, v)) or h < l or l <= 0 or v < 0:
            continue
        out.append((ts, o, h, l, c, v))
    out.sort(key=lambda r: r[0])
    return out


def avg_range_pct(bars: Optional[Sequence], n: int = 10) -> Optional[float]:
    """Average (high - low) / close % of the last `n` bars given (needs at least 3), or None."""
    rows = _clean_bars(bars)[-max(1, int(n)):]
    if len(rows) < 3:
        return None
    rs = [(r[2] - r[3]) / r[4] * 100.0 for r in rows if r[4] > 0]
    return sum(rs) / len(rs) if len(rs) >= 3 else None


def _hold(reason: str, signals: Optional[List[str]] = None, **extra) -> dict:
    sig = list(signals or [])
    return {"action": "HOLD", "new_stop": None, "new_target": None, "book_qty": 0, "book_mode": None, "signals": sig, "count": len(sig),
            "reason": reason, "trail_gap_pct": extra.get("trail_gap_pct")}


def decide(*, side: str, entry, stop, target, ltp, qty, bars, tick, age_min=None, tape=None, fading=None, trigger_level=None,
           no_new_best_min=None, typical_gain_pct=None, scaleout: bool = False,
           tighten_n: int = 2, book_n: int = 3, min_book_profit_pct: float = 0.2,
           climax_vol_mult: float = 2.5, climax_wick_frac: float = 0.5, climax_lookback: int = 10, typical_gain_frac: float = 0.9,
           gap_min_pct: float = 0.15, gap_range_mult: float = 1.5, min_target_pct: float = 0.3, min_scaleout_qty: int = 4) -> dict:
    """{"action": "HOLD"|"TIGHTEN"|"BOOK", "new_stop", "new_target", "book_qty", "book_mode": None|"FULL"|"HALF", "signals", "count", "reason", "trail_gap_pct"}.

    `bars` = this trade's 1-minute bars since entry (ts, o, h, l, c, v), the LAST one still forming is ignored. `tape` = the market-data /flow verdict dict
    (reliable flag, checks, hvn_above / hvn_below). Never raises."""
    try:
        s = -1 if str(side or "").strip().upper() in ("SHORT", "SELL") else 1
        entry, ltp, tick = _num(entry), _num(ltp), _num(tick)
        stop, target = _num(stop), _num(target)
        if entry is None or ltp is None or tick is None:
            return _hold("bad_number")
        try:
            q = int(qty)
        except (TypeError, ValueError):
            q = 0
        if q <= 0:
            return _hold("no_quantity")
        rows = _clean_bars(bars)
        if len(rows) >= 2:
            rows = rows[:-1]                      # the newest bar is still forming
        profit_pct = s * (ltp / entry - 1.0) * 100.0
        rng = avg_range_pct(rows, 10)

        best_px = None                            # the trade's best price on closed bars (highest high for a long, lowest low for a short)
        best_ts = None
        for r in rows:
            px = r[2] if s > 0 else r[3]
            if best_px is None or s * (px - best_px) > 0:
                best_px, best_ts = px, r[0]
        was_in_profit = (best_px is not None and s * (best_px - entry) > 0) or profit_pct > 0

        on: List[str] = []
        # 1 exhaustion: the last closed bar set a new best while the delta is against the trade
        tp = tape if isinstance(tape, dict) else {}
        if len(rows) >= 2 and tp.get("available", True) and tp.get("reliable") and (tp.get("checks") or {}).get("delta") == "against":
            last, prior = rows[-1], rows[:-1]
            prior_best = max(r[2] for r in prior) if s > 0 else min(r[3] for r in prior)
            last_best = last[2] if s > 0 else last[3]
            if s * (last_best - prior_best) > 0 and s * (last_best - entry) > 0:
                on.append("exhaustion")
        # 2 no new best within the time the setup normally needs
        nnb = _num(no_new_best_min)
        age = _num(age_min)
        if nnb is not None and age is not None and age >= nnb and was_in_profit and best_ts is not None and rows:
            if (rows[-1][0] + 60.0 - best_ts) / 60.0 >= nnb:
                on.append("no_new_best")
        # 3 climax bar: heavy volume with a long wick on the profit side, only while in profit
        if len(rows) >= 4 and profit_pct > 0:
            last = rows[-1]
            base = rows[-1 - max(3, int(climax_lookback)):-1]
            mean_v = sum(r[5] for r in base) / len(base) if base else 0.0
            rg = last[2] - last[3]
            if mean_v > 0 and rg > 0 and last[5] >= float(climax_vol_mult) * mean_v:
                wick = (last[2] - max(last[1], last[4])) if s > 0 else (min(last[1], last[4]) - last[3])
                if wick / rg >= float(climax_wick_frac):
                    on.append("climax_bar")
        # 4 the next wall or the setup's typical maximum gain
        wall = _num(tp.get("hvn_above") if s > 0 else tp.get("hvn_below")) if tp.get("reliable", True) else None
        at_wall = wall is not None and s * (wall - ltp) >= 0 and s * (wall - ltp) <= tick + 1e-9
        tg = _num(typical_gain_pct)
        at_typ = tg is not None and profit_pct >= float(typical_gain_frac) * tg
        if (at_wall or at_typ) and profit_pct > 0:
            on.append("at_wall")
        # 5 lost VWAP / the trigger level (needs the trade to have been in profit)
        lvl = _num(trigger_level)
        lost = (fading is True) or (lvl is not None and s * (ltp - lvl) < 0)
        if lost and was_in_profit:
            on.append("lost_level")

        n = len(on)
        trail_gap = None if rng is None else round(max(float(gap_min_pct), float(gap_range_mult) * rng), 3)
        if n < 2:
            return _hold("healthy" if n == 0 else "one_signal", on, trail_gap_pct=trail_gap)

        names = ",".join(on)
        if n >= int(book_n) and profit_pct >= float(min_book_profit_pct):
            if scaleout and q >= int(min_scaleout_qty):
                return {"action": "BOOK", "new_stop": None, "new_target": None, "book_qty": q // 2, "book_mode": "HALF", "signals": on, "count": n,
                        "reason": f"MOMENTUM_BOOK:{n}_signals({names})", "trail_gap_pct": trail_gap}
            return {"action": "BOOK", "new_stop": None, "new_target": None, "book_qty": q, "book_mode": "FULL", "signals": on, "count": n,
                    "reason": f"MOMENTUM_BOOK:{n}_signals({names})", "trail_gap_pct": trail_gap}

        # TIGHTEN: stop behind price by the stock's own 1-minute range, target one tick before the wall
        new_stop = None
        if stop is not None and s * (ltp - stop) > 0:
            gap = max(float(gap_min_pct), float(gap_range_mult) * rng) if rng is not None else float(gap_min_pct)
            cand = _toward_price(ltp * (1.0 - s * gap / 100.0), s, tick)
            better = s * (cand - stop) >= 2 * tick - 1e-9
            inside = s * (ltp - cand) > 0
            ok_target = target is None or s * (target - cand) > 0
            if better and inside and ok_target:
                new_stop = cand
        new_target = None
        if wall is not None and target is not None and s * (wall - ltp) > 0:
            cand_t = round(wall - s * tick, 4)
            pays = s * (cand_t / entry - 1.0) * 100.0 >= float(min_target_pct)
            if pays and s * (target - cand_t) > 0 and s * (cand_t - ltp) > 0:
                new_target = cand_t
        if new_stop is None and new_target is None:
            return _hold("nothing_to_tighten", on, trail_gap_pct=trail_gap)
        return {"action": "TIGHTEN", "new_stop": new_stop, "new_target": new_target, "book_qty": 0, "book_mode": None, "signals": on, "count": n,
                "reason": f"MOMENTUM_TIGHTEN:{n}_signals({names})", "trail_gap_pct": trail_gap}
    except Exception:  # noqa: BLE001 - no opinion
        return _hold("error")
