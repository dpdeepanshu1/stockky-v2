"""order_flow_tape.py - intraday order-flow tape built from the live quote rows (E268).

WHAT IT ANSWERS (the six-point order-flow checklist):
  1 profile      session volume profile: POC, VAH, VAL (70 % value area) and where the price sits
  2 HVN / LVN    high / low volume nodes of that profile, and the next HVN above / below the price
  3 footprint    who is aggressive: buy vs sell volume per 1-minute bar and per price level, stacked imbalances
  4 imbalance    resting-book imbalance (best 5, from the row), absorption and exhaustion on the bars
  5 delta        buy minus sell volume, and whether it agrees with the price move
  6 big prints   interval volume blocks far above that symbol's normal interval

HOW THE NUMBERS ARE BUILT - read this before trusting them:
  Dhan's quote row carries the day's CUMULATIVE volume and the day's average price (VWAP), not individual trades. Each time a row arrives with a
  higher cumulative volume the difference is one "interval" of trading (a second or two). Its average trade price is recovered from the VWAP change:
      (avg_new * vol_new - avg_old * vol_old) / interval_volume
  The interval is classified:
    1. trade price >= best ask -> buy; <= best bid -> sell (quote rule)
    2. else above the previous trade price -> buy, below -> sell (tick rule)
    3. else the previous side; with none, neutral (kept out of delta).
  So delta is an ESTIMATE from snapshots, not a real aggressor feed; a footprint is only as fine as the snapshot interval. A "big print" is an
  interval block (several trades inside one snapshot), not a single trade. Every summary says how much of the day's volume the tape has seen
  (`coverage_pct`) and `reliable` is False until enough has been seen: callers must treat an unreliable answer as "no opinion".

Pure Python, thread-safe, never raises out of a public function. Memory is bounded (symbols, bars per symbol, price levels per bar).
Switches (read at call time): FLOW_TAPE_ENABLED (1), FLOW_TAPE_MAX_SYMBOLS (1500), FLOW_BIG_PRINT_MULT (8), FLOW_BIG_PRINT_MIN_VALUE_RS (500000),
FLOW_MIN_INTERVALS (40), FLOW_MIN_COVERAGE_PCT (35), FLOW_MIN_TAPE_MINUTES (8), FLOW_GATE_MIN_AGAINST (2).
"""
from __future__ import annotations

import os
import statistics
import threading
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

_IST = timezone(timedelta(hours=5, minutes=30))
_MAX_BARS = 420
_MAX_LEVELS_PER_BAR = 24
_MAX_BINS = 400
_INTERVAL_MEMORY = 90
_STALE_ROW_S = 120.0
_GAP_S = 120.0

_lock = threading.Lock()
_state: "OrderedDict[str, _Sym]" = OrderedDict()
_stats = {"rows": 0, "intervals": 0, "skipped_no_volume": 0, "skipped_reset": 0, "skipped_stale": 0, "skipped_reorder": 0, "symbols_evicted": 0, "errors": 0}


# ── settings (call time) ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _envf(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else float(default)
    except (TypeError, ValueError):
        return float(default)


def enabled() -> bool:
    return str(os.getenv("FLOW_TAPE_ENABLED", "1")).strip().lower() not in ("0", "false", "no", "off")


def _max_symbols() -> int:
    return int(max(50, _envf("FLOW_TAPE_MAX_SYMBOLS", 1500)))


def _f(v) -> Optional[float]:
    try:
        if v is None or isinstance(v, bool):
            return None
        x = float(v)
        return x if x == x and x not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


# ── per-symbol state ─────────────────────────────────────────────────────────────────────────────────────────────────────────
class _Bar:
    __slots__ = ("minute", "o", "h", "l", "c", "buy", "sell", "neutral", "big_buy", "big_sell", "big_buy_val", "big_sell_val", "levels")

    def __init__(self, minute: int, px: float):
        self.minute = minute
        self.o = self.h = self.l = self.c = px
        self.buy = self.sell = self.neutral = 0.0
        self.big_buy = self.big_sell = 0
        self.big_buy_val = self.big_sell_val = 0.0
        self.levels: Dict[int, List[float]] = {}


class _Sym:
    __slots__ = ("day", "bin", "first_vol", "last_vol", "last_avg", "last_px", "last_side", "bars", "bins", "ivols", "intervals",
                 "tape_volume", "started_at", "last_at", "last_row")

    def __init__(self, day: str, width: float, vol: float, avg: Optional[float], px: float, now: datetime):
        self.day = day
        self.bin = width
        self.first_vol = vol
        self.last_vol = vol
        self.last_avg = avg
        self.last_px = px
        self.last_side: Optional[str] = None
        self.bars: "OrderedDict[int, _Bar]" = OrderedDict()
        self.bins: Dict[int, float] = {}
        self.ivols: deque = deque(maxlen=_INTERVAL_MEMORY)
        self.intervals = 0
        self.tape_volume = 0.0
        self.started_at = now
        self.last_at = now
        self.last_row: dict = {}


def _key(symbol) -> str:
    s = str(symbol or "").strip().upper()
    for suf in (".NS", ".BO"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s


def _bin_width(price: float) -> float:
    w = round(price * 0.0005 / 0.05) * 0.05          # 5 bps of price on the 0.05 tick grid
    return max(0.05, round(w, 2))


def _to_bin(price: float, width: float) -> int:
    return int(round(price / width))


def _ist_day(now: datetime) -> str:
    return now.astimezone(_IST).strftime("%Y-%m-%d")


def _ist_minute(now: datetime) -> int:
    t = now.astimezone(_IST)
    return t.hour * 60 + t.minute


# ── pure functions ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def interval_price(avg_old, vol_old, avg_new, vol_new, ltp) -> Optional[float]:
    """Average trade price of the volume between two snapshots, recovered from the day's VWAP. Falls back to the LTP when the VWAP is missing or the
    result is more than 2 % from the LTP (a VWAP rounding artefact)."""
    try:
        a0, a1, v0, v1 = _f(avg_old), _f(avg_new), _f(vol_old), _f(vol_new)
        if not a0 or not a1 or v0 is None or v1 is None or v1 <= v0:
            return _f(ltp)
        px = (a1 * v1 - a0 * v0) / (v1 - v0)
        l = _f(ltp)
        if px <= 0 or (l and abs(px / l - 1.0) > 0.02):
            return l
        return px
    except Exception:  # noqa: BLE001
        return _f(ltp)


def classify(trade_px, best_bid, best_ask, last_px, last_side) -> str:
    """buy / sell / neutral. Quote rule first, then tick rule, then the previous side."""
    tp, bid, ask, lp = _f(trade_px), _f(best_bid), _f(best_ask), _f(last_px)
    if tp is None:
        return last_side or "neutral"
    if ask and tp >= ask:
        return "buy"
    if bid and tp <= bid:
        return "sell"
    if lp is not None:
        if tp > lp + 1e-9:
            return "buy"
        if tp < lp - 1e-9:
            return "sell"
    return last_side or "neutral"


def build_profile(bins: Dict[int, float], width: float, price: Optional[float] = None, value_area: float = 0.70) -> Optional[dict]:
    """Volume profile from {bin: volume}: POC, VAH, VAL (value_area of the volume around the POC), HVN / LVN lists and where `price` sits."""
    try:
        bins = {k: v for k, v in (bins or {}).items() if v and v > 0}
        if not bins:
            return None
        total = float(sum(bins.values()))
        kmin, kmax = min(bins), max(bins)
        poc = max(bins, key=lambda k: (bins[k], -abs(k - (_to_bin(price, width) if price else k))))
        lo = hi = poc
        acc = bins[poc]
        while acc < value_area * total and (lo > kmin or hi < kmax):
            up = bins.get(hi + 1, 0.0) + bins.get(hi + 2, 0.0) if hi < kmax else -1.0
            dn = bins.get(lo - 1, 0.0) + bins.get(lo - 2, 0.0) if lo > kmin else -1.0
            if up >= dn and hi < kmax:
                hi += 1
                acc += bins.get(hi, 0.0)
            elif lo > kmin:
                lo -= 1
                acc += bins.get(lo, 0.0)
            else:
                hi += 1
                acc += bins.get(hi, 0.0)
        dense = [bins.get(k, 0.0) for k in range(kmin, kmax + 1)]
        sm = []
        for i in range(len(dense)):
            win = dense[max(0, i - 1): i + 2]
            sm.append(sum(win) / len(win))
        nz = [x for x in sm if x > 0]
        mean = (sum(nz) / len(nz)) if nz else 0.0
        hvn, lvn = [], []
        if len(dense) >= 5 and mean > 0:
            for i in range(1, len(sm) - 1):
                if sm[i] >= sm[i - 1] and sm[i] >= sm[i + 1] and (sm[i] > sm[i - 1] or sm[i] > sm[i + 1]) and sm[i] >= 1.3 * mean:
                    hvn.append((sm[i], i + kmin))
                if sm[i] <= sm[i - 1] and sm[i] <= sm[i + 1] and (sm[i] < sm[i - 1] or sm[i] < sm[i + 1]) and sm[i] <= 0.6 * mean:
                    lvn.append((sm[i], i + kmin))
        hvn_px = [round(k * width, 2) for _, k in sorted(hvn, reverse=True)[:8]]
        lvn_px = [round(k * width, 2) for _, k in sorted(lvn)[:8]]
        out = {"poc": round(poc * width, 2), "vah": round(hi * width, 2), "val": round(lo * width, 2), "low": round(kmin * width, 2),
               "high": round(kmax * width, 2), "volume": total, "hvn": hvn_px, "lvn": lvn_px, "bin": width,
               "location": None, "node": None, "hvn_above": None, "hvn_above_pct": None, "hvn_below": None, "hvn_below_pct": None}
        p = _f(price)
        if p:
            out["location"] = "above_value" if p > out["vah"] + width / 2 else ("below_value" if p < out["val"] - width / 2 else "inside_value")
            node = "normal"
            if any(abs(p - h) <= width * 1.5 for h in hvn_px):
                node = "hvn"
            elif any(abs(p - l) <= width * 1.5 for l in lvn_px):
                node = "lvn"
            out["node"] = node
            above = sorted(h for h in hvn_px if h > p + width * 1.5)
            below = sorted((h for h in hvn_px if h < p - width * 1.5), reverse=True)
            if above:
                out["hvn_above"], out["hvn_above_pct"] = above[0], round((above[0] / p - 1.0) * 100.0, 3)
            if below:
                out["hvn_below"], out["hvn_below_pct"] = below[0], round((1.0 - below[0] / p) * 100.0, 3)
        return out
    except Exception:  # noqa: BLE001
        return None


def _stacked(bars: Iterable[_Bar], ratio: float = 3.0, run: int = 3, min_vol: float = 200.0) -> Dict[str, int]:
    """Stacked imbalances: `run` or more adjacent price levels where one side outweighs the other `ratio` to 1 (footprint). Levels of the given bars
    are summed first. Returns the number of stacks per side."""
    agg: Dict[int, List[float]] = {}
    for b in bars:
        for k, (bv, sv) in b.levels.items():
            a = agg.setdefault(k, [0.0, 0.0])
            a[0] += bv
            a[1] += sv
    out = {"buy": 0, "sell": 0}
    for side, i_me, i_oth in (("buy", 0, 1), ("sell", 1, 0)):
        n = 0
        prev = None
        for k in sorted(agg):
            me, oth = agg[k][i_me], agg[k][i_oth]
            ok = me >= min_vol and me >= ratio * max(oth, 1.0)
            if ok and (prev is None or k == prev + 1):
                n += 1
            elif ok:
                n = 1
            else:
                n = 0
            prev = k if ok else None
            if n == run:
                out[side] += 1
    return out


def _sign(x: float, eps: float = 0.0) -> int:
    return 1 if x > eps else (-1 if x < -eps else 0)


def _signals(st: _Sym, price: Optional[float]) -> dict:
    bars = list(st.bars.values())
    out = {"delta_day": 0.0, "buy_pct_day": None, "delta_10m": 0.0, "buy_pct_10m": None, "delta_agrees": None, "delta_ratio_20m": None,
           "price_move_20m_pct": None, "absorption": None, "exhaustion": None, "aggressor": "balanced",
           "big_prints": {"buy": 0, "sell": 0, "buy_value": 0.0, "sell_value": 0.0}, "stacked": {"buy": 0, "sell": 0}}
    if not bars:
        return out
    buy = sum(b.buy for b in bars)
    sell = sum(b.sell for b in bars)
    out["delta_day"] = round(buy - sell, 1)
    out["buy_pct_day"] = round(100.0 * buy / (buy + sell), 1) if buy + sell > 0 else None
    last_m = bars[-1].minute

    def win(n):
        return [b for b in bars if b.minute > last_m - n]

    w10, w20, w30 = win(10), win(20), win(30)
    b10, s10 = sum(b.buy for b in w10), sum(b.sell for b in w10)
    out["delta_10m"] = round(b10 - s10, 1)
    out["buy_pct_10m"] = round(100.0 * b10 / (b10 + s10), 1) if b10 + s10 > 0 else None
    b20, s20 = sum(b.buy for b in w20), sum(b.sell for b in w20)
    out["delta_ratio_20m"] = round((b20 - s20) / (b20 + s20), 3) if b20 + s20 > 0 else None
    move = None
    if w20 and w20[0].o:
        move = (bars[-1].c / w20[0].o - 1.0) * 100.0
        out["price_move_20m_pct"] = round(move, 3)
    if move is not None and out["delta_ratio_20m"] is not None and abs(move) >= 0.05 and abs(out["delta_ratio_20m"]) >= 0.05:
        out["delta_agrees"] = _sign(move) == _sign(out["delta_ratio_20m"])
    # absorption: heavy one-sided volume in the last 5 minutes that does not move the price
    w5 = win(5)
    prior = [b for b in bars if b.minute <= last_m - 5][-20:]
    if len(w5) >= 3 and len(prior) >= 5:
        v5 = sum(b.buy + b.sell for b in w5) / len(w5)
        vp = sum(b.buy + b.sell for b in prior) / len(prior)
        b5, s5 = sum(b.buy for b in w5), sum(b.sell for b in w5)
        rng = (max(b.h for b in w5) - min(b.l for b in w5)) / max(w5[0].o, 1e-9) * 100.0
        mv5 = (w5[-1].c / w5[0].o - 1.0) * 100.0 if w5[0].o else 0.0
        if vp > 0 and v5 >= 1.5 * vp and b5 + s5 > 0 and rng <= 0.15:
            pct5 = b5 / (b5 + s5)
            if pct5 >= 0.60 and mv5 <= 0.03:
                out["absorption"] = "buying_absorbed"
            elif pct5 <= 0.40 and mv5 >= -0.03:
                out["absorption"] = "selling_absorbed"
    # exhaustion: a move of 0.3 %+ in the last 10 minutes whose last 3 minutes of delta have turned against it
    if len(w10) >= 6 and w10[0].o:
        mv10 = (bars[-1].c / w10[0].o - 1.0) * 100.0
        w3 = win(3)
        d3 = sum(b.buy - b.sell for b in w3)
        d_early = sum(b.buy - b.sell for b in w10 if b not in w3)
        if mv10 >= 0.3 and d_early > 0 and d3 < 0:
            out["exhaustion"] = "buy_exhaustion"
        elif mv10 <= -0.3 and d_early < 0 and d3 > 0:
            out["exhaustion"] = "sell_exhaustion"
    bp = out["big_prints"]
    for b in w30:
        bp["buy"] += b.big_buy
        bp["sell"] += b.big_sell
        bp["buy_value"] += b.big_buy_val
        bp["sell_value"] += b.big_sell_val
    bp["buy_value"], bp["sell_value"] = round(bp["buy_value"], 0), round(bp["sell_value"], 0)
    out["stacked"] = _stacked(w10)
    pct = out["buy_pct_10m"]
    if pct is not None:
        if pct >= 58.0 or (out["stacked"]["buy"] > out["stacked"]["sell"] and pct >= 52.0):
            out["aggressor"] = "buyers"
        elif pct <= 42.0 or (out["stacked"]["sell"] > out["stacked"]["buy"] and pct <= 48.0):
            out["aggressor"] = "sellers"
    return out


def _book_imbalance(row: dict) -> Optional[float]:
    b, a = _f((row or {}).get("bid_qty_5")), _f((row or {}).get("ask_qty_5"))
    if b is None or a is None or b + a <= 0:
        return None
    return round((b - a) / (b + a), 3)


def _checklist(sig: dict, prof: Optional[dict], imb5: Optional[float], price: Optional[float]) -> dict:
    """Six checks per side: each is 'for', 'against' or 'neutral'. `against` counts the adverse ones."""
    res = {}
    for side, s in (("long", 1), ("short", -1)):
        c = {}
        loc = (prof or {}).get("location")
        poc = (prof or {}).get("poc")
        if loc is None:
            c["profile"] = "neutral"
        elif loc == ("above_value" if s > 0 else "below_value"):
            c["profile"] = "for"
        elif loc == ("below_value" if s > 0 else "above_value"):
            c["profile"] = "against"
        elif price and poc and (price >= poc if s > 0 else price <= poc):
            c["profile"] = "for"
        else:
            c["profile"] = "neutral"
        wall = (prof or {}).get("hvn_above_pct" if s > 0 else "hvn_below_pct")
        if wall is not None and wall <= 0.3:
            c["nodes"] = "against"
        elif (prof or {}).get("node") == "lvn":
            c["nodes"] = "for"
        else:
            c["nodes"] = "neutral"
        me, oth = ("buy", "sell") if s > 0 else ("sell", "buy")
        ag = sig.get("aggressor")
        st_ = sig.get("stacked") or {}
        if ag == ("sellers" if s > 0 else "buyers") or st_.get(oth, 0) > st_.get(me, 0) >= 0 and st_.get(oth, 0) >= 1:
            c["footprint"] = "against"
        elif ag == ("buyers" if s > 0 else "sellers"):
            c["footprint"] = "for"
        else:
            c["footprint"] = "neutral"
        absn, exh = sig.get("absorption"), sig.get("exhaustion")
        bad_abs = "buying_absorbed" if s > 0 else "selling_absorbed"
        good_abs = "selling_absorbed" if s > 0 else "buying_absorbed"
        bad_exh = "buy_exhaustion" if s > 0 else "sell_exhaustion"
        lean = None if imb5 is None else imb5 * s
        if absn == bad_abs or exh == bad_exh or (lean is not None and lean <= -0.3):
            c["imbalance"] = "against"
        elif absn == good_abs or (lean is not None and lean >= 0.3):
            c["imbalance"] = "for"
        else:
            c["imbalance"] = "neutral"
        pct = sig.get("buy_pct_10m")
        my_pct = None if pct is None else (pct if s > 0 else 100.0 - pct)
        mv = sig.get("price_move_20m_pct")
        if (my_pct is not None and my_pct < 45.0) or (sig.get("delta_agrees") is False and mv is not None and _sign(mv) == s):
            c["delta"] = "against"
        elif my_pct is not None and my_pct >= 55.0 and sig.get("delta_agrees") is not False:
            c["delta"] = "for"
        else:
            c["delta"] = "neutral"
        bp = sig.get("big_prints") or {}
        mv_, ov_ = (bp.get("buy_value", 0.0), bp.get("sell_value", 0.0)) if s > 0 else (bp.get("sell_value", 0.0), bp.get("buy_value", 0.0))
        if ov_ > 0 and ov_ >= 2.0 * mv_:
            c["big_prints"] = "against"
        elif mv_ > 0 and mv_ >= 2.0 * ov_:
            c["big_prints"] = "for"
        else:
            c["big_prints"] = "neutral"
        against = [k for k, v in c.items() if v == "against"]
        fors = [k for k, v in c.items() if v == "for"]
        res[side] = {"checks": c, "against": len(against), "against_names": against, "for_count": len(fors),
                     "reasons": [f"{k}:{v}" for k, v in c.items() if v != "neutral"]}
    return res


def _reliability(st: _Sym, now: datetime) -> Tuple[bool, str, Optional[float]]:
    cov = None
    if st.last_vol and st.last_vol > 0:
        cov = round(100.0 * st.tape_volume / st.last_vol, 1)
    if st.intervals < int(_envf("FLOW_MIN_INTERVALS", 40)):
        return False, "too_few_intervals", cov
    if (now - st.started_at).total_seconds() < _envf("FLOW_MIN_TAPE_MINUTES", 8) * 60.0:
        return False, "tape_too_young", cov
    if cov is None or cov < _envf("FLOW_MIN_COVERAGE_PCT", 35):
        return False, "low_coverage", cov
    if (now - st.last_at).total_seconds() > _STALE_ROW_S:
        return False, "stale", cov
    return True, "ok", cov


# ── intake ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def _add_interval(st: _Sym, now: datetime, ltp: float, tpx: float, dv: float, side: str) -> None:
    m = _ist_minute(now)
    bar = st.bars.get(m)
    if bar is None:
        bar = _Bar(m, ltp)
        st.bars[m] = bar
        while len(st.bars) > _MAX_BARS:
            st.bars.popitem(last=False)
    bar.c = ltp
    bar.h = max(bar.h, ltp)
    bar.l = min(bar.l, ltp)
    value = dv * tpx
    big = False
    if len(st.ivols) >= 20:
        med = statistics.median(st.ivols)
        big = med > 0 and dv >= _envf("FLOW_BIG_PRINT_MULT", 8) * med and value >= _envf("FLOW_BIG_PRINT_MIN_VALUE_RS", 500000)
    st.ivols.append(dv)
    b = _to_bin(tpx, st.bin)
    if side == "buy":
        bar.buy += dv
        if big:
            bar.big_buy += 1
            bar.big_buy_val += value
    elif side == "sell":
        bar.sell += dv
        if big:
            bar.big_sell += 1
            bar.big_sell_val += value
    else:
        bar.neutral += dv
    if side in ("buy", "sell") and (b in bar.levels or len(bar.levels) < _MAX_LEVELS_PER_BAR):
        lv = bar.levels.setdefault(b, [0.0, 0.0])
        lv[0 if side == "buy" else 1] += dv
    if b in st.bins or len(st.bins) < _MAX_BINS:
        st.bins[b] = st.bins.get(b, 0.0) + dv
    st.intervals += 1
    st.tape_volume += dv
    _stats["intervals"] += 1


def _observe(row: dict, now: datetime) -> int:
    sym = _key(row.get("symbol"))
    px = _f(row.get("price"))
    if not sym or not px or px <= 0:
        return 0
    vol = _f(row.get("volume"))
    if vol is None or vol < 0:
        _stats["skipped_no_volume"] += 1
        return 0
    avg = _f(row.get("avg_price"))
    day = _ist_day(now)
    st = _state.get(sym)
    if st is None or st.day != day:
        if st is None and len(_state) >= _max_symbols():
            _state.popitem(last=False)
            _stats["symbols_evicted"] += 1
        st = _Sym(day, _bin_width(px), vol, avg, px, now)
        _state[sym] = st
    else:
        _state.move_to_end(sym)
    st.last_row = {"bid_qty_5": row.get("bid_qty_5"), "ask_qty_5": row.get("ask_qty_5"), "price": px}
    gap = (now - st.last_at).total_seconds()
    st.last_at = now
    if st.intervals == 0 and st.tape_volume == 0 and vol == st.first_vol and st.last_vol == vol:
        return 0                                           # the anchor row itself
    if vol < st.last_vol and vol >= st.last_vol * 0.98:    # a slightly older REST row arriving after a websocket tick: ignore, do not re-anchor
        _stats["skipped_reorder"] += 1
        return 0
    if vol < st.last_vol:                                  # cumulative volume went backwards: a reset / bad row, re-anchor
        _stats["skipped_reset"] += 1
        st.last_vol, st.last_avg, st.last_px = vol, avg, px
        return 0
    if vol == st.last_vol:                                 # nothing traded
        return 0
    dv = vol - st.last_vol
    if gap > _GAP_S or (st.intervals > 0 and dv > max(5_000_000.0, st.last_vol * 0.5)):
        _stats["skipped_stale"] += 1                       # a gap in the feed: one lump would wreck the classification
        st.last_vol, st.last_avg, st.last_px = vol, avg, px
        return 0
    tpx = interval_price(st.last_avg, st.last_vol, avg, vol, px) or px
    side = classify(tpx, row.get("best_bid"), row.get("best_ask"), st.last_px, st.last_side)
    _add_interval(st, now, px, tpx, dv, side)
    st.last_vol, st.last_avg, st.last_px = vol, avg, tpx
    if side != "neutral":
        st.last_side = side
    return 1


def on_rows(rows: Iterable[dict], now: Optional[datetime] = None) -> int:
    """Listener: feed quote rows into the tape. Returns the number of intervals added. Never raises."""
    if not enabled():
        return 0
    n = 0
    try:
        now = now or datetime.now(timezone.utc)
        with _lock:
            for r in rows or []:
                try:
                    _stats["rows"] += 1
                    n += _observe(r, now)
                except Exception:  # noqa: BLE001
                    _stats["errors"] += 1
    except Exception:  # noqa: BLE001
        _stats["errors"] += 1
    return n


# ── read API ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def snapshot(symbol: str, now: Optional[datetime] = None) -> dict:
    sym = _key(symbol)
    try:
        now = now or datetime.now(timezone.utc)
        with _lock:
            st = _state.get(sym)
            if st is None or st.day != _ist_day(now):
                return {"symbol": sym, "available": False, "reliable": False, "reason": "no_tape"}
            reliable, why, cov = _reliability(st, now)
            price = _f(st.last_row.get("price")) or st.last_px
            prof = build_profile(st.bins, st.bin, price)
            sig = _signals(st, price)
            imb = _book_imbalance(st.last_row)
            cl = _checklist(sig, prof, imb, price)
            return {"symbol": sym, "available": True, "reliable": reliable, "reason": why, "coverage_pct": cov, "intervals": st.intervals,
                    "tape_minutes": round((now - st.started_at).total_seconds() / 60.0, 1), "price": price, "profile": prof, "signals": sig,
                    "imbalance_5": imb, "checklist": cl}
    except Exception as e:  # noqa: BLE001
        _stats["errors"] += 1
        return {"symbol": sym, "available": False, "reliable": False, "reason": f"error:{type(e).__name__}"}


def verdict(symbol: str, side: str = "LONG", min_against: Optional[int] = None, now: Optional[datetime] = None) -> dict:
    """Compact gate answer. `block` is True only on a RELIABLE tape with at least `min_against` adverse checks for the side."""
    s = "short" if str(side or "").strip().upper() in ("SHORT", "SELL") else "long"
    n_min = int(min(6, max(1, min_against if min_against is not None else _envf("FLOW_GATE_MIN_AGAINST", 2))))
    snap = snapshot(symbol, now)
    base = {"symbol": snap.get("symbol"), "side": s.upper(), "available": bool(snap.get("available")), "reliable": bool(snap.get("reliable")),
            "block": False, "reason": snap.get("reason"), "against": 0, "for_count": 0, "checks": {}, "coverage_pct": snap.get("coverage_pct")}
    if not snap.get("available"):
        return base
    cl = snap["checklist"][s]
    sig, prof = snap["signals"], snap.get("profile") or {}
    base.update({"against": cl["against"], "for_count": cl["for_count"], "checks": cl["checks"], "buy_pct_10m": sig.get("buy_pct_10m"),
                 "delta_agrees": sig.get("delta_agrees"), "aggressor": sig.get("aggressor"), "location": prof.get("location"),
                 "node": prof.get("node"), "price": snap.get("price"),
                 "hvn_above": prof.get("hvn_above"), "hvn_below": prof.get("hvn_below")})
    if snap["reliable"] and cl["against"] >= n_min:
        base["block"] = True
        base["reason"] = f"ORDER_FLOW:{cl['against']}_of_6_against_{s}({','.join(cl['against_names'])})"
    return base


def bars(symbol: str, limit: int = 60) -> list:
    try:
        with _lock:
            st = _state.get(_key(symbol))
            if st is None:
                return []
            out = []
            for b in list(st.bars.values())[-max(1, int(limit)):]:
                out.append({"minute": f"{b.minute // 60:02d}:{b.minute % 60:02d}", "o": b.o, "h": b.h, "l": b.l, "c": b.c, "buy": round(b.buy),
                            "sell": round(b.sell), "neutral": round(b.neutral), "delta": round(b.buy - b.sell), "big_buy": b.big_buy,
                            "big_sell": b.big_sell, "levels": {round(k * st.bin, 2): [round(v[0]), round(v[1])] for k, v in sorted(b.levels.items())}})
            return out
    except Exception:  # noqa: BLE001
        _stats["errors"] += 1
        return []


def status(now: Optional[datetime] = None) -> dict:
    """Tape health. Counters as before, plus (E271) the aggregate the calibration card and the live check read: how many of TODAY's symbols have a
    RELIABLE tape (same rule the gate uses), their average coverage and why the others are not reliable. `reliable` = at least one symbol is."""
    with _lock:
        out = {"enabled": enabled(), "symbols": len(_state), **dict(_stats)}
        try:
            now = now or datetime.now(timezone.utc)
            today, rel, covs, why_n = _ist_day(now), 0, [], {}
            seen = 0
            for st in _state.values():
                if st.day != today:
                    continue
                seen += 1
                ok, why, cov = _reliability(st, now)
                if cov is not None:
                    covs.append(cov)
                if ok:
                    rel += 1
                else:
                    why_n[why] = why_n.get(why, 0) + 1
            out.update({"symbols_today": seen, "reliable_symbols": rel, "reliable": rel > 0,
                        "avg_coverage_pct": round(sum(covs) / len(covs), 1) if covs else None, "unreliable_reasons": why_n})
        except Exception:  # noqa: BLE001 - a health read must never fail
            out.update({"symbols_today": None, "reliable_symbols": None, "reliable": None, "avg_coverage_pct": None, "unreliable_reasons": {}})
        return out


def _reset_for_tests() -> None:
    with _lock:
        _state.clear()
        for k in _stats:
            _stats[k] = 0
