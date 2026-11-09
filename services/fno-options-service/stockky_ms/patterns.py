"""stockky_ms.patterns - C18-C20: candlestick patterns, chart patterns, Bollinger squeeze, golden / death cross, MACD + RSI confluence and the
support / resistance flip. CONTEXT ONLY: nothing here feeds a decision, gate or score. C21: the output is logged (ml_features.build_pattern_features, scoring.pattern_shadow)
as a separate log-only row; it joins a decision only after the B-tools show an edge.

    analyze_patterns(raw_bars, candle_lookback=3) -> dict

Pure function of the bars it is given (no clock, no I/O, no lookahead): a pivot is only used once `PIVOT_K` bars after it exist, and every
section looks at the last bar as "now". Each section is None when there are too few bars, never a guess; the call never raises.

Sections
  candles      21 patterns on the last `candle_lookback` bars: doji, hammer, inverted_hammer, shooting_star, hanging_man, marubozu, bullish / bearish
               engulfing, bullish / bearish harami, piercing_line, dark_cloud_cover, morning_star, evening_star, three_white_soldiers, three_black_crows,
               tweezer_bottom / tweezer_top, dragonfly_doji, gravestone_doji, spinning_top (the last five come from the whyXShubh guide).
               Reversal patterns need the matching prior move (> 1 ATR over the 5 bars before the pattern), otherwise they are not reported.
  chart        13 patterns from confirmed pivots: double_top / bottom, head_and_shoulders (+ inverse), ascending / descending / symmetrical triangle,
               rising / falling wedge, rectangle, bull / bear flag, cup_and_handle. status = forming | confirmed (close beyond the neckline / line by 0.25 ATR).
  bollinger    20-bar band width and its percentile over the last 120 bars; squeeze = percentile <= 10; release = squeeze in the last 5 bars + close outside a band.
  crosses      golden / death cross events of SMA50 / SMA200 (needs 205 bars) and SMA20 / SMA50 (needs 55 bars) in the last 5 bars, plus the current state.
  confluence   MACD(12,26,9) and RSI(14) agreeing: bullish = MACD above signal, histogram rising, RSI 50-70; bearish = the mirror (RSI 30-50).
  sr_flip      a level that was broken and then retested and held from the other side (old resistance now support, old support now resistance).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .atr import atr_series
from .ms_types import C, H, L, O, V, finite, r2, r3

PIVOT_K = 3
MIN_BARS = 30
_BULL, _BEAR, _NEUT = "bullish", "bearish", "neutral"


# ---------------------------------------------------------------- helpers
def _clean(raw_bars: Any) -> List[Tuple[float, float, float, float, float, float]]:
    out = []
    for b in raw_bars or []:
        try:
            if len(b) >= 6 and all(finite(x) for x in b[:5]):
                out.append((float(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5]) if finite(b[5]) else 0.0))
        except (TypeError, ValueError):
            continue
    return out


def _sma(vals: Sequence[float], n: int, i: int) -> Optional[float]:
    return sum(vals[i - n + 1: i + 1]) / n if i >= n - 1 else None


def _ema(vals: Sequence[float], n: int) -> List[float]:
    k, out = 2.0 / (n + 1), []
    for x in vals:
        out.append(x if not out else out[-1] + k * (x - out[-1]))
    return out


def _rsi(closes: Sequence[float], n: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    gain, loss = gain / n, loss / n
    out[n] = 100.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gain = (gain * (n - 1) + max(d, 0.0)) / n
        loss = (loss * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)
    return out


def _pivots(bars: Sequence[Sequence[float]], k: int = PIVOT_K) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """(pivot highs, pivot lows) as (index, price); a pivot at i exists only when bars i+1..i+k are present (confirmed, no lookahead)."""
    ph: List[Tuple[int, float]] = []
    pl: List[Tuple[int, float]] = []
    for i in range(k, len(bars) - k):
        h, l = bars[i][H], bars[i][L]
        if h > max(bars[j][H] for j in range(i - k, i)) and h >= max(bars[j][H] for j in range(i + 1, i + k + 1)):
            ph.append((i, h))
        if l < min(bars[j][L] for j in range(i - k, i)) and l <= min(bars[j][L] for j in range(i + 1, i + k + 1)):
            pl.append((i, l))
    return ph, pl


def _fit(pts: Sequence[Tuple[int, float]]) -> Tuple[float, float]:
    """Least-squares (slope per bar, intercept) through (idx, price) points."""
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    den = sum((p[0] - mx) ** 2 for p in pts)
    s = sum((p[0] - mx) * (p[1] - my) for p in pts) / den if den else 0.0
    return s, my - s * mx


# ---------------------------------------------------------------- candlesticks
def _prior_move(bars, i: int, atr: float) -> Optional[str]:
    """'down' / 'up' when the close fell / rose by more than 1 ATR over the 5 bars before bar i, else None."""
    if i < 6 or not atr:
        return None
    d = bars[i - 1][C] - bars[i - 6][C]
    return "down" if d < -atr else ("up" if d > atr else None)


def _candle_at(bars, i: int, atr: float, vol_avg: Optional[float]) -> List[Dict[str, Any]]:
    o, h, l, c = bars[i][O], bars[i][H], bars[i][L], bars[i][C]
    rng = h - l
    if rng <= 0 or not atr:
        return []
    body = abs(c - o)
    up, lo = h - max(o, c), min(o, c) - l
    bull, bear = c > o, c < o
    prior = _prior_move(bars, i, atr)
    found: List[Tuple[str, str]] = []
    if body <= 0.1 * rng:
        if lo >= 0.6 * rng and up <= 0.1 * rng:
            found.append(("dragonfly_doji", _NEUT))
        elif up >= 0.6 * rng and lo <= 0.1 * rng:
            found.append(("gravestone_doji", _NEUT))
        else:
            found.append(("doji", _NEUT))
    elif body >= 0.9 * rng and rng >= 0.8 * atr:
        found.append(("marubozu", _BULL if bull else _BEAR))
    elif body <= 0.3 * rng and rng >= 0.5 * atr and min(up, lo) >= 0.25 * rng and min(up, lo) >= 0.5 * max(up, lo):
        found.append(("spinning_top", _NEUT))
    if body >= 0.05 * rng:
        if lo >= 2 * body and up <= 0.15 * rng:
            if prior == "down":
                found.append(("hammer", _BULL))
            elif prior == "up":
                found.append(("hanging_man", _BEAR))
        if up >= 2 * body and lo <= 0.15 * rng:
            if prior == "down":
                found.append(("inverted_hammer", _BULL))
            elif prior == "up":
                found.append(("shooting_star", _BEAR))
    if i >= 1:
        po, pc = bars[i - 1][O], bars[i - 1][C]
        pbody = abs(pc - po)
        pbull, pbear = pc > po, pc < po
        big_prev = pbody >= 0.5 * atr
        tw = 0.1 * atr
        if pbear and bull and prior == "down" and pbody >= 0.3 * atr and abs(l - bars[i - 1][L]) <= tw:
            found.append(("tweezer_bottom", _BULL))
        if pbull and bear and prior == "up" and pbody >= 0.3 * atr and abs(h - bars[i - 1][H]) <= tw:
            found.append(("tweezer_top", _BEAR))
        if pbear and bull and big_prev and prior == "down":
            if o <= pc and c >= po and body > pbody:
                found.append(("bullish_engulfing", _BULL))
            elif o >= pc and c <= po and body < pbody:
                found.append(("bullish_harami", _BULL))
            if o <= pc and po > c > (po + pc) / 2.0:
                found.append(("piercing_line", _BULL))
        if pbull and bear and big_prev and prior == "up":
            if o >= pc and c <= po and body > pbody:
                found.append(("bearish_engulfing", _BEAR))
            elif o <= pc and c >= po and body < pbody:
                found.append(("bearish_harami", _BEAR))
            if o >= pc and po < c < (po + pc) / 2.0:
                found.append(("dark_cloud_cover", _BEAR))
    if i >= 2:
        a, b = bars[i - 2], bars[i - 1]
        abody, bbody = abs(a[C] - a[O]), abs(b[C] - b[O])
        p2 = _prior_move(bars, i - 1, atr)
        if a[C] < a[O] and abody >= 0.5 * atr and bbody <= 0.3 * abody and bull and c > (a[O] + a[C]) / 2.0 and p2 == "down":
            found.append(("morning_star", _BULL))
        if a[C] > a[O] and abody >= 0.5 * atr and bbody <= 0.3 * abody and bear and c < (a[O] + a[C]) / 2.0 and p2 == "up":
            found.append(("evening_star", _BEAR))
        x, y, z = bars[i - 2], bars[i - 1], bars[i]
        if all(q[C] > q[O] and (q[C] - q[O]) >= 0.5 * (q[H] - q[L]) for q in (x, y, z)) and x[C] < y[C] < z[C] and x[O] < y[O] < z[O] \
                and x[O] <= y[O] <= x[C] and y[O] <= z[O] <= y[C]:
            found.append(("three_white_soldiers", _BULL))
        if all(q[C] < q[O] and (q[O] - q[C]) >= 0.5 * (q[H] - q[L]) for q in (x, y, z)) and x[C] > y[C] > z[C] and x[O] > y[O] > z[O] \
                and x[C] <= y[O] <= x[O] and y[C] <= z[O] <= y[O]:
            found.append(("three_black_crows", _BEAR))
    vol_ok = bool(vol_avg and bars[i][V] >= 1.2 * vol_avg)
    return [{"name": n, "bias": b, "volume_confirmed": vol_ok} for n, b in found]


def candle_patterns(bars, lookback: int = 3) -> List[Dict[str, Any]]:
    atrs = atr_series(bars)
    out: List[Dict[str, Any]] = []
    n = len(bars)
    for i in range(max(0, n - lookback), n):
        window = [b[V] for b in bars[max(0, i - 20): i]]
        vol_avg = sum(window) / len(window) if len(window) >= 10 and sum(window) > 0 else None
        for p in _candle_at(bars, i, atrs[i] or 0.0, vol_avg):
            p["bars_ago"] = n - 1 - i
            out.append(p)
    return out


def _cup_and_handle(bars, ph, atr: float, price: float, buf: float, tol: float) -> Optional[Dict[str, Any]]:
    """Rounded cup between two level rims (pivot highs), then a short shallow handle that stays in the upper half of the cup.
    Rims within 1.5 x tol of each other, cup 25-130 bars wide, depth >= 3 ATR (and <= 50% of the rim), a U (not a V: >= 35% of the cup
    closes in its lowest quarter), handle 3-30 bars. confirmed = close above the rim by 0.25 ATR."""
    n = len(bars)
    rims = [p for p in ph if p[0] >= n - 150]
    for a in range(len(rims) - 1):
        for b in range(len(rims) - 1, a, -1):
            (il, pl_), (ir, pr) = rims[a], rims[b]
            width = ir - il
            if width < 25 or width > 130 or abs(pl_ - pr) / max(pl_, pr) > 1.5 * tol:
                continue
            rim = max(pl_, pr)
            seg = bars[il:ir + 1]
            bot = min(range(len(seg)), key=lambda j: seg[j][L])
            bottom = seg[bot][L]
            depth = rim - bottom
            if depth < 3 * atr or depth > 0.5 * rim or not (0.2 * width <= bot <= 0.8 * width):
                continue
            if max(x[H] for x in seg) > rim + buf:
                continue
            if sum(1 for x in seg if x[C] <= bottom + 0.25 * depth) < 0.35 * len(seg):
                continue
            handle = bars[ir + 1:]
            if not (3 <= len(handle) <= 30) or min(x[L] for x in handle) < bottom + 0.5 * depth:
                continue
            return {"rim": rim, "bottom": bottom, "depth": depth, "start": il, "rim_right": ir, "handle_bars": len(handle),
                    "status": "confirmed" if price > rim + buf else "forming"}
    return None


# ---------------------------------------------------------------- chart patterns
def chart_patterns(bars, window: int = 120) -> List[Dict[str, Any]]:
    n = len(bars)
    atrs = atr_series(bars)
    atr = atrs[-1] or 0.0
    price = bars[-1][C]
    if not atr or not price:
        return []
    start = max(0, n - window)
    ph, pl = _pivots(bars)
    ph = [p for p in ph if p[0] >= start]
    pl = [p for p in pl if p[0] >= start]
    tol = max(0.01, 0.6 * atr / price)
    buf = 0.25 * atr
    out: List[Dict[str, Any]] = []

    def add(name, bias, status, **kw):
        d = {"name": name, "bias": bias, "status": status}
        d.update({k: (r2(v) if isinstance(v, float) else v) for k, v in kw.items()})
        out.append(d)

    # double top / bottom (last two same-side pivots)
    if len(ph) >= 2 and n - 1 - ph[-1][0] <= 40:
        (i1, p1), (i2, p2) = ph[-2], ph[-1]
        trough = min(b[L] for b in bars[i1:i2 + 1])
        if i2 - i1 >= 5 and abs(p1 - p2) / max(p1, p2) <= tol and max(p1, p2) - trough >= 1.5 * atr:
            add("double_top", _BEAR, "confirmed" if price < trough - buf else "forming", neckline=trough,
                target=trough - (max(p1, p2) - trough), peaks=[i1, i2], start=i1)
    if len(pl) >= 2 and n - 1 - pl[-1][0] <= 40:
        (i1, p1), (i2, p2) = pl[-2], pl[-1]
        peak = max(b[H] for b in bars[i1:i2 + 1])
        if i2 - i1 >= 5 and abs(p1 - p2) / max(p1, p2) <= tol and peak - min(p1, p2) >= 1.5 * atr:
            add("double_bottom", _BULL, "confirmed" if price > peak + buf else "forming", neckline=peak,
                target=peak + (peak - min(p1, p2)), troughs=[i1, i2], start=i1)
    # head and shoulders (+ inverse)
    if len(ph) >= 3 and n - 1 - ph[-1][0] <= 40:
        (ia, a), (ib, b_), (ic, c_) = ph[-3:]
        if b_ > max(a, c_) * (1 + tol) and abs(a - c_) / max(a, c_) <= 1.5 * tol and ib - ia >= 3 and ic - ib >= 3:
            neck = (min(x[L] for x in bars[ia:ib + 1]) + min(x[L] for x in bars[ib:ic + 1])) / 2.0
            add("head_and_shoulders", _BEAR, "confirmed" if price < neck - buf else "forming", neckline=neck, target=neck - (b_ - neck), head=ib, start=ia)
    if len(pl) >= 3 and n - 1 - pl[-1][0] <= 40:
        (ia, a), (ib, b_), (ic, c_) = pl[-3:]
        if b_ < min(a, c_) * (1 - tol) and abs(a - c_) / max(a, c_) <= 1.5 * tol and ib - ia >= 3 and ic - ib >= 3:
            neck = (max(x[H] for x in bars[ia:ib + 1]) + max(x[H] for x in bars[ib:ic + 1])) / 2.0
            add("inverse_head_and_shoulders", _BULL, "confirmed" if price > neck + buf else "forming", neckline=neck, target=neck + (neck - b_), head=ib, start=ia)
    # triangles, wedges, rectangle: lines through the last pivots inside the last 60 bars
    rh = [p for p in ph if p[0] >= n - 60][-3:]
    rl = [p for p in pl if p[0] >= n - 60][-3:]
    if len(rh) >= 2 and len(rl) >= 2 and (rh[-1][0] - rh[0][0]) >= 10 and (rl[-1][0] - rl[0][0]) >= 10:
        sh, bh = _fit(rh)
        sl, bl = _fit(rl)
        flat = 0.05 * atr
        up_now, lo_now = sh * (n - 1) + bh, sl * (n - 1) + bl
        width_now = up_now - lo_now
        first = min(rh[0][0], rl[0][0])
        width_then = (sh * first + bh) - (sl * first + bl)
        inside = lo_now - buf <= price <= up_now + buf
        if width_now > 0 and width_then > 0 and inside:
            converging = width_now < 0.85 * width_then
            hf, lf = abs(sh) <= flat, abs(sl) <= flat
            name = bias = None
            if hf and sl > flat and converging:
                name, bias = "ascending_triangle", _BULL
            elif lf and sh < -flat and converging:
                name, bias = "descending_triangle", _BEAR
            elif sh < -flat and sl > flat and converging:
                name, bias = "symmetrical_triangle", _NEUT
            elif sh > flat and sl > flat and sl > sh and converging:
                name, bias = "rising_wedge", _BEAR
            elif sh < -flat and sl < -flat and sh < sl and converging:
                name, bias = "falling_wedge", _BULL
            elif hf and lf and width_now >= 2 * atr:
                name, bias = "rectangle", _NEUT
            if name:
                status = "confirmed" if (price > up_now + buf or price < lo_now - buf) else "forming"
                add(name, bias, status, upper=up_now, lower=lo_now, start=first, upper0=sh * first + bh, lower0=sl * first + bl)
    # flags: a sharp pole, then a short shallow drift against it. end == n: still inside the flag (forming);
    # end == n - 1: the last bar is the candidate breakout bar (confirmed only when it closes beyond the flag by 0.25 ATR).
    done = False
    for end in (n, n - 1):
        for f in range(5, 16):
            pole = bars[max(0, end - f - 15):end - f]
            flag = bars[end - f:end]
            if len(pole) < 5 or len(flag) < 5:
                continue
            flag_hi, flag_lo = max(b[H] for b in flag), min(b[L] for b in flag)
            ph_i = max(range(len(pole)), key=lambda j: pole[j][H])
            pl_i = min(range(len(pole)), key=lambda j: pole[j][L])
            gain = pole[ph_i][H] - pole[pl_i][L]
            if gain < 4 * atr or (flag_hi - flag_lo) > 0.6 * gain:
                continue
            if pl_i < ph_i and pole[ph_i][H] >= flag_hi - 1e-9 and (pole[ph_i][H] - flag_lo) <= 0.5 * gain and flag[-1][C] <= flag[0][C]:
                if end == n or price > flag_hi + buf:
                    add("bull_flag", _BULL, "forming" if end == n else "confirmed", flag_bars=f, pole_gain=gain, target=price + gain,
                        start=end - f, flag_high=flag_hi, flag_low=flag_lo)
                    done = True
                    break
            if ph_i < pl_i and pole[pl_i][L] <= flag_lo + 1e-9 and (flag_hi - pole[pl_i][L]) <= 0.5 * gain and flag[-1][C] >= flag[0][C]:
                if end == n or price < flag_lo - buf:
                    add("bear_flag", _BEAR, "forming" if end == n else "confirmed", flag_bars=f, pole_gain=gain, target=price - gain,
                        start=end - f, flag_high=flag_hi, flag_low=flag_lo)
                    done = True
                    break
        if done:
            break
    # cup and handle
    cup = _cup_and_handle(bars, ph, atr, price, buf, tol)
    if cup:
        add("cup_and_handle", _BULL, cup["status"], neckline=cup["rim"], target=cup["rim"] + cup["depth"], start=cup["start"],
            cup_low=cup["bottom"], rim_right=cup["rim_right"], handle_bars=cup["handle_bars"])
    return out


# ---------------------------------------------------------------- indicators
def bollinger_squeeze(bars, period: int = 20, look: int = 120, pct_cut: float = 10.0) -> Optional[Dict[str, Any]]:
    closes = [b[C] for b in bars]
    n = len(closes)
    if n < period + look:
        return None

    def width(i):
        w = closes[i - period + 1: i + 1]
        m = sum(w) / period
        sd = (sum((x - m) ** 2 for x in w) / period) ** 0.5
        return (4 * sd / m if m else None), m, sd

    widths = [width(i)[0] for i in range(n - look, n)]
    if any(w is None for w in widths):
        return None
    cur = widths[-1]
    pct = 100.0 * sum(1 for w in widths if w <= cur) / len(widths)
    squeeze_now = pct <= pct_cut
    recent = [100.0 * sum(1 for x in widths if x <= w) / len(widths) <= pct_cut for w in widths[-6:-1]]
    _, m, sd = width(n - 1)
    release = None
    if any(recent) and not squeeze_now:
        if closes[-1] > m + 2 * sd:
            release = _BULL
        elif closes[-1] < m - 2 * sd:
            release = _BEAR
    return {"width": r3(cur), "percentile": r2(pct), "squeeze": squeeze_now, "release": release}


def ma_crosses(bars, lookback: int = 5) -> Dict[str, Any]:
    closes = [b[C] for b in bars]
    n = len(closes)
    out: Dict[str, Any] = {}
    for key, fast, slow in (("ma50_200", 50, 200), ("ma20_50", 20, 50)):
        if n < slow + 5:
            out[key] = None
            continue
        diffs = [(_sma(closes, fast, i) or 0.0) - (_sma(closes, slow, i) or 0.0) for i in range(n - lookback - 1, n)]
        event = None
        for j in range(1, len(diffs)):
            if diffs[j - 1] <= 0 < diffs[j]:
                event = {"type": "golden_cross", "bars_ago": len(diffs) - 1 - j}
            elif diffs[j - 1] >= 0 > diffs[j]:
                event = {"type": "death_cross", "bars_ago": len(diffs) - 1 - j}
        out[key] = {"fast_above_slow": diffs[-1] > 0, "event": event}
    return out


def macd_rsi_confluence(bars) -> Optional[Dict[str, Any]]:
    closes = [b[C] for b in bars]
    if len(closes) < 60:
        return None
    e12, e26 = _ema(closes, 12), _ema(closes, 26)
    macd = [a - b for a, b in zip(e12, e26)]
    sig = _ema(macd, 9)
    hist = [m - s for m, s in zip(macd, sig)]
    rsi = _rsi(closes)[-1]
    if rsi is None:
        return None
    bull = macd[-1] > sig[-1] and hist[-1] > hist[-2] and 50 <= rsi < 70
    bear = macd[-1] < sig[-1] and hist[-1] < hist[-2] and 30 < rsi <= 50
    return {"state": _BULL if bull else (_BEAR if bear else _NEUT), "rsi": r2(rsi), "macd_hist": round(hist[-1], 4), "macd_above_signal": macd[-1] > sig[-1]}


def sr_flips(bars, window: int = 150, retest_within: int = 20) -> List[Dict[str, Any]]:
    n = len(bars)
    atr = (atr_series(bars)[-1] or 0.0)
    price = bars[-1][C]
    if n < MIN_BARS or not atr or not price:
        return []
    ph, pl = _pivots(bars)
    buf = max(0.003 * price, 0.25 * atr)
    out: List[Dict[str, Any]] = []
    for kind, pts in (("support_flip", [p for p in ph if p[0] >= n - window]), ("resistance_flip", [p for p in pl if p[0] >= n - window])):
        for idx, lvl in pts:
            conf = idx + PIVOT_K
            up = kind == "support_flip"
            brk = next((j for j in range(conf + 1, n) if (bars[j][C] > lvl + buf if up else bars[j][C] < lvl - buf)), None)
            if brk is None or brk >= n - 1:
                continue
            after = bars[brk + 1:]
            # the break must hold: no close back through the level since
            if any((b[C] < lvl - buf) if up else (b[C] > lvl + buf) for b in after):
                continue
            def _touch(b):
                return (b[L] <= lvl + buf and b[C] >= lvl - buf) if up else (b[H] >= lvl - buf and b[C] <= lvl + buf)
            rt = next((brk + 1 + k for k, b in enumerate(after) if _touch(b)), None)
            if rt is None or n - 1 - rt > retest_within:
                continue
            if (price > lvl) if up else (price < lvl):
                out.append({"type": kind, "level": r2(lvl), "broken_bars_ago": n - 1 - brk, "retest_bars_ago": n - 1 - rt})
    out.sort(key=lambda d: d["retest_bars_ago"])
    return out[:3]


# ---------------------------------------------------------------- entry point
def analyze_patterns(raw_bars: Any, candle_lookback: int = 3) -> Dict[str, Any]:
    out: Dict[str, Any] = {"context_only": True, "bars": 0, "candles": None, "chart": None, "bollinger": None, "crosses": None,
                           "confluence": None, "sr_flip": None, "errors": []}
    try:
        bars = _clean(raw_bars)
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"input:{type(e).__name__}")
        return out
    out["bars"] = len(bars)
    if len(bars) < MIN_BARS:
        return out
    for key, fn in (("candles", lambda: candle_patterns(bars, candle_lookback)), ("chart", lambda: chart_patterns(bars)),
                    ("bollinger", lambda: bollinger_squeeze(bars)), ("crosses", lambda: ma_crosses(bars)),
                    ("confluence", lambda: macd_rsi_confluence(bars)), ("sr_flip", lambda: sr_flips(bars))):
        try:
            out[key] = fn()
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"{key}:{type(e).__name__}")
    return out
